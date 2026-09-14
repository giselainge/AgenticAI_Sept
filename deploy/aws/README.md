# Billing assessment AWS deployment

This package deploys the invoice application for a short billing assessment without using the AWS console after login. It creates one CloudFormation stack containing a VPC, public subnet, restricted security group, Amazon Linux 2023 EC2 instance, encrypted EBS volume, private S3 deployment bucket, IAM roles, and an EventBridge Scheduler/Lambda cleanup path.

The instance is managed with Systems Manager; port 22 is never opened. Ports 7860, 8000, and 8501 are restricted to the public `/32` address detected by the deployment script. The cleanup schedule empties the deployment bucket and requests deletion of the complete stack after 1–12 hours. `destroy.ps1` provides an explicit early-deletion path.

## What is deployed

- `http://<temporary-host>:8000/ready`: the FastAPI readiness and OCR-profile report.
- `http://<temporary-host>:8501/`: the invoice import and review dashboard.
- `http://<temporary-host>:7860/`: the Gradio source-verified A/B lab.
- Three containers loaded from the exact locally tested hardened Docker image.
- A transactional SQLite provider-memory database at `/opt/billing/runtime/knowledge_base.sqlite3`,
  persisted on the encrypted EC2 volume only for the life of the stack.

The first Gradio session creates the database with a safe provider/category catalog and no invoice values.
Uploaded or reviewed invoices then populate that runtime database; private local invoice files are never bundled.

No Gemini key is uploaded. Enter a temporary key in the dashboard when running Gemini or Plan B. The package does not deploy `Qwen/Qwen3.5-9B`: the old `/v1` address was an OpenAI-compatible model service, while this repository currently calls Gemini directly and contains no Qwen client. Hosting a 9B model requires a separate GPU design, model licensing review, and cost controls.

## One-time AWS CLI setup

Install AWS CLI v2, then configure an IAM Identity Center profile:

```powershell
aws configure sso --profile billing-assessment
```

Enter the access-portal URL supplied for the account when prompted:

```text
https://<directory-id>.awsapps.com/start
```

AWS CLI configuration expects the URL without the browser fragment (`#/`). The SSO region, AWS account, and permission-set role cannot be inferred from the URL. Select the assigned values. The role needs permission to manage the CloudFormation resources declared in `ephemeral-stack.yaml` and to call SSM Run Command.

Authenticate before deployment:

```powershell
aws sso login --profile billing-assessment
aws sts get-caller-identity --profile billing-assessment
```

An SSO/browser or device-code login is unavoidable: code cannot obtain permission from the portal URL alone.

## Check the old endpoint

This reachability check needs no AWS credentials:

```powershell
.\deploy\aws\status.ps1 -OldEndpointOnly -OldModelBaseUrl "http://<old-host>:8000/v1"
```

A timeout only proves that the public model endpoint is unavailable. It cannot distinguish a stopped instance, changed IP/DNS name, security-group block, or stopped model server. Authenticated EC2 access is required for that diagnosis.

## Build and test the deployment image locally

From the project root:

```powershell
.\deploy\local.ps1
```

The command builds `agentic-ai-billing-agent:local`, starts all three services, verifies their local endpoints, and prints the image ID and OCR quality fingerprint. Test OCR + rules and the Gemini-enabled cases in Gradio before continuing. Stop the containers with `.\deploy\local.ps1 -Down` when desired; the image remains available for the AWS transfer.

## Deploy the tested image for three hours

From the project root:

```powershell
.\deploy\aws\deploy.ps1 -Profile billing-assessment -Region eu-west-1 -Hours 3
```

The script:

1. verifies the authenticated identity;
2. detects your public IP and refuses `0.0.0.0/0`;
3. creates the self-deleting CloudFormation stack;
4. runs a strict OCR dependency check in the already tested local image;
5. exports that exact image, computes its SHA-256 digest, and uploads it to the private bucket without invoice data or secrets;
6. verifies the digest, loads the image, and starts the API, dashboard, and Gradio containers through SSM Run Command;
7. verifies the same OCR quality fingerprint on EC2; and
8. prints the temporary URLs and UTC deletion time.

Use `-AllowedCidr '203.0.113.10/32'` if automatic IP detection is unavailable. If your public IP changes, rerun `deploy.ps1` with the new `/32`; CloudFormation updates the security group and resets the requested expiration time.

## Check status

```powershell
.\deploy\aws\status.ps1 -Profile billing-assessment -Region eu-west-1
```

## Delete early and verify deletion

```powershell
.\deploy\aws\destroy.ps1 -Profile billing-assessment -Region eu-west-1
```

The command empties the temporary bucket, deletes the stack, waits for `stack-delete-complete`, and fails visibly if CloudFormation cannot finish. Check status afterward. CloudFormation cannot delete resources created manually outside this stack.
