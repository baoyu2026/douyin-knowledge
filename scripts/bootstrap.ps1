param(
    [string]$InstanceRoot = "",
    [switch]$WithDev,
    [switch]$SkipBrowser,
    [switch]$InstallCodexSkill
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$VirtualEnvironment = Join-Path $Repository ".venv"
$Python = Join-Path $VirtualEnvironment "Scripts\python.exe"
$Cli = Join-Path $VirtualEnvironment "Scripts\douyin-knowledge.exe"

if (-not $InstanceRoot) {
    if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is unavailable; pass -InstanceRoot." }
    $InstanceRoot = Join-Path $env:LOCALAPPDATA "douyin-knowledge"
}
$InstanceRoot = [System.IO.Path]::GetFullPath($InstanceRoot)
if ($InstanceRoot -eq [System.IO.Path]::GetFullPath($Repository)) {
    throw "InstanceRoot must be separate from the source repository."
}

if (-not (Test-Path -LiteralPath $Python)) {
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($Launcher) {
        & $Launcher.Source -3.12 -m venv $VirtualEnvironment
        if ($LASTEXITCODE -ne 0) {
            & $Launcher.Source -3.11 -m venv $VirtualEnvironment
        }
    }
    else {
        & python -m venv $VirtualEnvironment
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment." }
}

& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
$InstallTarget = if ($WithDev) { ".[dev]" } else { "." }
Push-Location $Repository
try {
    & $Python -m pip install $InstallTarget
    if ($LASTEXITCODE -ne 0) { throw "Failed to install douyin-knowledge." }
}
finally {
    Pop-Location
}

if (-not $SkipBrowser) {
    & $Python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Playwright Chromium." }
}

& $Cli --root $InstanceRoot init --json
if ($LASTEXITCODE -ne 0) { throw "Failed to initialize the private instance." }
if ($InstallCodexSkill) {
    & (Join-Path $PSScriptRoot "install-skill.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Failed to install the Codex Skill." }
}
& $Cli --root $InstanceRoot doctor --json
exit $LASTEXITCODE
