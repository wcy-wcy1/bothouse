param(
    [Parameter(Mandatory)][string]$StateDirectory,
    [ValidateRange(1024, 65535)][int]$Port = 8790,
    [string]$UvPath = 'uv.exe',
    [string]$PythonPath,
    [string]$SourceDirectory = (Join-Path $PSScriptRoot '../../role-household'),
    [string]$UserMentionId
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
$env:RH_HUB_TOKEN = $state.web_token
$env:RH_BOT_TOKEN = $state.bot_token
$env:PYTHONIOENCODING = 'utf-8'
$database = Join-Path $StateDirectory 'hub.sqlite3'
Push-Location -LiteralPath $SourceDirectory
try {
    if ($UserMentionId) {
        $env:RH_USER_MENTION_ID = $UserMentionId
    }
    $hubArgs = @('-m', 'rh.hub', '--host', '127.0.0.1', '--port', $Port, '--db', $database)
    if ($PythonPath) {
        & $PythonPath @hubArgs
    } else {
        & $UvPath run --no-project --python '>=3.11' --with 'aiohttp==3.14.5' --with 'cryptography==50.0.2' python @hubArgs
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
    $env:RH_HUB_TOKEN = $previousWeb
    $env:RH_BOT_TOKEN = $previousBot
    $env:PYTHONIOENCODING = $previousEncoding
    $env:RH_USER_MENTION_ID = $previousUserMention
}
