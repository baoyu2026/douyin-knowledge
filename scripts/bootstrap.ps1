param(
    [string]$InstanceRoot = "",
    [switch]$WithDev,
    [switch]$SkipBrowser,
    [switch]$InstallCodexSkill,
    [switch]$ForceSkill
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$VirtualEnvironment = Join-Path $Repository ".venv"
$Python = Join-Path $VirtualEnvironment "Scripts\python.exe"
$Cli = Join-Path $VirtualEnvironment "Scripts\douyin-knowledge.exe"
$InstanceBinding = Join-Path $VirtualEnvironment "instance-root.txt"

if (-not $InstanceRoot) {
    if (Test-Path -LiteralPath $InstanceBinding -PathType Leaf) {
        $InstanceRoot = (Get-Content -Raw -Encoding UTF8 -LiteralPath $InstanceBinding).Trim()
        if (-not $InstanceRoot) {
            throw "The existing instance binding is empty; pass -InstanceRoot explicitly."
        }
    }
    else {
        if (-not $env:LOCALAPPDATA) {
            throw "LOCALAPPDATA is unavailable; pass -InstanceRoot."
        }
        $InstanceRoot = Join-Path $env:LOCALAPPDATA "douyin-knowledge"
    }
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

$BindingTemporary = "$InstanceBinding.tmp"
[System.IO.File]::WriteAllText(
    $BindingTemporary,
    $InstanceRoot + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $BindingTemporary -Destination $InstanceBinding -Force

if (-not $SkipBrowser) {
    & $Python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Playwright Chromium." }
}

& $Cli --root $InstanceRoot init --json
if ($LASTEXITCODE -ne 0) { throw "Failed to initialize the private instance." }
if ($InstallCodexSkill) {
    $SkillParameters = @{ InstanceRoot = $InstanceRoot; CliPath = $Cli }
    if ($ForceSkill) { $SkillParameters["Force"] = $true }
    & (Join-Path $PSScriptRoot "install-skill.ps1") @SkillParameters
    if ($LASTEXITCODE -ne 0) { throw "Failed to install the Codex Skill." }
}
& $Cli --root $InstanceRoot doctor --json
exit $LASTEXITCODE
