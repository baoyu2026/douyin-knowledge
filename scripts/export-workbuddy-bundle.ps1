[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$OutputDirectory = "",
    [string]$Python = "",
    [string]$SkillSource = "",
    [string]$Launcher = ""
)

$ErrorActionPreference = "Stop"
$DistributionRoot = Split-Path -Parent $PSScriptRoot
$Builder = Join-Path $PSScriptRoot "build-workbuddy-bundle.py"

if (-not $SkillSource) {
    $SkillSource = Join-Path $DistributionRoot "skills\workbuddy\douyin-knowledge"
}
if (-not $Launcher) {
    $Launcher = Join-Path $PSScriptRoot "douyin-knowledge-mcp.ps1"
}
if (-not $OutputDirectory) {
    $Downloads = Join-Path ([Environment]::GetFolderPath("UserProfile")) "Downloads"
    $OutputDirectory = Join-Path $Downloads "douyin-knowledge-workbuddy"
}

if (-not $Python) {
    $Candidates = @(
        (Join-Path $DistributionRoot ".venv\Scripts\python.exe")
    )
    $ShareDirectory = Split-Path -Parent $DistributionRoot
    if ((Split-Path -Leaf $ShareDirectory) -eq "share") {
        $Prefix = Split-Path -Parent $ShareDirectory
        $Candidates += Join-Path $Prefix "Scripts\python.exe"
        $Candidates += Join-Path $Prefix "python.exe"
    }
    $Python = $Candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if (-not $Python) {
        $Installed = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
        if ($Installed) { $Python = $Installed.Source }
    }
}

foreach ($RequiredFile in @($Python, $Builder, $Launcher, (Join-Path $SkillSource "SKILL.md"))) {
    if (-not $RequiredFile -or -not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "WorkBuddy bundle input is unavailable. Install the package or run bootstrap first."
    }
}

& $Python $Builder `
    --skill-source ([System.IO.Path]::GetFullPath($SkillSource)) `
    --launcher ([System.IO.Path]::GetFullPath($Launcher)) `
    --output ([System.IO.Path]::GetFullPath($OutputDirectory))
exit $LASTEXITCODE
