[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$InstanceRoot = "",
    [string]$GatewayPath = "",
    [string]$WorkspaceRoot = ""
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot

if (-not $GatewayPath) {
    $Candidates = @(
        (Join-Path $Repository ".venv\Scripts\douyin-knowledge-mcp.exe")
    )
    $ShareDirectory = Split-Path -Parent $Repository
    if ((Split-Path -Leaf $ShareDirectory) -eq "share") {
        $Prefix = Split-Path -Parent $ShareDirectory
        $Candidates += Join-Path $Prefix "Scripts\douyin-knowledge-mcp.exe"
    }
    $GatewayPath = $Candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if (-not $GatewayPath) {
        $Installed = Get-Command douyin-knowledge-mcp `
            -CommandType Application -ErrorAction SilentlyContinue
        if ($Installed) { $GatewayPath = $Installed.Source }
    }
}
if (-not $GatewayPath -or -not (Test-Path -LiteralPath $GatewayPath -PathType Leaf)) {
    throw "douyin-knowledge MCP gateway is unavailable. Run scripts/bootstrap.ps1 first."
}
$GatewayPath = [System.IO.Path]::GetFullPath($GatewayPath)

if (-not $InstanceRoot) {
    $Binding = Join-Path $Repository ".venv\instance-root.txt"
    if (Test-Path -LiteralPath $Binding -PathType Leaf) {
        $InstanceRoot = (Get-Content -Raw -Encoding UTF8 -LiteralPath $Binding).Trim()
    }
}
if (-not $InstanceRoot) { $InstanceRoot = $env:DOUYIN_KNOWLEDGE_ROOT }
if (-not $InstanceRoot) {
    if (-not $env:LOCALAPPDATA) {
        throw "Instance root is unavailable. Pass -InstanceRoot explicitly."
    }
    $InstanceRoot = Join-Path $env:LOCALAPPDATA "douyin-knowledge"
}
$InstanceRoot = [System.IO.Path]::GetFullPath($InstanceRoot)

$Arguments = @("--root", $InstanceRoot)
if ($WorkspaceRoot) {
    $Arguments += @("--workspace", [System.IO.Path]::GetFullPath($WorkspaceRoot))
}
& $GatewayPath @Arguments
exit $LASTEXITCODE
