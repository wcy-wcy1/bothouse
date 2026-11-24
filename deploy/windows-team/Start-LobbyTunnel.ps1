param(
    [Parameter(Mandatory)][string]$SshHost,
    [ValidateRange(1024, 65535)][int]$LocalPort = 8790,
    [ValidateRange(1024, 65535)][int]$RemotePort = 18879
)

$forward = "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}"
& ssh.exe -N -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes `
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R $forward -- $SshHost
exit $LASTEXITCODE
