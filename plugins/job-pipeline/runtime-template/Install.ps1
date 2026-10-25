[CmdletBinding()]
param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-SupportedPowerShellVersion {
    param([Parameter(Mandatory = $true)][Version]$Version)
    return $Version -ge [Version]'5.1'
}

function Test-SupportedPythonVersion {
    param([Parameter(Mandatory = $true)][string]$VersionText)
    if ($VersionText -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
        return $false
    }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    return ($major -gt 3) -or ($major -eq 3 -and $minor -ge 11)
}

function Get-SafeDiagnostic {
    param([string]$Message)
    $safe = [string]$Message
    if ($PSScriptRoot) {
        $safe = [regex]::Replace($safe, [regex]::Escape($PSScriptRoot), '<project>', 'IgnoreCase')
    }
    if ($env:JOB_PIPELINE_TEST_PYTHON) {
        $safe = [regex]::Replace($safe, [regex]::Escape($env:JOB_PIPELINE_TEST_PYTHON), '<python>', 'IgnoreCase')
    }
    return [regex]::Replace($safe, '(?i)[a-z]:\\users\\[^\\\r\n]+', '<user>')
}

function Get-ProjectRoot {
    $item = Get-Item -LiteralPath $PSScriptRoot -Force
    if (-not $item.PSIsContainer -or $item.PSProvider.Name -ne 'FileSystem') {
        throw 'Project root is not a local directory.'
    }
    $linkType = $item.PSObject.Properties['LinkType']
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($linkType -and $linkType.Value)) {
        throw 'Project root must not be a link or reparse point.'
    }
    return $item.FullName.TrimEnd([IO.Path]::DirectorySeparatorChar)
}

function Assert-ConfinedPath {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )
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
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is missing."
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSProvider.Name -ne 'FileSystem') {
        throw "$Label is not a local file."
    }
    $linkType = $item.PSObject.Properties['LinkType']
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($linkType -and $linkType.Value)) {
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
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $confined = Assert-ConfinedPath -ProjectRoot $ProjectRoot -Path $Path
    return Assert-PlainSingleLinkFile -Path $confined -Label $Label
}

function Test-IsWithinProject {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $root = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $full = [IO.Path]::GetFullPath($Path)
    $rootPrefix = $root + [IO.Path]::DirectorySeparatorChar
    return $full.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-TrustedExecutablePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidateSet('py.exe', 'python.exe', 'node.exe')][string]$ExpectedName,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Label
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

function Invoke-CapturedNative {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @()
    )
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

function Get-PythonVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$PrefixArguments = @()
    )
    $arguments = @($PrefixArguments) + @('-E', '-s', '-c', 'import sys; print(sys.version_info.major,sys.version_info.minor,sys.version_info.micro,sep=chr(46))')
    $probe = Invoke-CapturedNative -Executable $Executable -Arguments $arguments
    if ($probe.ExitCode -ne 0 -or $probe.Output.Count -ne 1) {
        return $null
    }
    $value = ([string]$probe.Output[0]).Trim()
    if (-not (Test-SupportedPythonVersion $value)) {
        return $null
    }
    return $value
}

function Get-FirstApplication {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('py.exe', 'python.exe', 'node.exe')][string]$Name,
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )
    $commands = @(Get-Command -Name $Name -CommandType Application -All -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($commands.Count -eq 0) {
        return $null
    }
    return Assert-TrustedExecutablePath -Path $commands[0].Source -ExpectedName $Name -ProjectRoot $ProjectRoot -Label 'Resolved application'
}

function Resolve-InstallerPython {
    param(
        [switch]$UseTestOverride,
        [string]$ProjectRoot = (Get-ProjectRoot)
    )
    if ($UseTestOverride) {
        if (-not $env:JOB_PIPELINE_TEST_PYTHON) {
            throw '-SkipDependencyInstall is test-only and requires JOB_PIPELINE_TEST_PYTHON.'
        }
        $path = Assert-TrustedExecutablePath -Path $env:JOB_PIPELINE_TEST_PYTHON -ExpectedName 'python.exe' -ProjectRoot $ProjectRoot -Label 'Test Python interpreter'
        if (-not (Get-PythonVersion -Executable $path)) {
            throw 'Python 3.11 or newer is required.'
        }
        return [pscustomobject]@{ Executable = $path; PrefixArguments = @() }
    }
    if ($env:JOB_PIPELINE_TEST_PYTHON) {
        throw 'JOB_PIPELINE_TEST_PYTHON is test-only and valid only with -SkipDependencyInstall.'
    }
    $launcher = Get-FirstApplication -Name 'py.exe' -ProjectRoot $ProjectRoot
    if ($launcher) {
        if (Get-PythonVersion -Executable $launcher -PrefixArguments @('-3')) {
            return [pscustomobject]@{ Executable = $launcher; PrefixArguments = @('-3') }
        }
    }
    $python = Get-FirstApplication -Name 'python.exe' -ProjectRoot $ProjectRoot
    if ($python) {
        if (Get-PythonVersion -Executable $python) {
            return [pscustomobject]@{ Executable = $python; PrefixArguments = @() }
        }
    }
    throw 'Python 3.11 or newer is required. Install Python for Windows, then rerun Install.ps1.'
}

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$Label
    )
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

