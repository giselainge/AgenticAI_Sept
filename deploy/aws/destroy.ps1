[CmdletBinding()]
param(
    [string]$Profile = "billing-demo",
    [string]$Region = "eu-west-1",
    [string]$StackName = "agentic-invoice-demo"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw "AWS CLI v2 is required."
}
& aws sts get-caller-identity --profile $Profile --region $Region --no-cli-pager | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "No active AWS SSO session. Run 'aws sso login --profile $Profile' and retry."
}

$bucket = & aws cloudformation describe-stacks --profile $Profile --region $Region --stack-name $StackName --query "Stacks[0].Outputs[?OutputKey=='DeploymentBucketName'].OutputValue | [0]" --output text --no-cli-pager 2>$null
if ($LASTEXITCODE -ne 0 -or -not $bucket -or $bucket -eq "None") {
    Write-Host "Stack '$StackName' does not exist or has already been deleted."
    exit 0
}

Write-Host "Emptying the temporary deployment bucket..."
& aws s3 rm "s3://$bucket" --recursive --profile $Profile --region $Region --only-show-errors
if ($LASTEXITCODE -ne 0) { throw "Could not empty deployment bucket '$bucket'." }

Write-Host "Deleting stack '$StackName' and all resources it owns..."
& aws cloudformation delete-stack --profile $Profile --region $Region --stack-name $StackName --no-cli-pager
if ($LASTEXITCODE -ne 0) { throw "CloudFormation did not accept the deletion request." }
& aws cloudformation wait stack-delete-complete --profile $Profile --region $Region --stack-name $StackName --no-cli-pager
if ($LASTEXITCODE -ne 0) { throw "Stack deletion did not complete successfully. Check CloudFormation events." }

Write-Host "Deleted '$StackName': EC2, EBS, network, S3 bucket, IAM roles, Lambda, logs, and schedule are removed."
