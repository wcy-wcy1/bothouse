param(
    [ValidateRange(1024, 65535)][int]$LocalPort = 8790,
    [ValidateRange(1024, 65535)][int]$ServeTcpPort = 8790,
    [switch]$EnableHttps,
    [ValidateRange(1, 65535)][int]$HttpsPort = 443,
    [switch]$Disable,
    [switch]$DryRun,
    [string]$TailscaleExe = 'tailscale'
)

$ErrorActionPreference = 'Stop'
$tcpDestination = "127.0.0.1:${LocalPort}"
$tcpCommand = @('serve', '--bg', '--yes', '--tcp', $ServeTcpPort, "tcp://$tcpDestination")
$httpsCommand = @('serve', '--bg', '--yes', "--https=$HttpsPort", "http://$tcpDestination")
$tcpOff = @('serve', "--tcp=$ServeTcpPort", 'off')
$httpsOff = @('serve', "--https=$HttpsPort", 'off')
$serveJson = @('serve', 'status', '--json')

if (!$Disable -and $EnableHttps -and $ServeTcpPort -eq $HttpsPort) {
    throw 'ServeTcpPort and HttpsPort must differ when HTTPS is enabled.'
}

if ($DryRun) {
    Write-Output 'Dry run: Tailnet-only Tailscale Serve.'
    if ($Disable) {
        Write-Output "$TailscaleExe $serveJson (check configured listeners)"
        Write-Output "$TailscaleExe $tcpOff (if the TCP forward exists)"
        Write-Output "$TailscaleExe $httpsOff (if the HTTPS listener exists)"
    } else {
        if ($EnableHttps) {
            Write-Output "$TailscaleExe status --json (check HTTPS Certificates)"
        }
        Write-Output "$TailscaleExe $tcpCommand"
        if ($EnableHttps) {
            Write-Output "$TailscaleExe $httpsCommand"
        }
        Write-Output "$TailscaleExe $serveJson (verify forwarding and restore TCP if needed)"
    }
    Write-Output "$TailscaleExe serve status"
    return
}

function Invoke-Tailscale {
    param([string[]]$Arguments)

    & $TailscaleExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Tailscale command failed (exit code ${LASTEXITCODE}): $Arguments"
    }
}

if ($Disable) {
    # Tailscale can return an error for an absent listener when other Serve rules exist.
    $serve = Invoke-Tailscale $serveJson | ConvertFrom-Json
    if ($serve.TCP.PSObject.Properties[[string]$ServeTcpPort].Value.TCPForward) {
        Invoke-Tailscale $tcpOff
    }
    if ($serve.TCP.PSObject.Properties[[string]$HttpsPort].Value.HTTPS) {
        Invoke-Tailscale $httpsOff
    }
    Invoke-Tailscale @('serve', 'status')
    return
}

if ($EnableHttps) {
    $nodeStatus = Invoke-Tailscale @('status', '--json') | ConvertFrom-Json
    if (!$nodeStatus.CertDomains) {
        throw 'Enable HTTPS Certificates in the Tailscale admin console under DNS, then rerun this command.'
    }
}

Invoke-Tailscale $tcpCommand
if ($EnableHttps) {
    Invoke-Tailscale $httpsCommand
}

$serve = Invoke-Tailscale $serveJson | ConvertFrom-Json
if ($EnableHttps -and $serve.TCP.PSObject.Properties[[string]$ServeTcpPort].Value.TCPForward -ne $tcpDestination) {
    Invoke-Tailscale $tcpCommand
    $serve = Invoke-Tailscale $serveJson | ConvertFrom-Json
}
if ($serve.TCP.PSObject.Properties[[string]$ServeTcpPort].Value.TCPForward -ne $tcpDestination) {
    throw "Tailscale Serve did not report TCP port $ServeTcpPort forwarding to $tcpDestination."
}
if ($EnableHttps -and !$serve.TCP.PSObject.Properties[[string]$HttpsPort].Value.HTTPS) {
    throw "Tailscale Serve did not report HTTPS port $HttpsPort."
}
Invoke-Tailscale @('serve', 'status')