function Get-PipInstallArguments {
    param([Parameter(Mandatory = $true)][string]$RequirementsPath)
    return @(
        '-E', '-s', '-m', 'pip', '--isolated', 'install',
        '--disable-pip-version-check', '--no-input', '--no-cache-dir',
        '--requirement', $RequirementsPath
    )
}

function Invoke-Install {
    if (-not (Test-SupportedPowerShellVersion $PSVersionTable.PSVersion)) {
        throw 'PowerShell 5.1 or newer is required.'
    }
    $projectRoot = Get-ProjectRoot
    $null = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path $PSCommandPath -Label 'Installer script'
    foreach ($relative in @(
        'requirements.txt',
        'Pipeline\__init__.py',
        'Pipeline\initialize.py',
        'Pipeline\pipeline_store.py',
        'Pipeline\sync_dashboard.py',
        'Pipeline\onboarding.py',
        'Pipeline\privacy_scan.py'
    )) {
        $null = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path (Join-Path $projectRoot $relative) -Label 'Required project file'
    }

    foreach ($name in @(
        'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT', 'PYTHONBREAKPOINT',
        'PYTEST_ADDOPTS', 'PIP_CONFIG_FILE', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL',
        'PIP_REQUIRE_VIRTUALENV', 'PIP_TARGET'
    )) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    [Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', '1', 'Process')

    $python = Resolve-InstallerPython -UseTestOverride:$SkipDependencyInstall -ProjectRoot $projectRoot
    $venvPath = Assert-ConfinedPath -ProjectRoot $projectRoot -Path (Join-Path $projectRoot '.venv')
    $venvPythonPath = Join-Path $venvPath 'Scripts\python.exe'

    Push-Location -LiteralPath $projectRoot
    try {
        if (-not (Test-Path -LiteralPath $venvPath)) {
            Invoke-CheckedNative -Executable $python.Executable -Arguments (@($python.PrefixArguments) + @('-E', '-s', '-m', 'venv', $venvPath)) -Label 'Virtual environment creation'
        }
        $null = Assert-ConfinedPath -ProjectRoot $projectRoot -Path $venvPath
        if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
            throw 'Project .venv is not a directory.'
        }
        $venvPython = Assert-ConfinedPlainFile -ProjectRoot $projectRoot -Path $venvPythonPath -Label 'Project virtual-environment interpreter'
        if (-not (Get-PythonVersion -Executable $venvPython)) {
            throw 'Project virtual environment requires Python 3.11 or newer.'
        }

        if ($SkipDependencyInstall) {
            $runPython = $python.Executable
        }
        else {
            [Console]::Out.WriteLine('Installing pinned Python dependencies...')
            $requirements = Join-Path $projectRoot 'requirements.txt'
            $pipArguments = @(Get-PipInstallArguments -RequirementsPath $requirements)
            Invoke-CheckedNative -Executable $venvPython -Arguments $pipArguments -Label 'Pinned dependency installation'
            $runPython = $venvPython
            $smoke = "import fitz, jinja2, yaml, docx, pytest; from PIL import Image; from weasyprint import HTML; data=HTML(string='<html><body>ok</body></html>').write_pdf(); assert data.startswith(b'%PDF')"
            Invoke-CheckedNative -Executable $runPython -Arguments @('-B', '-E', '-s', '-c', $smoke) -Label 'Dependency and renderer smoke test'
        }

        Invoke-CheckedNative -Executable $runPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.initialize', '--root', $projectRoot) -Label 'Private-state initialization'
        Invoke-CheckedNative -Executable $runPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.pipeline_store', '--root', $projectRoot, 'validate') -Label 'Canonical database validation'
        Invoke-CheckedNative -Executable $runPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.pipeline_store', '--root', $projectRoot, 'export') -Label 'Canonical state export'
        $statusProbe = Invoke-CapturedNative -Executable $runPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.onboarding', '--root', $projectRoot, 'status')
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
            Invoke-CheckedNative -Executable $runPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.sync_dashboard', '--root', $projectRoot) -Label 'Dashboard-state synchronization'
        }
        Invoke-CheckedNative -Executable $runPython -Arguments @('-B', '-E', '-s', '-m', 'Pipeline.privacy_scan', '--installed', $projectRoot) -Label 'Installed privacy scan'
    }
    finally {
        Pop-Location
    }

    [Console]::Out.WriteLine('Installation complete. New blank installs remain review_only with daily cap 0.')
    [Console]::Out.WriteLine('Existing private files were preserved.')
    [Console]::Out.WriteLine('Next: open this exact workspace in Claude Code or Codex local execution and invoke the Job Pipeline setup skill. Other surfaces require verified local-file and process capabilities.')
    [Console]::Out.WriteLine('First task: Set up my job pipeline. Ask me for my master CV, master profile, and desired jobs.')
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-Install
        exit 0
    }
    catch {
        [Console]::Error.WriteLine('Install failed: ' + (Get-SafeDiagnostic $_.Exception.Message))
        exit 1
    }
}
