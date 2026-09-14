[CmdletBinding()]
param(
    [string]$Profile = "billing-assessment",
    [string]$Region = "eu-west-1",
    [string]$StackName = "billing-assessment",
    [ValidateRange(1, 12)][int]$Hours = 3,
    [ValidateSet("t3.large", "t3.xlarge")][string]$InstanceType = "t3.large",
    [string]$AllowedCidr = "",
    [ValidatePattern("^[a-z0-9][a-z0-9._/-]*(?::[a-zA-Z0-9][a-zA-Z0-9._-]*)?$")]
    [string]$ImageTag = "agentic-ai-billing-agent:local"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Aws {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & aws @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "AWS CLI command failed: aws $($Arguments -join ' ')"
    }
}

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "AWS CLI v2 is required. Install it, then run 'aws configure sso --profile $Profile'."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required. Run the local container test before deploying."
}

& aws sts get-caller-identity --profile $Profile --region $Region --no-cli-pager | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "No active AWS SSO session for '$Profile'. Run 'aws sso login --profile $Profile' and retry."
}

if (-not $AllowedCidr) {
    try {
        $publicIp = (Invoke-RestMethod -Uri "https://checkip.amazonaws.com" -TimeoutSec 10).Trim()
        $AllowedCidr = "$publicIp/32"
    }
    catch {
        throw "Could not determine your public IP. Retry with -AllowedCidr '<your-public-ip>/32'."
    }
}
if ($AllowedCidr -eq "0.0.0.0/0") {
    throw "Refusing a public-to-everyone deployment. Supply your public IP as a /32 CIDR."
}

$template = Join-Path $PSScriptRoot "ephemeral-stack.yaml"
$expiresAt = (Get-Date).ToUniversalTime().AddHours($Hours).ToString("yyyy-MM-ddTHH:mm:ss")
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "billing-aws-$PID"
$imageArchive = Join-Path $tempRoot "application-image.tar"
$requestFile = Join-Path $tempRoot "ssm-request.json"
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

