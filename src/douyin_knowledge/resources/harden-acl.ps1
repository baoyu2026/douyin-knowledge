param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string[]]$Paths = @("config", "data", "output", "logs")
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

$ResolvedRoot = (Resolve-Path -LiteralPath $Root).Path
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Resolve-SafeProjectPath {
    param(
        [Parameter(Mandatory = $true)][string]$Relative
    )
    if ([System.IO.Path]::IsPathRooted($Relative)) {
        throw "Paths must be relative to project root: $Relative"
    }
    $Segments = $Relative -split '[\\/]+'
    if ($Segments -contains '..' -or $Segments -contains '') {
        throw "Paths must stay inside project root: $Relative"
    }
    $FullPath = [System.IO.Path]::GetFullPath((Join-Path $ResolvedRoot $Relative))
    $RootWithSeparator = $ResolvedRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $FullPath.StartsWith($RootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved path escapes project root: $Relative"
    }
    $Current = $ResolvedRoot
    foreach ($Segment in $Segments) {
        $Current = Join-Path $Current $Segment
        if (Test-Path -LiteralPath $Current) {
            $Item = Get-Item -LiteralPath $Current -Force
            if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Sensitive path must not be a reparse point: $Current"
            }
        }
    }
    return $FullPath
}

foreach ($Relative in $Paths) {
    $Target = Resolve-SafeProjectPath $Relative
    if (-not (Test-Path -LiteralPath $Target)) {
        New-Item -ItemType Directory -Path $Target | Out-Null
    }
    Invoke-Checked icacls $Target "/inheritance:r"
    Invoke-Checked icacls $Target "/remove:g" "Everyone" "BUILTIN\Users" "NT AUTHORITY\Authenticated Users" "Authenticated Users"
    Invoke-Checked icacls $Target "/grant:r" "${CurrentUser}:(OI)(CI)(F)" "SYSTEM:(OI)(CI)(F)" "BUILTIN\Administrators:(OI)(CI)(F)"
}

$Cookie = Resolve-SafeProjectPath "config\cookies.json"
if (Test-Path -LiteralPath $Cookie) {
    Invoke-Checked icacls $Cookie "/inheritance:r"
    Invoke-Checked icacls $Cookie "/remove:g" "Everyone" "BUILTIN\Users" "NT AUTHORITY\Authenticated Users" "Authenticated Users"
    Invoke-Checked icacls $Cookie "/grant:r" "${CurrentUser}:(F)" "SYSTEM:(F)" "BUILTIN\Administrators:(F)"
}

& (Join-Path $ResolvedRoot ".venv\Scripts\python.exe") -m app.security metadata --root $ResolvedRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
