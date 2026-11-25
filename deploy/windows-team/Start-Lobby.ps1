param(
    [Parameter(Mandatory)][string]$StateDirectory,
    [ValidateRange(1024, 65535)][int]$Port = 8790,
    [string]$UvPath = 'uv.exe',
    [string]$PythonPath,
    [string]$SourceDirectory = (Join-Path $PSScriptRoot '../../role-household'),
    [string]$UserMentionId,
    [string]$T3StateDb,
    [string]$PanelConfig,
    [string]$WikiDir,
    [string]$LogFile
)

$ErrorActionPreference = 'Stop'
$StateDirectory = (Resolve-Path -LiteralPath $StateDirectory).ProviderPath
$state = Get-Content -LiteralPath (Join-Path $StateDirectory 'hub.json') -Raw | ConvertFrom-Json
if (!$state.web_token -or !$state.bot_token) {
    throw 'hub.json must contain both web_token and bot_token'
}
$previousWeb = $env:RH_HUB_TOKEN
$previousBot = $env:RH_BOT_TOKEN
$previousEncoding = $env:PYTHONIOENCODING
$previousUserMention = $env:RH_USER_MENTION_ID
$previousSslCertFile = $env:SSL_CERT_FILE
$previousT3StateDb = $env:RH_T3_STATE_DB
$previousPanelConfig = $env:RH_PANEL_CONFIG
$previousWikiDir = $env:RH_WIKI_DIR

$env:RH_HUB_TOKEN = $state.web_token
$env:RH_BOT_TOKEN = $state.bot_token
$env:PYTHONIOENCODING = 'utf-8'
if ($UserMentionId) {
    $env:RH_USER_MENTION_ID = $UserMentionId
}
if ($T3StateDb) {
    $env:RH_T3_STATE_DB = $T3StateDb
}
if ($PanelConfig) {
    $env:RH_PANEL_CONFIG = $PanelConfig
}
if ($WikiDir) {
    $env:RH_WIKI_DIR = $WikiDir
}
if (-not $env:SSL_CERT_FILE -and $PythonPath) {
    $ErrorActionPreference = 'Continue'
    $certifiPath = (& $PythonPath -c "import certifi; print(certifi.where())" 2>$null | Select-Object -First 1)
    $ErrorActionPreference = 'Stop'
    if ($certifiPath -and (Test-Path -LiteralPath $certifiPath.Trim())) {
        $env:SSL_CERT_FILE = $certifiPath.Trim()
    }
}

$database = Join-Path $StateDirectory 'hub.sqlite3'
Push-Location -LiteralPath $SourceDirectory
try {
    $hubArgs = @('-m', 'rh.hub', '--host', '127.0.0.1', '--port', $Port, '--db', $database)
    $cmdExe = if ($PythonPath) { $PythonPath } else { $UvPath }
    $cmdArgs = if ($PythonPath) {
        $hubArgs
    } else {
        @('run', '--no-project', '--python', '>=3.11', '--with', 'aiohttp==3.14.5', '--with', 'cryptography==50.0.2', 'python') + $hubArgs
    }
    if ($LogFile) {
        $logParent = Split-Path -Parent $LogFile
        if ($logParent -and !(Test-Path -LiteralPath $logParent)) {
            New-Item -ItemType Directory -Path $logParent -Force | Out-Null
        }
        $ErrorActionPreference = 'Continue'
        & $cmdExe @cmdArgs 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { $_ }
        } | Out-File -FilePath $LogFile -Append -Encoding utf8
        exit $LASTEXITCODE
    }
    & $cmdExe @cmdArgs
    exit $LASTEXITCODE
} finally {
    $ErrorActionPreference = 'Stop'
    Pop-Location
    $env:RH_HUB_TOKEN = $previousWeb
    $env:RH_BOT_TOKEN = $previousBot
    $env:PYTHONIOENCODING = $previousEncoding
    $env:RH_USER_MENTION_ID = $previousUserMention
    $env:SSL_CERT_FILE = $previousSslCertFile
    $env:RH_T3_STATE_DB = $previousT3StateDb
    $env:RH_PANEL_CONFIG = $previousPanelConfig
    $env:RH_WIKI_DIR = $previousWikiDir
}
