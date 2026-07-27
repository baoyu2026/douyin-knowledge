[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$WorkspaceRoot = ""
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
$RuntimePath = Join-Path $SkillRoot "runtime.local.json"
$Launcher = ""
$GatewayPath = ""
$InstanceRoot = ""

if (Test-Path -LiteralPath $RuntimePath -PathType Leaf) {
    $Runtime = Get-Content -Raw -Encoding UTF8 -LiteralPath $RuntimePath | ConvertFrom-Json
    if ($Runtime.schema_version -ne 1) { throw "Unsupported Skill runtime binding." }
    $Launcher = [string]$Runtime.gateway_launcher
    $GatewayPath = [string]$Runtime.gateway_path
    $InstanceRoot = [string]$Runtime.instance_root
}
else {
    $Repository = Split-Path -Parent (Split-Path -Parent $SkillRoot)
    $Launcher = Join-Path $Repository "scripts\douyin-knowledge-mcp.ps1"
}

if (-not $Launcher -or -not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "The MCP gateway runtime is not bound. Re-run scripts/install-skill.ps1."
}

$Parameters = @{}
if ($GatewayPath) { $Parameters["GatewayPath"] = $GatewayPath }
if ($InstanceRoot) { $Parameters["InstanceRoot"] = $InstanceRoot }
if ($WorkspaceRoot) { $Parameters["WorkspaceRoot"] = $WorkspaceRoot }
& $Launcher @Parameters
exit $LASTEXITCODE
