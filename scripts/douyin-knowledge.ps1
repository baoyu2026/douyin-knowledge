[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$InstanceRoot = "",
    [string]$CliPath = "",
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$CliArguments = @()
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot

if (-not $CliPath) {
    $Candidates = @(
        (Join-Path $Repository ".venv\Scripts\douyin-knowledge.exe")
    )
    $ShareDirectory = Split-Path -Parent $Repository
    if ((Split-Path -Leaf $ShareDirectory) -eq "share") {
        $Prefix = Split-Path -Parent $ShareDirectory
        $Candidates += Join-Path $Prefix "Scripts\douyin-knowledge.exe"
    }
    $CliPath = $Candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if (-not $CliPath) {
        $Installed = Get-Command douyin-knowledge -CommandType Application -ErrorAction SilentlyContinue
        if ($Installed) { $CliPath = $Installed.Source }
    }
}
if (-not $CliPath -or -not (Test-Path -LiteralPath $CliPath -PathType Leaf)) {
    throw "douyin-knowledge CLI is unavailable. Run scripts/bootstrap.ps1 first."
}
$CliPath = [System.IO.Path]::GetFullPath($CliPath)

$RootProvided = @(
    $CliArguments | Where-Object { $_ -eq "--root" -or $_ -like "--root=*" }
).Count -gt 0
if (-not $RootProvided) {
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
    & $CliPath --root $InstanceRoot @CliArguments
}
else {
    & $CliPath @CliArguments
}
exit $LASTEXITCODE
