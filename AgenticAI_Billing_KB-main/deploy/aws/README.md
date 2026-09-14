# Temporary AWS deployment

This package deploys the invoice application for a short assessment demo without using the AWS console after login. It creates one CloudFormation stack containing a VPC, public subnet, restricted security group, Amazon Linux 2023 EC2 instance, encrypted EBS volume, private S3 deployment bucket, IAM roles, and an EventBridge Scheduler/Lambda cleanup path.

The instance is managed with Systems Manager; port 22 is never opened. Ports 8000 and 8501 are restricted to the public `/32` address detected by the deployment script. The cleanup schedule empties the deployment bucket and requests deletion of the complete stack after 1–12 hours. `destroy.ps1` provides an explicit early-deletion path.

## What is deployed

- `http://<temporary-host>:8000/health`: the lightweight FastAPI health service.
- `http://<temporary-host>:8501/`: the invoice import and review dashboard.
- Two containers built from the same hardened Docker image.
- Persistent application data only for the life of the EC2/EBS stack.

No Gemini key is uploaded. Enter a temporary key in the dashboard when running Gemini or Plan B. The package does not deploy `Qwen/Qwen3.5-9B`: the old `/v1` address was an OpenAI-compatible model service, while this repository currently calls Gemini directly and contains no Qwen client. Hosting a 9B model requires a separate GPU design, model licensing review, and cost controls.

## One-time AWS CLI setup

Install AWS CLI v2, then configure an IAM Identity Center profile:

```powershell
aws configure sso --profile iseg
```

Use the start URL supplied for the account:

```text
https://d-90661657fb.awsapps.com/start/#/
```

AWS CLI configuration expects the start URL without the fragment (`#/`):

```text
https://d-90661657fb.awsapps.com/start
```

The SSO region, AWS account, and permission-set role cannot be inferred from the start URL. Select the values assigned by ISEG. The role needs permission to manage the CloudFormation resources declared in `ephemeral-stack.yaml` and to call SSM Run Command.

Authenticate before deployment:

```powershell
aws sso login --profile iseg
aws sts get-caller-identity --profile iseg
```

An SSO/browser or device-code login is unavoidable: code cannot obtain permission from the portal URL alone.

## Check the old endpoint

This reachability check needs no AWS credentials:

```powershell
.\deploy\aws\status.ps1 -OldEndpointOnly
```

A timeout only proves that the public model endpoint is unavailable. It cannot distinguish a stopped instance, changed IP/DNS name, security-group block, or stopped model server. Authenticated EC2 access is required for that diagnosis.

## Deploy for three hours

From the project root:

```powershell
.\deploy\aws\deploy.ps1 -Profile iseg -Region eu-west-1 -Hours 3
```

The script:

1. verifies the authenticated identity;
2. detects your public IP and refuses `0.0.0.0/0`;
3. creates the self-deleting CloudFormation stack;
4. packages only application code, excluding `.env`, Git metadata, tests, and invoice data;
5. uploads the bundle to the private bucket;
6. builds and starts the API and dashboard through SSM Run Command; and
7. prints the temporary URLs and UTC deletion time.

Use `-AllowedCidr '203.0.113.10/32'` if automatic IP detection is unavailable. If your public IP changes, rerun `deploy.ps1` with the new `/32`; CloudFormation updates the security group and resets the requested expiration time.

## Check status

```powershell
.\deploy\aws\status.ps1 -Profile iseg -Region eu-west-1
```

## Delete early and verify deletion

```powershell
.\deploy\aws\destroy.ps1 -Profile iseg -Region eu-west-1
```

The command empties the temporary bucket, deletes the stack, waits for `stack-delete-complete`, and fails visibly if CloudFormation cannot finish. Check status afterward. CloudFormation cannot delete resources created manually outside this stack.

## Publish the nested project to the ISEG repository

The current Git root is one directory above this project, and `origin` points to `kugguk2022/AgenticAI_Billing_KB-main`. After reviewing and committing all changes, publish only this nested project as the root of an existing empty ISEG repository:

```powershell
.\deploy\publish_to_iseg.ps1 -TargetRemote "https://github.com/<github-owner>/<repository>.git"
```

The script refuses a dirty repository, does not force-push, and keeps the old remote. Verify the new repository before archiving the old one. A target URL and GitHub authorization are required; the word “ISEG” alone is not enough to identify a destination repository.
