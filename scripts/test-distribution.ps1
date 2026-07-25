[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WheelPath,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$WheelPath = (Resolve-Path -LiteralPath $WheelPath).Path
$TemporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "douyin-knowledge-wheel-" + [guid]::NewGuid().ToString("N")
)
$EnvironmentRoot = Join-Path $TemporaryRoot "clean environment"
$SkillDestination = Join-Path $TemporaryRoot "Codex Skills\douyin-knowledge"
$InstanceRoot = Join-Path $TemporaryRoot "实例 root"

try {
    New-Item -ItemType Directory -Path $TemporaryRoot | Out-Null
    & $Python -m venv $EnvironmentRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to create clean wheel environment." }
    $EnvironmentPython = Join-Path $EnvironmentRoot "Scripts\python.exe"
    $EnvironmentCli = Join-Path $EnvironmentRoot "Scripts\douyin-knowledge.exe"
    & $EnvironmentPython -m pip install $WheelPath
    if ($LASTEXITCODE -ne 0) { throw "Failed to install wheel and dependencies." }

    $ShareRoot = Join-Path $EnvironmentRoot "share\douyin-knowledge"
    $Required = @(
        (Join-Path $ShareRoot "scripts\douyin-knowledge.ps1"),
        (Join-Path $ShareRoot "scripts\install-skill.ps1"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\SKILL.md"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\agents\openai.yaml"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\references\host-adapters.md"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\scripts\invoke.ps1")
    )
    foreach ($Path in $Required) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Wheel is missing required portable artifact: $Path"
        }
    }

    $Installer = Join-Path $ShareRoot "scripts\install-skill.ps1"
    & $Installer -Destination $SkillDestination -InstanceRoot $InstanceRoot `
        -CliPath $EnvironmentCli
    if ($LASTEXITCODE -ne 0) { throw "Wheel Skill installation failed." }
    $RuntimePath = Join-Path $SkillDestination "runtime.local.json"
    $Runtime = Get-Content -Raw -Encoding UTF8 -LiteralPath $RuntimePath | ConvertFrom-Json
    if ([string]$Runtime.instance_root -ne [System.IO.Path]::GetFullPath($InstanceRoot)) {
        throw "Installed Skill did not preserve the Unicode instance root."
    }
    if ([string]$Runtime.cli_path -ne [System.IO.Path]::GetFullPath($EnvironmentCli)) {
        throw "Installed Skill did not bind the wheel CLI."
    }
    $Repeated = & $Installer -Destination $SkillDestination -InstanceRoot $InstanceRoot `
        -CliPath $EnvironmentCli | ConvertFrom-Json
    if (-not $Repeated.reused) { throw "Identical Skill reinstall was not idempotent." }
    $Forced = & $Installer -Destination $SkillDestination -CliPath $EnvironmentCli -Force |
        ConvertFrom-Json
    if (-not $Forced.installed -or $Forced.reused) {
        throw "Forced Skill upgrade did not replace the bundle."
    }
    $Runtime = Get-Content -Raw -Encoding UTF8 -LiteralPath $RuntimePath | ConvertFrom-Json
    if ([string]$Runtime.instance_root -ne [System.IO.Path]::GetFullPath($InstanceRoot)) {
        throw "Forced Skill upgrade did not preserve the existing instance binding."
    }
    $Status = & (Join-Path $SkillDestination "scripts\invoke.ps1") status --json |
        ConvertFrom-Json
    if (-not $Status.ok -or $Status.operation -ne "status") {
        throw "Installed Skill adapter did not execute the wheel CLI."
    }
    Write-Output "distribution portability smoke test passed"
}
finally {
    if (Test-Path -LiteralPath $TemporaryRoot) {
        Remove-Item -LiteralPath $TemporaryRoot -Recurse -Force
    }
}
