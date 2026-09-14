[CmdletBinding()]
param(
    [string]$Profile = "iseg",
    [string]$Region = "eu-west-1",
    [string]$StackName = "iseg-agentic-invoice-demo",
    [string]$OldModelBaseUrl = "http://ec2-108-132-55-75.eu-west-1.compute.amazonaws.com:8000/v1",
    [switch]$OldEndpointOnly
)

$ErrorActionPreference = "Continue"
Set-StrictMode -Version Latest

Write-Host "Checking old Qwen-compatible endpoint without AWS credentials..."
try {
    $response = Invoke-RestMethod -Uri "$($OldModelBaseUrl.TrimEnd('/'))/models" -TimeoutSec 8
    Write-Host "Old endpoint is reachable."
    $response | ConvertTo-Json -Depth 5
}
catch {
    Write-Host "Old endpoint is not reachable: $($_.Exception.Message)"
}

if ($OldEndpointOnly) { exit 0 }
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Host "AWS CLI v2 is not installed; cloud resource status cannot be checked."
    exit 1
}

& aws sts get-caller-identity --profile $Profile --region $Region --no-cli-pager | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "AWS profile '$Profile' is not authenticated. Run 'aws sso login --profile $Profile'."
    exit 1
}

$stackJson = & aws cloudformation describe-stacks --profile $Profile --region $Region --stack-name $StackName --query "Stacks[0].{Status:StackStatus,Outputs:Outputs}" --output json --no-cli-pager 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Temporary stack '$StackName' is absent (or this role cannot describe it)."
    exit 0
}

$stack = $stackJson | ConvertFrom-Json
Write-Host "Stack status: $($stack.Status)"
$outputMap = @{}
foreach ($output in $stack.Outputs) { $outputMap[$output.OutputKey] = $output.OutputValue }
Write-Host "API health: $($outputMap['ApiUrl'])/health"
Write-Host "Dashboard:  $($outputMap['DashboardUrl'])"
Write-Host "Auto-delete: $($outputMap['ExpiresAtUtc']) UTC"

$instanceId = $outputMap["InstanceId"]
$state = & aws ec2 describe-instances --profile $Profile --region $Region --instance-ids $instanceId --query "Reservations[0].Instances[0].State.Name" --output text --no-cli-pager
Write-Host "EC2 $instanceId state: $state"
