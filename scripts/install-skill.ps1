param(
    [string]$Destination = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Repository = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $Repository "skills\douyin-knowledge"
if (-not (Test-Path -LiteralPath (Join-Path $Source "SKILL.md"))) {
    throw "The bundled douyin-knowledge Skill is missing."
}
if (-not $Destination) {
    $Destination = Join-Path $HOME ".codex\skills\douyin-knowledge"
}
$Destination = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Destination) {
    if (-not $Force) {
        throw "Skill destination already exists. Pass -Force to replace this Skill only."
    }
    $Parent = Split-Path -Parent $Destination
    $Backup = Join-Path $Parent ("douyin-knowledge.backup." + (Get-Date -Format "yyyyMMddHHmmss"))
    Move-Item -LiteralPath $Destination -Destination $Backup
}
$DestinationParent = Split-Path -Parent $Destination
New-Item -ItemType Directory -Force -Path $DestinationParent | Out-Null
Copy-Item -LiteralPath $Source -Destination $Destination -Recurse
Write-Output $Destination
