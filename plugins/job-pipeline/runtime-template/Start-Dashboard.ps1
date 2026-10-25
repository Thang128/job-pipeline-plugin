[CmdletBinding()]
param(
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-SupportedPowerShellVersion {
    param([Parameter(Mandatory = $true)][Version]$Version)
    return $Version -ge [Version]'5.1'
}

function Get-SafeDiagnostic {
    param([string]$Message)
    $safe = [string]$Message
    if ($PSScriptRoot) {
        $safe = [regex]::Replace($safe, [regex]::Escape($PSScriptRoot), '<project>', 'IgnoreCase')
    }
    return [regex]::Replace($safe, '(?i)[a-z]:\\users\\[^\\\r\n]+', '<user>')
}

function Get-ProjectRoot {
    $item = Get-Item -LiteralPath $PSScriptRoot -Force
    $linkType = $item.PSObject.Properties['LinkType']
    if (-not $item.PSIsContainer -or $item.PSProvider.Name -ne 'FileSystem' -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($linkType -and $linkType.Value)) {
        throw 'Project root must be a plain local directory.'
    }
    return $item.FullName.TrimEnd([IO.Path]::DirectorySeparatorChar)
}

function Assert-ConfinedPath {
    param([string]$ProjectRoot, [string]$Path)
    $rootPrefix = $ProjectRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Project path escapes extracted folder.'
    }
    $relative = $full.Substring($rootPrefix.Length)
    $current = $ProjectRoot
    foreach ($part in $relative.Split([IO.Path]::DirectorySeparatorChar)) {
        if (-not $part) { continue }
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            $linkType = $item.PSObject.Properties['LinkType']
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($linkType -and $linkType.Value)) {
                throw 'Project path contains a link or reparse point.'
            }
        }
    }
    return $full
}

function Assert-PlainSingleLinkFile {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing. Run Install.ps1 first."
    }
    $item = Get-Item -LiteralPath $Path -Force
    $linkType = $item.PSObject.Properties['LinkType']
    if ($item.PSProvider.Name -ne 'FileSystem' -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($linkType -and $linkType.Value)) {
        throw "$Label must not be a link or reparse point."
    }
    # Query the open file itself; fsutil hardlink list is unsupported on some
    # Windows filesystems even when file-handle link metadata is available.
    if (-not ('JobPipelineFileSafety' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class JobPipelineFileSafety {
    [StructLayout(LayoutKind.Sequential)]
    private struct FileInfo {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);
    public static uint LinkCount(string path) {
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                                          FileShare.ReadWrite | FileShare.Delete)) {
            FileInfo info;
            if (!GetFileInformationByHandle(stream.SafeFileHandle, out info))
                throw new IOException("File metadata unavailable.");
            if ((info.Attributes & 0x400) != 0)
                throw new IOException("Reparse point rejected.");
            return info.Links;
        }
    }
}
'@
    }
    try {
        $linkCount = [JobPipelineFileSafety]::LinkCount($item.FullName)
    }
    catch {
        throw "$Label link safety could not be verified."
    }
    if ($linkCount -ne 1) {
        throw "$Label must have exactly one file link."
    }
    return $item.FullName
}

function Assert-ConfinedPlainFile {
    param([string]$ProjectRoot, [string]$Path, [string]$Label)
    $confined = Assert-ConfinedPath -ProjectRoot $ProjectRoot -Path $Path
    return Assert-PlainSingleLinkFile -Path $confined -Label $Label
}

function ConvertTo-SafeOutput {
    param([object]$Value, [string]$ProjectRoot)
    $line = [string]$Value
    $line = [regex]::Replace($line, [regex]::Escape($ProjectRoot), '<project>', 'IgnoreCase')
    return [regex]::Replace($line, '(?i)[a-z]:\\users\\[^\\\r\n]+', '<user>')
}

function Invoke-Dashboard {
    if (-not (Test-SupportedPowerShellVersion $PSVersionTable.PSVersion)) {
        throw 'PowerShell 5.1 or newer is required.'
    }
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw 'Port must be an integer from 1 through 65535.'
    }
    $projectRoot = Get-ProjectRoot
    $null = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path $PSCommandPath -Label 'Dashboard launcher'
    $null = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path (Join-Path $projectRoot 'Pipeline\__init__.py') -Label 'Pipeline package'
    $null = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path (Join-Path $projectRoot 'Pipeline\dashboard_server.py') -Label 'Dashboard server'
    $venvPath = Assert-ConfinedPath -ProjectRoot $projectRoot -Path (Join-Path $projectRoot '.venv')
    if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
        throw 'Project virtual environment is missing. Run Install.ps1 first.'
    }
    $venvPython = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path (Join-Path $venvPath 'Scripts\python.exe') -Label 'Project virtual-environment interpreter'
    $arguments = @('-B', '-u', '-E', '-s', '-m', 'Pipeline.dashboard_server', '--root', $projectRoot, '--port', ([string]$Port), '--open')

    $environmentNames = @(
        'BROWSER', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT',
        'PYTHONBREAKPOINT', 'PYTEST_ADDOPTS'
    )
    $oldEnvironment = @{}
    foreach ($name in $environmentNames) {
        $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    Push-Location -LiteralPath $projectRoot
    try {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $venvPython @arguments 2>&1 | ForEach-Object {
                [Console]::Out.WriteLine((ConvertTo-SafeOutput -Value $_ -ProjectRoot $projectRoot))
            }
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
    }
    finally {
        Pop-Location
        foreach ($name in $environmentNames) {
            [Environment]::SetEnvironmentVariable($name, $oldEnvironment[$name], 'Process')
        }
    }
    if ($exitCode -ne 0) {
        throw "Dashboard server failed (exit code $exitCode)."
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Dashboard
        exit 0
    }
    catch {
        [Console]::Error.WriteLine('Dashboard failed: ' + (Get-SafeDiagnostic $_.Exception.Message))
        exit 1
    }
}
