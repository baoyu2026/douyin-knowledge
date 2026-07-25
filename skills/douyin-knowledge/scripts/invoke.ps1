[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$CliArguments = @()
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
$RuntimePath = Join-Path $SkillRoot "runtime.local.json"
$Launcher = ""
$CliPath = ""
$InstanceRoot = ""

if (Test-Path -LiteralPath $RuntimePath -PathType Leaf) {
    $Runtime = Get-Content -Raw -Encoding UTF8 -LiteralPath $RuntimePath | ConvertFrom-Json
    if ($Runtime.schema_version -ne 1) { throw "Unsupported Skill runtime binding." }
    $Launcher = [string]$Runtime.launcher
    $CliPath = [string]$Runtime.cli_path
    $InstanceRoot = [string]$Runtime.instance_root
}
else {
    $Repository = Split-Path -Parent (Split-Path -Parent $SkillRoot)
    $Launcher = Join-Path $Repository "scripts\douyin-knowledge.ps1"
}

if (-not $Launcher -or -not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "The Skill runtime is not bound. Re-run scripts/install-skill.ps1."
}

$Parameters = @{}
if ($CliPath) { $Parameters["CliPath"] = $CliPath }
if ($InstanceRoot) { $Parameters["InstanceRoot"] = $InstanceRoot }
& $Launcher @Parameters @CliArguments
exit $LASTEXITCODE
