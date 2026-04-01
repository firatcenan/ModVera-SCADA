[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$StageDir = "release_bundle"
)

$ErrorActionPreference = "Stop"

$python = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

if ($PSCmdlet.ShouldProcess("Modvera workspace", "Run release checks")) {
    Write-Host "[INFO] Release checks calistiriliyor..." -ForegroundColor Cyan
    & $python "run_release_checks.py"
}

if ($PSCmdlet.ShouldProcess("Release bundle", "Build EXE bundle into '$StageDir'")) {
    Write-Host "[INFO] EXE bundle olusturuluyor..." -ForegroundColor Cyan
    & $python "build_exe.py" "--stage-dir" $StageDir "--no-root-copy"
}
