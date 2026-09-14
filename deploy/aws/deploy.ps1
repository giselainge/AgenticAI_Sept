[CmdletBinding()]
param(
    [string]$Profile = "iseg",
    [string]$Region = "eu-west-1",
    [string]$StackName = "iseg-agentic-invoice-demo",
    [ValidateRange(1, 12)][int]$Hours = 3,
    [ValidateSet("t3.large", "t3.xlarge")][string]$InstanceType = "t3.large",
    [string]$AllowedCidr = ""
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
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw "tar is required to package the application. Windows 10/11 includes tar.exe."
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

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$template = Join-Path $PSScriptRoot "ephemeral-stack.yaml"
$expiresAt = (Get-Date).ToUniversalTime().AddHours($Hours).ToString("yyyy-MM-ddTHH:mm:ss")
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) "billing-aws-$PID"
$bundle = Join-Path $tempRoot "application.tar.gz"
$requestFile = Join-Path $tempRoot "ssm-request.json"
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

try {
    Write-Host "Creating/updating $StackName in $Region; automatic deletion: $expiresAt UTC"
    Invoke-Aws cloudformation deploy `
        --profile $Profile `
        --region $Region `
        --stack-name $StackName `
        --template-file $template `
        --capabilities CAPABILITY_NAMED_IAM `
        --parameter-overrides "AllowedCidr=$AllowedCidr" "ExpiresAt=$expiresAt" "InstanceType=$InstanceType" `
        --tags "project=agentic-invoice-parser" "temporary=true" "expires-at=$expiresAt" `
        --no-cli-pager

    $outputsJson = & aws cloudformation describe-stacks --profile $Profile --region $Region --stack-name $StackName --query "Stacks[0].Outputs" --output json --no-cli-pager
    if ($LASTEXITCODE -ne 0) { throw "Could not read CloudFormation outputs." }
    $outputMap = @{}
    foreach ($output in ($outputsJson | ConvertFrom-Json)) {
        $outputMap[$output.OutputKey] = $output.OutputValue
    }
    $instanceId = $outputMap["InstanceId"]
    $bucket = $outputMap["DeploymentBucketName"]

    Push-Location $repoRoot
    try {
        & tar -czf $bundle `
            --exclude="rag/knowledge_base.json" `
            --exclude="rag/last_retrieval_context.json" `
            --exclude="*/__pycache__" `
            .dockerignore Dockerfile requirements.txt invoice_parser llm rag scripts vector_store
        if ($LASTEXITCODE -ne 0) { throw "Application packaging failed." }
    }
    finally {
        Pop-Location
    }
    Invoke-Aws s3 cp $bundle "s3://$bucket/application.tar.gz" --profile $Profile --region $Region --only-show-errors

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
        "mkdir -p /opt/billing/app /opt/billing/data /opt/billing/runtime",
        "aws s3 cp s3://$bucket/application.tar.gz /tmp/application.tar.gz",
        "find /opt/billing/app -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
        "tar -xzf /tmp/application.tar.gz -C /opt/billing/app",
        "chown -R 10001:10001 /opt/billing/data /opt/billing/runtime",
        "cd /opt/billing/app",
        "docker build --pull --tag agentic-ai-billing-agent:demo .",
        "docker rm -f billing-api billing-dashboard 2>/dev/null || true",
        "docker run -d --name billing-api --restart unless-stopped --read-only --security-opt no-new-privileges --cap-drop ALL --tmpfs /tmp:rw,nosuid,nodev,size=512m -p 8000:8000 -e INVOICE_DATA_ROOT=/app/data -e RAG_KB_PATH=/app/runtime/knowledge_base.json -e VECTOR_STORE_DIR=/app/runtime/vector_store -e HF_HOME=/app/runtime/huggingface -v /opt/billing/data:/app/data -v /opt/billing/runtime:/app/runtime agentic-ai-billing-agent:demo",
        "docker run -d --name billing-dashboard --restart unless-stopped --read-only --security-opt no-new-privileges --cap-drop ALL --tmpfs /tmp:rw,nosuid,nodev,size=512m -p 8501:8501 -e INVOICE_DATA_ROOT=/app/data -e RAG_KB_PATH=/app/runtime/knowledge_base.json -e VECTOR_STORE_DIR=/app/runtime/vector_store -e HF_HOME=/app/runtime/huggingface -v /opt/billing/data:/app/data -v /opt/billing/runtime:/app/runtime agentic-ai-billing-agent:demo python scripts/dashboard.py --host 0.0.0.0 --port 8501",
        "curl --fail --retry 12 --retry-delay 5 http://127.0.0.1:8000/health",
        "curl --fail --retry 12 --retry-delay 5 http://127.0.0.1:8501/ >/dev/null"
    )
    $request = @{
        DocumentName = "AWS-RunShellScript"
        InstanceIds = @($instanceId)
        Comment = "Deploy the Agentic Invoice Parser containers"
        TimeoutSeconds = 3600
        Parameters = @{ commands = $commands }
    }
    $request | ConvertTo-Json -Depth 6 | Set-Content -Path $requestFile -Encoding utf8
    $commandId = & aws ssm send-command --profile $Profile --region $Region --cli-input-json "file://$requestFile" --query "Command.CommandId" --output text --no-cli-pager
    if ($LASTEXITCODE -ne 0 -or -not $commandId) { throw "Could not start the remote deployment command." }

    Write-Host "Building and starting the containers on $instanceId..."
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
    Write-Host "API health: $($outputMap['ApiUrl'])/health"
    Write-Host "Dashboard:  $($outputMap['DashboardUrl'])"
    Write-Host "Auto-delete: $($outputMap['ExpiresAtUtc']) UTC"
    Write-Host "Delete early: .\deploy\aws\destroy.ps1 -Profile $Profile -Region $Region -StackName $StackName"
}
finally {
    Remove-Item -Path $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
