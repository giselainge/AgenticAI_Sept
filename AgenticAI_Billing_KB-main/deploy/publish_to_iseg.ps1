[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TargetRemote,
    [string]$Branch = "main",
    [string]$RemoteName = "iseg"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required."
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$gitRoot = (& git -C $projectRoot rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0) { throw "The project is not inside a Git repository." }
$status = & git -C $gitRoot status --porcelain
if ($status) {
    throw "The source repository has uncommitted changes. Review and commit them before publishing to ISEG."
}

$relativePrefix = [System.IO.Path]::GetRelativePath($gitRoot, $projectRoot).Replace("\", "/")
if ($relativePrefix -eq ".") {
    throw "This helper is intended for the current nested-repository layout. The project is already at the Git root."
}

& git -C $gitRoot remote get-url $RemoteName 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    & git -C $gitRoot remote set-url $RemoteName $TargetRemote
}
else {
    & git -C $gitRoot remote add $RemoteName $TargetRemote
}
if ($LASTEXITCODE -ne 0) { throw "Could not configure the '$RemoteName' Git remote." }

$temporaryBranch = "iseg-publish-$PID"
try {
    & git -C $gitRoot subtree split --prefix $relativePrefix --branch $temporaryBranch
    if ($LASTEXITCODE -ne 0) { throw "Could not extract the nested project history." }
    & git -C $gitRoot push $RemoteName "${temporaryBranch}:$Branch"
    if ($LASTEXITCODE -ne 0) {
        throw "Push failed. Verify the ISEG repository URL, your access, and that its target branch is empty or compatible."
    }
    Write-Host "Published '$relativePrefix' as the root of $TargetRemote on branch '$Branch'."
    Write-Host "Verify the new repository before archiving or deleting the old repository."
}
finally {
    & git -C $gitRoot branch --delete --force $temporaryBranch 2>$null | Out-Null
}
