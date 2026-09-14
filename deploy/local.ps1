[CmdletBinding()]
param(
    [switch]$NoBuild,
    [switch]$Down
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop with Docker Compose is required for the parity test."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoRoot
try {
    if ($Down) {
        & docker compose down
        if ($LASTEXITCODE -ne 0) { throw "Docker Compose could not stop the local services." }
        Write-Host "Local invoice services stopped."
        exit 0
    }

    New-Item -ItemType Directory -Path "data", "runtime" -Force | Out-Null
    if (-not $NoBuild) {
        & docker compose build --pull
        if ($LASTEXITCODE -ne 0) { throw "The local parity image build failed." }
    }
    & docker compose up --detach --remove-orphans
    if ($LASTEXITCODE -ne 0) { throw "The local parity services did not start." }

    $endpoints = @(
        @{ Name = "API readiness"; Uri = "http://127.0.0.1:8000/ready" },
        @{ Name = "Dashboard"; Uri = "http://127.0.0.1:8501/" },
        @{ Name = "Gradio lab"; Uri = "http://127.0.0.1:7860/" }
    )
    foreach ($endpoint in $endpoints) {
        $ready = $false
        for ($attempt = 1; $attempt -le 30; $attempt++) {
            try {
                $response = Invoke-WebRequest -UseBasicParsing -Uri $endpoint.Uri -TimeoutSec 5
                if ($response.StatusCode -eq 200) {
                    $ready = $true
                    break
                }
            }
            catch {
                Start-Sleep -Seconds 2
            }
        }
        if (-not $ready) { throw "$($endpoint.Name) did not become ready at $($endpoint.Uri)." }
    }

    $fingerprint = [string](& docker exec billing-api python scripts/runtime_check.py --strict --fingerprint-only)
    if ($LASTEXITCODE -ne 0) { throw "The container OCR runtime check failed." }
    $imageId = [string](& docker image inspect --format "{{.Id}}" agentic-ai-billing-agent:local)
    if ($LASTEXITCODE -ne 0) { throw "Could not identify the locally tested image." }

    Write-Host "Local parity environment is ready."
    Write-Host "Image:       $($imageId.Trim())"
    Write-Host "OCR profile: $($fingerprint.Trim())"
    Write-Host "API ready:   http://127.0.0.1:8000/ready"
    Write-Host "Dashboard:   http://127.0.0.1:8501/"
    Write-Host "Gradio lab:  http://127.0.0.1:7860/"
    Write-Host "Use '.\deploy\local.ps1 -Down' to stop the services."
}
finally {
    Pop-Location
}
