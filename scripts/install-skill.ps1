[CmdletBinding()]
param(
    [string]$Destination = "",
    [string]$InstanceRoot = "",
    [string]$CliPath = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $Repository "skills\douyin-knowledge"
$Launcher = Join-Path $PSScriptRoot "douyin-knowledge.ps1"
if (-not (Test-Path -LiteralPath (Join-Path $Source "SKILL.md") -PathType Leaf)) {
    throw "The bundled douyin-knowledge Skill is missing."
}
if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "The bundled douyin-knowledge launcher is missing."
}

if (-not $CliPath) {
    $SourceCli = Join-Path $Repository ".venv\Scripts\douyin-knowledge.exe"
    $ShareDirectory = Split-Path -Parent $Repository
    $WheelPrefix = if ((Split-Path -Leaf $ShareDirectory) -eq "share") {
        Split-Path -Parent $ShareDirectory
    }
    $WheelCli = if ($WheelPrefix) { Join-Path $WheelPrefix "Scripts\douyin-knowledge.exe" }
    if (Test-Path -LiteralPath $SourceCli -PathType Leaf) {
        $CliPath = $SourceCli
    }
    elseif ($WheelCli -and (Test-Path -LiteralPath $WheelCli -PathType Leaf)) {
        $CliPath = $WheelCli
    }
}
if (-not $CliPath -or -not (Test-Path -LiteralPath $CliPath -PathType Leaf)) {
    throw "The CLI entry point is unavailable. Install the package or run bootstrap first."
}
$CliPath = [System.IO.Path]::GetFullPath($CliPath)

if (-not $Destination) {
    $Destination = Join-Path $HOME ".codex\skills\douyin-knowledge"
}
$Destination = [System.IO.Path]::GetFullPath($Destination)
if ($Destination -eq [System.IO.Path]::GetFullPath($Source)) {
    throw "Skill destination must differ from the bundled source directory."
}

if (-not $InstanceRoot) {
    $ExistingRuntimePath = Join-Path $Destination "runtime.local.json"
    if (Test-Path -LiteralPath $ExistingRuntimePath -PathType Leaf) {
        try {
            $ExistingRuntime = Get-Content -Raw -Encoding UTF8 -LiteralPath $ExistingRuntimePath |
                ConvertFrom-Json
            if ($ExistingRuntime.schema_version -ne 1 -or -not $ExistingRuntime.instance_root) {
                throw "unsupported binding"
            }
            $InstanceRoot = [string]$ExistingRuntime.instance_root
        }
        catch {
            throw "The existing Skill runtime binding is invalid; pass -InstanceRoot explicitly."
        }
    }
}
if (-not $InstanceRoot) {
    $Binding = Join-Path $Repository ".venv\instance-root.txt"
    if (Test-Path -LiteralPath $Binding -PathType Leaf) {
        $InstanceRoot = (Get-Content -Raw -Encoding UTF8 -LiteralPath $Binding).Trim()
    }
}
if (-not $InstanceRoot) { $InstanceRoot = $env:DOUYIN_KNOWLEDGE_ROOT }
if (-not $InstanceRoot) {
    if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is unavailable; pass -InstanceRoot." }
    $InstanceRoot = Join-Path $env:LOCALAPPDATA "douyin-knowledge"
}
$InstanceRoot = [System.IO.Path]::GetFullPath($InstanceRoot)

function Test-SameSkillBundle {
    param([string]$Left, [string]$Right)
    $Files = Get-ChildItem -LiteralPath $Left -File -Recurse
    foreach ($File in $Files) {
        $Relative = $File.FullName.Substring($Left.Length).TrimStart('\', '/')
        $Other = Join-Path $Right $Relative
        if (-not (Test-Path -LiteralPath $Other -PathType Leaf)) { return $false }
        if ((Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $Other -Algorithm SHA256).Hash) { return $false }
    }
    return $true
}

$Reused = $false
if (Test-Path -LiteralPath $Destination) {
    if (-not $Force -and (Test-SameSkillBundle -Left $Source -Right $Destination)) {
        $Reused = $true
    }
    elseif (-not $Force) {
        throw "Skill destination differs from this release. Pass -Force to replace it."
    }
    else {
        $Parent = Split-Path -Parent $Destination
        $Backup = Join-Path $Parent (
            "douyin-knowledge.backup." + (Get-Date -Format "yyyyMMddHHmmssfff")
        )
        Move-Item -LiteralPath $Destination -Destination $Backup
    }
}
if (-not $Reused) {
    $DestinationParent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $DestinationParent | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse
}

$Runtime = [ordered]@{
    schema_version = 1
    launcher = [System.IO.Path]::GetFullPath($Launcher)
    cli_path = $CliPath
    instance_root = $InstanceRoot
}
$RuntimePath = Join-Path $Destination "runtime.local.json"
$RuntimeTemporary = "$RuntimePath.tmp"
[System.IO.File]::WriteAllText(
    $RuntimeTemporary,
    (($Runtime | ConvertTo-Json -Depth 2) + [Environment]::NewLine),
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $RuntimeTemporary -Destination $RuntimePath -Force

[ordered]@{
    installed = $true
    reused = $Reused
} | ConvertTo-Json -Compress