try {
    $imageId = [string](& docker image inspect --format "{{.Id}}" $ImageTag 2>$null)
    $imageId = $imageId.Trim()
    if ($LASTEXITCODE -ne 0 -or -not $imageId) {
        throw "Local image '$ImageTag' was not found. Run '.\deploy\local.ps1' and test it first."
    }
    $qualityFingerprint = [string](& docker run --rm --read-only `
        --tmpfs /tmp:rw,nosuid,nodev,size=256m `
        --tmpfs /app/data:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0770 `
        --tmpfs /app/runtime:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0770 `
        $ImageTag python scripts/runtime_check.py --strict --fingerprint-only)
    $qualityFingerprint = $qualityFingerprint.Trim()
    if ($LASTEXITCODE -ne 0 -or -not $qualityFingerprint) {
        throw "The locally tested image failed the strict OCR runtime check."
    }
    Write-Host "Packaging the tested image $imageId (quality profile $qualityFingerprint)..."
    & docker save --output $imageArchive $ImageTag
    if ($LASTEXITCODE -ne 0) { throw "Could not export local image '$ImageTag'." }
    $imageSha256 = (Get-FileHash -Algorithm SHA256 -Path $imageArchive).Hash.ToLowerInvariant()

    Write-Host "Creating/updating $StackName in $Region; automatic deletion: $expiresAt UTC"
    Invoke-Aws cloudformation deploy `
        --profile $Profile `
        --region $Region `
        --stack-name $StackName `
        --template-file $template `
        --capabilities CAPABILITY_NAMED_IAM `
        --parameter-overrides "AllowedCidr=$AllowedCidr" "ExpiresAt=$expiresAt" "InstanceType=$InstanceType" `
        --tags "project=billing-assessment" "temporary=true" "expires-at=$expiresAt" `
        --no-cli-pager

    $outputsJson = & aws cloudformation describe-stacks --profile $Profile --region $Region --stack-name $StackName --query "Stacks[0].Outputs" --output json --no-cli-pager
    if ($LASTEXITCODE -ne 0) { throw "Could not read CloudFormation outputs." }
    $outputMap = @{}
    foreach ($output in ($outputsJson | ConvertFrom-Json)) {
        $outputMap[$output.OutputKey] = $output.OutputValue
    }
    $instanceId = $outputMap["InstanceId"]
    $bucket = $outputMap["DeploymentBucketName"]

    Invoke-Aws s3 cp $imageArchive "s3://$bucket/application-image.tar" --profile $Profile --region $Region --only-show-errors

    Write-Host "Waiting for the EC2 instance to register with Systems Manager..."
    $online = $false
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        $ping = & aws ssm describe-instance-information --profile $Profile --region $Region --filters "Key=InstanceIds,Values=$instanceId" --query "InstanceInformationList[0].PingStatus" --output text --no-cli-pager 2>$null
        if ($LASTEXITCODE -eq 0 -and $ping -eq "Online") {
            $online = $true
            break
        }
        Start-Sleep -Seconds 10
    }
    if (-not $online) {
        throw "Instance $instanceId did not become available in Systems Manager. The stack will still auto-delete at $expiresAt UTC."
    }

    $commands = @(
        "set -euxo pipefail",
        "mkdir -p /opt/billing/data /opt/billing/runtime",
        "aws s3 cp s3://$bucket/application-image.tar /tmp/application-image.tar",
        "echo '$imageSha256  /tmp/application-image.tar' | sha256sum --check -",
        "docker load --input /tmp/application-image.tar",
        "rm -f /tmp/application-image.tar",
        "chown -R 10001:10001 /opt/billing/data /opt/billing/runtime",
        "docker rm -f billing-api billing-dashboard billing-gradio 2>/dev/null || true",
        "docker run -d --name billing-gradio --restart unless-stopped --read-only --security-opt no-new-privileges --cap-drop ALL --log-opt max-size=10m --log-opt max-file=2 --tmpfs /tmp:rw,nosuid,nodev,size=1g -p 7860:7860 -e INVOICE_DATA_ROOT=/app/data -e RAG_DB_PATH=/app/runtime/knowledge_base.sqlite3 -e VECTOR_STORE_DIR=/app/runtime/vector_store -e OCR_LANGUAGES=por+eng -e OCR_DPI=300 -e OCR_TIMEOUT_SECONDS=900 -e OCR_IMAGE_MIN_DIMENSION=1800 -e OCR_IMAGE_MAX_PIXELS=24000000 -e OCR_IMAGE_MAX_SCALE=3.0 -e OMP_THREAD_LIMIT=2 -e ALLOW_CONTAINER_BIND=1 -e HEALTHCHECK_PORT=7860 -e HEALTHCHECK_PATH=/ -v /opt/billing/data:/app/data -v /opt/billing/runtime:/app/runtime $ImageTag python scripts/gradio_app.py --host 0.0.0.0 --port 7860",
        "docker run --rm --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m --tmpfs /app/data:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0770 --tmpfs /app/runtime:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0770 $ImageTag python scripts/runtime_check.py --strict --expect-fingerprint $qualityFingerprint",
        "curl --fail --retry 12 --retry-delay 5 http://127.0.0.1:7860/ >/dev/null"
    )
    $request = @{
        DocumentName = "AWS-RunShellScript"
        InstanceIds = @($instanceId)
        Comment = "Deploy the Agentic Invoice Parser Plan A/B lab"
        TimeoutSeconds = 3600
        Parameters = @{ commands = $commands }
    }
    $request | ConvertTo-Json -Depth 6 | Set-Content -Path $requestFile -Encoding utf8
    $commandId = & aws ssm send-command --profile $Profile --region $Region --cli-input-json "file://$requestFile" --query "Command.CommandId" --output text --no-cli-pager
    if ($LASTEXITCODE -ne 0 -or -not $commandId) { throw "Could not start the remote deployment command." }

    Write-Host "Loading the locally tested image and starting the containers on $instanceId..."
    $terminalStatus = ""
    for ($attempt = 1; $attempt -le 240; $attempt++) {
        $terminalStatus = & aws ssm get-command-invocation --profile $Profile --region $Region --command-id $commandId --instance-id $instanceId --query "Status" --output text --no-cli-pager 2>$null
        if ($terminalStatus -in @("Success", "Cancelled", "TimedOut", "Failed", "Cancelling")) { break }
        Start-Sleep -Seconds 10
    }
    if ($terminalStatus -ne "Success") {
        & aws ssm get-command-invocation --profile $Profile --region $Region --command-id $commandId --instance-id $instanceId --query "StandardErrorContent" --output text --no-cli-pager
        throw "Remote deployment ended with status '$terminalStatus'. The stack will still auto-delete at $expiresAt UTC."
    }

    Write-Host "Deployment ready."
    Write-Host "Image:      $imageId"
    Write-Host "OCR profile: $qualityFingerprint"
    Write-Host "Gradio lab: $($outputMap['GradioUrl'])"
    Write-Host "Auto-delete: $($outputMap['ExpiresAtUtc']) UTC"
    Write-Host "Delete early: .\deploy\aws\destroy.ps1 -Profile $Profile -Region $Region -StackName $StackName"
}
finally {
    Remove-Item -Path $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
