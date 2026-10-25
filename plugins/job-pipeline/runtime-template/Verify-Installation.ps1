[CmdletBinding()]
param(
    [switch]$ExpectBlank
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
        throw "$Label is missing."
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

function Test-IsWithinProject {
    param([string]$ProjectRoot, [string]$Path)
    $root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $full = [IO.Path]::GetFullPath($Path)
    $rootPrefix = $root + [IO.Path]::DirectorySeparatorChar
    return $full.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-TrustedExecutablePath {
    param(
        [string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('node.exe')][string]$ExpectedName,
        [string]$ProjectRoot,
        [string]$Label
    )
    if (-not [IO.Path]::GetExtension($Path).Equals('.exe', [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFileName($Path).Equals($ExpectedName, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label must be the exact expected .exe application."
    }
    $trusted = Assert-PlainSingleLinkFile -Path $Path -Label $Label
    if (Test-IsWithinProject -ProjectRoot $ProjectRoot -Path $trusted) {
        throw "$Label must be outside the extracted project folder."
    }
    return $trusted
}

function Invoke-CheckedNative {
    param([string]$Executable, [string[]]$Arguments, [string]$Label)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $null = & $Executable @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        throw "$Label failed (exit code $exitCode)."
    }
}

function Invoke-CapturedNative {
    param([string]$Executable, [string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $Executable @Arguments 2>$null)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = $output }
}

function Get-FirstApplication {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('node.exe')][string]$Name,
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )
    $commands = @(Get-Command -Name $Name -CommandType Application -All -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($commands.Count -eq 0) {
        return $null
    }
    return Assert-TrustedExecutablePath -Path $commands[0].Source -ExpectedName $Name -ProjectRoot $ProjectRoot -Label 'Node.js interpreter'
}

function Invoke-Verification {
    if (-not (Test-SupportedPowerShellVersion $PSVersionTable.PSVersion)) {
        throw 'PowerShell 5.1 or newer is required.'
    }
    $projectRoot = Get-ProjectRoot
    $null = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path $PSCommandPath -Label 'Installation verifier'
    $scannerPath = Join-Path $projectRoot 'Pipeline\privacy_scan.py'
    if (-not (Test-Path -LiteralPath $scannerPath -PathType Leaf)) {
        throw 'Privacy scanner is missing. Package is incomplete; restore Pipeline/privacy_scan.py.'
    }
    foreach ($relative in @(
        'Pipeline\__init__.py',
        'Pipeline\pipeline_store.py',
        'Pipeline\sync_dashboard.py',
        'Pipeline\onboarding.py',
        'Pipeline\privacy_scan.py',
        'Pipeline\dashboard.js'
    )) {
        $null = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path (Join-Path $projectRoot $relative) -Label 'Required verification file'
    }
    $venvPath = Assert-ConfinedPath -ProjectRoot $projectRoot -Path (Join-Path $projectRoot '.venv')
    if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
        throw 'Project virtual environment is missing. Run Install.ps1 first.'
    }
    $venvPython = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path (Join-Path $venvPath 'Scripts\python.exe') -Label 'Project virtual-environment interpreter'

    $smoke = "import fitz, jinja2, yaml, docx, pytest; from PIL import Image; from weasyprint import HTML; data=HTML(string='<!doctype html><html><body>renderer smoke</body></html>').write_pdf(); assert data.startswith(b'%PDF'); document=fitz.open(stream=data,filetype='pdf'); assert document.page_count == 1; document.close()"
    $privacyArguments = @('-B', '-E', '-s', '-m', 'Pipeline.privacy_scan', '--installed', $projectRoot)
    if ($ExpectBlank) {
        $privacyArguments += '--expect-blank'
    }
    $blankCheck = @'
import json
import sys
from pathlib import Path
from Pipeline.onboarding import load_runtime
from Pipeline.pipeline_store import PipelineStore
root = Path(sys.argv[1])
runtime = load_runtime(root)
state = PipelineStore(root / 'Pipeline' / 'pipeline.db').load_state()
accounts = json.loads((root / 'private' / 'accounts.json').read_text(encoding='utf-8'))
ok = (
    runtime['submission_mode'] == 'review_only'
    and runtime['master_cv_path'] == ''
    and runtime['master_cv_sha256'] == ''
    and runtime['auto_apply_approved_at'] is None
    and runtime['onboarding'] == {'complete': False, 'creator_ready': False}
    and state['jobs'] == []
    and state['settings']['daily_submission_cap'] == 0
    and accounts['identity_providers'] == []
    and accounts['employer_tenants'] == []
    and accounts['no_account_platforms'] == []
)
raise SystemExit(0 if ok else 23)
'@

    $environmentNames = @(
        'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT', 'PYTHONBREAKPOINT',
        'PYTEST_ADDOPTS', 'PYTHONDONTWRITEBYTECODE', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD',
        'NODE_OPTIONS'
    )
    $oldEnvironment = @{}
    foreach ($name in $environmentNames) {
        $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    [Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', '1', 'Process')
    [Environment]::SetEnvironmentVariable('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1', 'Process')
    Push-Location -LiteralPath $projectRoot
    try {
        Invoke-CheckedNative -Executable $venvPython -Arguments @('-B', '-E', '-s', '-c', $smoke) -Label 'Required imports and WeasyPrint renderer smoke test'
        Invoke-CheckedNative -Executable $venvPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.pipeline_store', '--root', $projectRoot, 'validate') -Label 'Canonical database validation'
        Invoke-CheckedNative -Executable $venvPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.pipeline_store', '--root', $projectRoot, 'export') -Label 'Canonical state export'
        $statusProbe = Invoke-CapturedNative -Executable $venvPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.onboarding', '--root', $projectRoot, 'status')
        if ($statusProbe.ExitCode -ne 0) {
            throw "Onboarding status validation failed (exit code $($statusProbe.ExitCode))."
        }
        try {
            $status = (($statusProbe.Output -join [Environment]::NewLine) | ConvertFrom-Json)
        }
        catch {
            throw 'Onboarding status validation returned invalid JSON.'
        }
        if ($null -eq $status -or $status.ready -isnot [bool]) {
            throw 'Onboarding status validation returned an invalid result.'
        }
        if ($status.ready) {
            Invoke-CheckedNative -Executable $venvPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.sync_dashboard', '--root', $projectRoot) -Label 'Dashboard-state synchronization'
        }
        Invoke-CheckedNative -Executable $venvPython -Arguments $privacyArguments -Label 'Installed privacy scan'
        if ($ExpectBlank) {
            Invoke-CheckedNative -Executable $venvPython -Arguments @('-B', '-E', '-s', '-c', $blankCheck, $projectRoot) -Label 'Blank-install state check'
        }

        $nodePath = Get-FirstApplication -Name 'node.exe' -ProjectRoot $projectRoot
        if ($nodePath) {
            Invoke-CheckedNative -Executable $nodePath -Arguments @('--check', (Join-Path $projectRoot 'Pipeline\dashboard.js')) -Label 'Dashboard JavaScript syntax check'
            [Console]::Out.WriteLine('Dashboard JavaScript syntax check passed.')
        }
        else {
            [Console]::Out.WriteLine('Node.js not found; dashboard JavaScript syntax check skipped. Node.js was not installed.')
        }

        Invoke-CheckedNative -Executable $venvPython -Arguments @('-B', '-E', '-s', '-m', 'pytest', '-p', 'no:cacheprovider', '-m', 'not source_only', 'Pipeline/tests', '-q') -Label 'Installed package tests'
    }
    finally {
        Pop-Location
        foreach ($name in $environmentNames) {
            [Environment]::SetEnvironmentVariable($name, $oldEnvironment[$name], 'Process')
        }
    }
    [Console]::Out.WriteLine('Installation verification passed. No external action was performed.')
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Verification
        exit 0
    }
    catch {
        [Console]::Error.WriteLine('Verification failed: ' + (Get-SafeDiagnostic $_.Exception.Message))
        exit 1
    }
}
