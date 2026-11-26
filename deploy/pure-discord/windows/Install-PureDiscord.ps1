[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$InstallOllamaShim,
    [int]$ShimPort = 11435,
    [string]$OllamaUrl = 'http://127.0.0.1:11434',
    [string]$ShimToken = '',
    [string]$PythonPath = 'python',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
if ($InstallOllamaShim -and ($ShimPort -lt 1 -or $ShimPort -gt 65535)) {
    throw '-ShimPort must be between 1 and 65535.'
}
if ($InstallOllamaShim) {
    $parsedUrl = $null
    if (-not [Uri]::TryCreate($OllamaUrl, [UriKind]::Absolute, [ref]$parsedUrl) -or
        $parsedUrl.Scheme -notin @('http', 'https') -or
        $parsedUrl.UserInfo -or $parsedUrl.Query -or $parsedUrl.Fragment) {
        throw '-OllamaUrl must be an HTTP or HTTPS base URL without credentials, a query, or a fragment.'
    }
}

$installDir = if ($env:INSTALL_DIR) { $env:INSTALL_DIR } else { Join-Path $HOME '.local\bin' }
$env:Path = "$installDir;$env:Path"
$bothouse = Get-Command bothouse -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $bothouse) {
    if ($SkipInstall) {
        throw 'Bothouse was not found. Install it or rerun without -SkipInstall.'
    }
    if ($DryRun) {
        Write-Host 'Would install Bothouse with the official install.ps1.'
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor
            [Net.SecurityProtocolType]::Tls12
        $installer = Invoke-RestMethod 'https://raw.githubusercontent.com/zuiho-kai/bothouse/main/install.ps1'
        & ([scriptblock]::Create($installer))
        $bothouse = Get-Command bothouse -CommandType Application | Select-Object -First 1
    }
}

if ($DryRun) {
    Write-Host 'Would check Python 3.11+ and run bothouse setup clis.'
} else {
    $pythonExe = & $PythonPath -c 'import sys; print(sys.executable) if sys.version_info >= (3, 11) else sys.exit(1)'
    if ($LASTEXITCODE -ne 0 -or -not $pythonExe) {
        throw 'Python 3.11+ is required. Install it and pass its path with -PythonPath.'
    }
    & $bothouse.Source setup clis
    if ($LASTEXITCODE -ne 0) { throw 'bothouse setup clis failed.' }
}

if ($InstallOllamaShim) {
    $taskName = 'BothouseOllamaJevShim'
    if ($DryRun) {
        Write-Host "Would register $taskName on logon at 127.0.0.1:$ShimPort."
    } else {
        $kitDir = Split-Path $PSScriptRoot -Parent
        $shimPath = Join-Path $kitDir 'ollama_jev_shim.py'
        $pythonWindowless = Join-Path (Split-Path $pythonExe -Parent) 'pythonw.exe'
        if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) {
            throw 'pythonw.exe is required for a hidden shim. Use a standard Python installation.'
        }
        if ($ShimToken) {
            [Environment]::SetEnvironmentVariable('OLLAMA_JEV_TOKEN', $ShimToken, 'User')
        }
        $account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $arguments = ('"{0}" --host 127.0.0.1 --port {1} --upstream "{2}"' -f
            $shimPath, $ShimPort, $parsedUrl.AbsoluteUri)
        $action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $arguments -WorkingDirectory $kitDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
        $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force |
            Out-Null
        Write-Host "Registered $taskName. The shim starts at your next logon."
    }
}
Write-Host 'Next, run setup_discord.py init-manifest, then apply --dry-run and apply. Invite the bots and run bothouse daemon install.'
