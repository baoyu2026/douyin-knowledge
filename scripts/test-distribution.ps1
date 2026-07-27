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
    $EnvironmentGateway = Join-Path $EnvironmentRoot "Scripts\douyin-knowledge-mcp.exe"
    & $EnvironmentPython -m pip install $WheelPath
    if ($LASTEXITCODE -ne 0) { throw "Failed to install wheel and dependencies." }

    $ShareRoot = Join-Path $EnvironmentRoot "share\douyin-knowledge"
    $Required = @(
        (Join-Path $ShareRoot "scripts\douyin-knowledge.ps1"),
        (Join-Path $ShareRoot "scripts\douyin-knowledge-mcp.ps1"),
        (Join-Path $ShareRoot "scripts\build-workbuddy-bundle.py"),
        (Join-Path $ShareRoot "scripts\export-workbuddy-bundle.ps1"),
        (Join-Path $ShareRoot "scripts\install-skill.ps1"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\SKILL.md"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\agents\openai.yaml"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\references\host-adapters.md"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\scripts\invoke.ps1"),
        (Join-Path $ShareRoot "skills\douyin-knowledge\scripts\invoke-mcp.ps1"),
        (Join-Path $ShareRoot "skills\workbuddy\douyin-knowledge\SKILL.md"),
        (Join-Path $ShareRoot (
            "skills\workbuddy\douyin-knowledge\references\gateway-workflow.md"
        ))
    )
    foreach ($Path in $Required) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Wheel is missing required portable artifact: $Path"
        }
    }

    $Installer = Join-Path $ShareRoot "scripts\install-skill.ps1"
    & $Installer -Destination $SkillDestination -InstanceRoot $InstanceRoot `
        -CliPath $EnvironmentCli -GatewayPath $EnvironmentGateway
    if ($LASTEXITCODE -ne 0) { throw "Wheel Skill installation failed." }
    $RuntimePath = Join-Path $SkillDestination "runtime.local.json"
    $Runtime = Get-Content -Raw -Encoding UTF8 -LiteralPath $RuntimePath | ConvertFrom-Json
    if ([string]$Runtime.instance_root -ne [System.IO.Path]::GetFullPath($InstanceRoot)) {
        throw "Installed Skill did not preserve the Unicode instance root."
    }
    if ([string]$Runtime.cli_path -ne [System.IO.Path]::GetFullPath($EnvironmentCli)) {
        throw "Installed Skill did not bind the wheel CLI."
    }
    if ([string]$Runtime.gateway_path -ne [System.IO.Path]::GetFullPath($EnvironmentGateway)) {
        throw "Installed Skill did not bind the wheel MCP gateway."
    }
    $Repeated = & $Installer -Destination $SkillDestination -InstanceRoot $InstanceRoot `
        -CliPath $EnvironmentCli -GatewayPath $EnvironmentGateway | ConvertFrom-Json
    if (-not $Repeated.reused) { throw "Identical Skill reinstall was not idempotent." }
    $Forced = & $Installer -Destination $SkillDestination -CliPath $EnvironmentCli `
        -GatewayPath $EnvironmentGateway -Force |
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
    & $EnvironmentGateway --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Installed MCP gateway entry point is unavailable." }
    $WorkBuddyOutput = Join-Path $TemporaryRoot "WorkBuddy Upload"
    & (Join-Path $ShareRoot "scripts\export-workbuddy-bundle.ps1") `
        -OutputDirectory $WorkBuddyOutput `
        -Python $EnvironmentPython
    if ($LASTEXITCODE -ne 0) { throw "WorkBuddy upload bundle export failed." }
    $WorkBuddySkill = Join-Path $WorkBuddyOutput "douyin-knowledge.zip"
    $WorkBuddyMcp = Join-Path $WorkBuddyOutput "douyin-knowledge.mcp.json"
    if (-not (Test-Path -LiteralPath $WorkBuddySkill -PathType Leaf) -or
        -not (Test-Path -LiteralPath $WorkBuddyMcp -PathType Leaf)) {
        throw "WorkBuddy upload bundle is incomplete."
    }
    $WorkBuddyConfigText = Get-Content -Raw -Encoding UTF8 -LiteralPath $WorkBuddyMcp
    $WorkBuddyConfig = $WorkBuddyConfigText | ConvertFrom-Json
    $WorkBuddyServer = $WorkBuddyConfig.mcpServers.'douyin-knowledge'
    if ($WorkBuddyServer.type -ne "stdio" -or
        $WorkBuddyServer.args[-1] -ne (Join-Path $ShareRoot "scripts\douyin-knowledge-mcp.ps1")) {
        throw "WorkBuddy MCP export did not bind the installed launcher."
    }
    if ($WorkBuddyConfigText.Contains($InstanceRoot)) {
        throw "WorkBuddy MCP export leaked the private instance path."
    }
    $env:DK_DISTRIBUTION_INSTANCE = $InstanceRoot
    $env:DK_DISTRIBUTION_GATEWAY = Join-Path $TemporaryRoot "agent gateway"
    try {
        & $EnvironmentPython -c @"
import asyncio
import os
from pathlib import Path
from douyin_knowledge.agent_gateway import AgentGateway
from douyin_knowledge.mcp_server import create_server
gateway = AgentGateway(
    Path(os.environ['DK_DISTRIBUTION_INSTANCE']),
    Path(os.environ['DK_DISTRIBUTION_GATEWAY']),
)
tools = asyncio.run(create_server(gateway).list_tools())
assert 'douyin_capabilities' in {tool.name for tool in tools}
"@
        if ($LASTEXITCODE -ne 0) { throw "Installed MCP gateway smoke test failed." }
    }
    finally {
        Remove-Item Env:DK_DISTRIBUTION_INSTANCE -ErrorAction SilentlyContinue
        Remove-Item Env:DK_DISTRIBUTION_GATEWAY -ErrorAction SilentlyContinue
    }
    Write-Output "distribution portability smoke test passed"
}
finally {
    if (Test-Path -LiteralPath $TemporaryRoot) {
        Remove-Item -LiteralPath $TemporaryRoot -Recurse -Force
    }
}
