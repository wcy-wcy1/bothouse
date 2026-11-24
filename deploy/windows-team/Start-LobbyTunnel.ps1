param(
    [Parameter(Mandatory)][string]$SshHost,
    [ValidateRange(1024, 65535)][int]$LocalPort = 8790,
    [ValidateRange(1024, 65535)][int]$RemotePort = 18879,
    [ValidateRange(0, 2147483647)][int]$MaxAttempts = 0,
    [string]$LogPath
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$ssh = Get-Command ssh.exe -ErrorAction Stop
$forward = "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}"
$sshArgs = @(
    '-n', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
    '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
    '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3'
)
if ($LogPath) {
    $sshArgs += @('-E', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LogPath))
}
$sshArgs += @('-R', $forward, '--', $SshHost)

$attempt = 0
$delaySeconds = 2
while ($true) {
    $attempt++
    $timer = [Diagnostics.Stopwatch]::StartNew()
    & $ssh @sshArgs
    $result = $LASTEXITCODE
    if ($MaxAttempts -gt 0 -and $attempt -ge $MaxAttempts) {
        exit $result
    }
    if ($timer.Elapsed.TotalMinutes -ge 1) {
        $delaySeconds = 2
    }
    Write-Warning "SSH exited with code $result. Retrying in $delaySeconds seconds."
    Start-Sleep -Seconds $delaySeconds
    $delaySeconds = [Math]::Min($delaySeconds * 2, 60)
}
