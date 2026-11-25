param(
    [string]$LobbyUrl = 'http://127.0.0.1:8790',
    [string]$HubStatePath = (Join-Path $env:USERPROFILE '.bothouse/hub/hub.json'),
    [string[]]$ExpectOnlineBots = @(),
    [switch]$CheckTailscale,
    [string]$PythonPath = 'python',
    [string]$TailscaleExe = 'tailscale'
)

$ErrorActionPreference = 'Stop'
$probe = @'
import asyncio
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ProbeError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get_ok(opener, request, label):
    try:
        response = opener.open(request, timeout=10)
    except HTTPError as error:
        raise ProbeError(f"{label}: expected HTTP 200, got HTTP {error.code}.") from None
    if response.status != 200:
        status = response.status
        response.close()
        raise ProbeError(f"{label}: expected HTTP 200, got HTTP {status}.")
    return response


async def check_bots(url, token, expected):
    try:
        import aiohttp
    except ImportError:
        raise ProbeError("Install aiohttp in the Python environment used for this check.") from None

    async def reject_redirect(session, context, params):
        raise ProbeError("WebSocket endpoint returned a redirect.")

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    async with aiohttp.ClientSession(trust_env=False, trace_configs=[trace]) as session:
        async with asyncio.timeout(10):
            async with session.ws_connect(url + "/ws") as ws:
                await ws.send_json({"type": "hello", "token": token, "kind": "web"})
                ready = await ws.receive_json()
    if ready.get("type") != "ready":
        raise ProbeError("WebSocket did not return a ready message.")

    bots = {bot["id"]: bot for bot in ready["bots"]}
    all_online = True
    for bot_id in expected:
        bot = bots.get(bot_id, {})
        online = bot.get("online") is True
        print(json.dumps({"id": bot_id, "name": bot.get("name", ""), "online": online}))
        all_online &= online
    if not all_online:
        raise ProbeError("One or more expected bots are missing or offline.")


def main():
    lobby_url, state_path, *expected = sys.argv[1:]
    stage = "Lobby URL"
    try:
        url = lobby_url.rstrip("/")
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.query or parts.fragment):
            raise ProbeError("Use an HTTP or HTTPS lobby URL without credentials, query, or fragment.")

        stage = "Lobby HTTP check"
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with get_ok(opener, url, stage):
            print("Lobby: HTTP 200")

        stage = "Hub state"
        token = json.loads(Path(state_path).read_text(encoding="utf-8-sig")).get("web_token")
        if not isinstance(token, str) or not token.strip():
            raise ProbeError("Hub state must contain a non-empty web token.")

        stage = "Push API check"
        request = Request(url + "/api/push", headers={"Authorization": f"Bearer {token}"})
        with get_ok(opener, request, stage) as response:
            key = json.load(response).get("key")
        if not isinstance(key, str) or not key.strip():
            raise ProbeError("Push API did not return a non-empty key.")
        print(f"Push API: HTTP 200, key length {len(key)}")

        if expected:
            stage = "WebSocket check"
            asyncio.run(check_bots(url, token, expected))
    except ProbeError as error:
        print(error, file=sys.stderr)
        return 1
    except Exception as error:
        # Exception messages can contain URLs, headers or response bodies.
        print(f"{stage} failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    return 0


sys.exit(main())
'@

$probe | & $PythonPath -B - $LobbyUrl $HubStatePath @ExpectOnlineBots
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

if ($CheckTailscale) {
    $statusJson = & $TailscaleExe serve status --json
    if ($LASTEXITCODE -ne 0) {
        throw "Tailscale Serve status failed (exit code $LASTEXITCODE)."
    }
    $serve = $statusJson | ConvertFrom-Json
    $activeConfigs = @($serve) + @($serve.Foreground.PSObject.Properties.Value)
    $publicListeners = $activeConfigs | ForEach-Object {
        $_.AllowFunnel.PSObject.Properties | Where-Object { $_.Value -eq $true }
    }
    if ($publicListeners) {
        throw 'Tailscale Serve status reports a public Funnel listener. Disable it before verifying Tailnet-only access.'
    }
    $tcpPorts = $serve.TCP.PSObject.Properties | Where-Object { $_.Value.TCPForward }
    if (!$tcpPorts) {
        throw 'Tailscale Serve status does not show a TCP forwarding listener.'
    }
    Write-Output "Tailscale Serve: Tailnet-only TCP forwarding on port(s) $($tcpPorts.Name -join ', ')."
}
