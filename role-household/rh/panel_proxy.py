"""Reverse proxy for the local Bothouse management panel."""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

from .hub import is_web_authed, make_auth_gate_html, set_auth_cookie

PANEL_PREFIX = "/collab/panel"
GATE_ALLOWED_PATHS = {"decide", "context", "lead", "whoami", "memvote"}
SAFE_REQUEST_HEADERS = {"accept", "accept-language", "content-type", "if-none-match", "if-modified-since", "range"}
SAFE_RESPONSE_HEADERS = {
    "content-type", "cache-control", "etag", "last-modified",
    "content-disposition", "content-range", "accept-ranges",
}


def rewrite_panel_html(body: str, prefix: str = PANEL_PREFIX) -> str:
    """Point the panel page at its mount path and mark it as proxy-signed."""
    mount = json.dumps(f"{prefix}/").replace("<", "\\u003c")
    fix = (
        f'<script>(()=>{{const m={mount},p=location.pathname,i=p.indexOf(m);'
        f'if(i>0)document.querySelector("base").setAttribute("href",p.slice(0,i)+m)}})()</script>'
    )
    head = f'<base href="{prefix}/" />{fix}\n    <meta name="bothouse-auth" content="proxy" />'
    if re.search(r"<base\b[^>]*>", body, flags=re.I):
        return re.sub(r"<base\b[^>]*>", head, body, count=1, flags=re.I)
    if re.search(r"<head(?:\s[^>]*)?>", body, flags=re.I):
        return re.sub(r"<head(?:\s[^>]*)?>", lambda m: f"{m.group(0)}\n    {head}", body, count=1, flags=re.I)
    return head + "\n" + body


def _notice_html(reason: str, help_text: str = "") -> str:
    safe_reason = html.escape(reason)
    safe_help = f"<p>{html.escape(help_text)}</p>" if help_text else ""
    return (
        '<!doctype html><meta charset="utf-8"><title>bothouse</title><meta name="color-scheme" content="light dark">'
        '<style>:root{--c:#fff;--s:#fff;--l:#e6e9ef;--i:#17181b;--m:#5a5a55;--a:#16a34a}'
        '@media (prefers-color-scheme:dark){:root{--c:#0d0d0f;--s:#16171a;--l:#2c2e34;--i:#eceef2;--m:#a5aab4;--a:#3dd68c}}'
        'body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--c);color:var(--m);font:14px/1.6 system-ui,"PingFang SC",sans-serif}'
        'main{box-sizing:border-box;width:min(520px,calc(100% - 32px));padding:24px;border:1px solid var(--l);border-radius:12px;background:var(--s)}'
        'h3{margin:0 0 8px;font-size:16px;font-weight:600;color:var(--i)}p{margin:0 0 8px}'
        'button{margin-top:8px;font:inherit;font-weight:500;height:36px;padding:0 14px;border-radius:8px;border:1px solid var(--l);background:var(--s);color:var(--i);cursor:pointer}</style>'
        f'<main><h3>打不开 bothouse 管理面板</h3><p>{safe_reason}</p>{safe_help}'
        '<button type="button" onclick="location.reload()">重试</button></main>'
    )


def attach(app: web.Application, hub, config_path: str | Path | None = None) -> None:
    cfg_path = config_path or os.environ.get("RH_PANEL_CONFIG")
    session: ClientSession | None = None

    def _read_toml() -> tuple[dict | None, str | None, str]:
        if not cfg_path or not Path(cfg_path).is_file():
            return None, "管理面板配置不存在", "请确认 RH_PANEL_CONFIG 指向本机 bothouse config.toml。"
        try:
            return tomllib.loads(Path(cfg_path).read_text("utf-8")), None, ""
        except (OSError, tomllib.TOMLDecodeError):
            return None, "管理面板配置无法读取", "请检查 TOML 格式是否正确。"

    def _norm_host(raw: object) -> str:
        host = str(raw or "127.0.0.1")
        if host in ("", "0.0.0.0", "::", "[::]"):
            return "127.0.0.1"
        if ":" in host and not host.startswith("["):
            return f"[{host}]"
        return host

    def settings():
        data, err, help_text = _read_toml()
        if err:
            return None, err, help_text
        value = data.get("management", {})
        if not isinstance(value, dict) or value.get("enabled") is not True:
            return None, "bothouse 管理接口未启用", "请在 [management] 中设置 enabled = true 并重启 bothouse。"
        host = _norm_host(value.get("host", "127.0.0.1"))
        try:
            port = int(value["port"])
            if not 1 <= port <= 65535:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return None, "管理面板端口配置无效", "请检查 [management].port。"
        return {"base": f"http://{host}:{port}", "token": str(value.get("token", ""))}, None, ""

    def bridge_settings():
        data, err, _help = _read_toml()
        if err:
            return None, err
        value = data.get("bridge", {})
        if not isinstance(value, dict) or value.get("enabled") is not True:
            return None, "bothouse Bridge 未启用"
        host = _norm_host(value.get("host", "127.0.0.1"))
        try:
            port = int(value.get("port", 9810))
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            return None, "bothouse Bridge 端口配置无效"
        raw_path = str(value.get("path") or "/bridge/ws").strip()
        ws_path = raw_path if raw_path.startswith("/") else "/" + raw_path
        return {"base": f"http://{host}:{port}", "path": ws_path}, None

    async def _relay_ws(request: web.Request, upstream_url: str, headers: dict[str, str], err_msg: str):
        raw_proto = request.headers.get("Sec-WebSocket-Protocol", "")
        protocols = [part.strip() for part in raw_proto.split(",") if part.strip()]
        try:
            upstream = await session.ws_connect(upstream_url, headers=headers, protocols=protocols or ())
        except (ClientError, OSError, asyncio.TimeoutError):
            return web.json_response({"ok": False, "error": err_msg}, status=502)
        downstream = web.WebSocketResponse(autoping=True)
        await downstream.prepare(request)

        async def down_to_up():
            async for msg in downstream:
                if msg.type == WSMsgType.TEXT:
                    await upstream.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await upstream.send_bytes(msg.data)

        async def up_to_down():
            async for msg in upstream:
                if msg.type == WSMsgType.TEXT:
                    await downstream.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await downstream.send_bytes(msg.data)

        tasks = [asyncio.create_task(down_to_up()), asyncio.create_task(up_to_down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for ws_conn in (upstream, downstream):
                if not ws_conn.closed:
                    try:
                        await ws_conn.close()
                    except Exception:
                        pass
        return downstream

    async def redirect(request: web.Request):
        raise web.HTTPPermanentRedirect(f"{PANEL_PREFIX}/")

    async def legacy(request: web.Request):
        raise web.HTTPPermanentRedirect(f"{PANEL_PREFIX}/" + (("?" + request.query_string) if request.query_string else ""))

    async def bridge_ws(request: web.Request):
        if not is_web_authed(hub, request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        bcfg, error = bridge_settings()
        if error:
            return web.json_response({"ok": False, "error": error}, status=503)
        if session is None:
            return web.json_response({"ok": False, "error": "proxy session unavailable"}, status=502)
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return web.json_response({"ok": False, "error": "websocket required"}, status=400)
        upstream_url = bcfg["base"] + bcfg["path"]
        if request.query_string:
            upstream_url += "?" + request.query_string
        headers = {k: v for k, v in request.headers.items() if k.lower() in SAFE_REQUEST_HEADERS}
        return await _relay_ws(request, upstream_url, headers, "Bridge WebSocket 不可达")

    async def proxy(request: web.Request):
        if request.path == f"{PANEL_PREFIX}/" and request.method == "GET" and request.query.get("token") and hub.token_ok(request.query["token"]):
            query = [(k, v) for k, v in request.query.items() if k != "token"]
            response = web.HTTPFound(f"{PANEL_PREFIX}/" + (("?" + urlencode(query)) if query else ""))
            set_auth_cookie(response, hub, request)
            raise response

        tail = request.match_info.get("tail", "")
        is_api = tail.startswith("api/") or tail == "api"

        if not is_web_authed(hub, request):
            if is_api:
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
            return web.Response(
                text=make_auth_gate_html(request.path_qs),
                status=401,
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        config, error, help_text = settings()
        if error:
            if is_api:
                return web.json_response({"ok": False, "error": error}, status=503)
            return web.Response(text=_notice_html(error, help_text), status=503, content_type="text/html", headers={"Cache-Control": "no-store"})

        if session is None:
            if is_api:
                return web.json_response({"ok": False, "error": "proxy session unavailable"}, status=502)
            return web.Response(text=_notice_html("管理面板代理尚未就绪"), status=502, content_type="text/html", headers={"Cache-Control": "no-store"})

        upstream_url = config["base"] + "/" + tail
        if request.query_string:
            upstream_url += "?" + request.query_string
        headers = {k: v for k, v in request.headers.items() if k.lower() in SAFE_REQUEST_HEADERS}
        if config["token"]:
            headers["Authorization"] = "Bearer " + config["token"]

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _relay_ws(request, upstream_url, headers, "管理面板 WebSocket 不可达")

        body = await request.read() if request.method not in ("GET", "HEAD") else None
        try:
            async with session.request(request.method, upstream_url, headers=headers, data=body, allow_redirects=False) as upstream:
                out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in SAFE_RESPONSE_HEADERS}
                content_type = upstream.headers.get("Content-Type", "")
                if not is_api and content_type.lower().startswith("text/html"):
                    payload = await upstream.read()
                    rewritten = rewrite_panel_html(payload.decode(upstream.charset or "utf-8", "replace"), PANEL_PREFIX)
                    out_headers["Cache-Control"] = "no-store"
                    return web.Response(body=rewritten.encode("utf-8"), status=upstream.status, headers=out_headers)
                stream = web.StreamResponse(status=upstream.status, headers=out_headers)
                await stream.prepare(request)
                if request.method != "HEAD":
                    async for chunk in upstream.content.iter_chunked(16384):
                        await stream.write(chunk)
                await stream.write_eof()
                return stream
        except (ClientError, OSError, asyncio.TimeoutError):
            if is_api:
                return web.json_response({"ok": False, "error": f"bothouse 面板不可达 ({config['base']})"}, status=502)
            return web.Response(
                text=_notice_html(f"bothouse 没在运行或不可达：{config['base']}", "请确认 BothouseDiscordCodex-Official 正在运行后点「重试」。"),
                status=502,
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

    gate_base = os.environ.get("RH_GATE_URL", "http://127.0.0.1:9930").rstrip("/")

    async def gate_proxy(request: web.Request):
        if not hub.bot_token_ok(request.match_info.get("bot_tok", "")):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        tail = request.match_info.get("tail", "").lstrip("/")
        if tail not in GATE_ALLOWED_PATHS:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        if session is None:
            return web.json_response({"ok": False, "error": "proxy session unavailable"}, status=502)
        upstream_url = f"{gate_base}/{tail}"
        if request.query_string:
            upstream_url += "?" + request.query_string
        headers = {}
        if request.headers.get("Content-Type"):
            headers["Content-Type"] = request.headers["Content-Type"]
        body = await request.read() if request.method not in ("GET", "HEAD") else None
        try:
            async with session.request(
                request.method,
                upstream_url,
                headers=headers,
                data=body,
                timeout=ClientTimeout(total=5.0),
                allow_redirects=False,
            ) as upstream:
                payload = await upstream.read()
                out_headers = {}
                if upstream.headers.get("Content-Type"):
                    out_headers["Content-Type"] = upstream.headers["Content-Type"]
                return web.Response(body=payload, status=upstream.status, headers=out_headers)
        except (ClientError, OSError, asyncio.TimeoutError):
            return web.json_response({"ok": False, "error": "gate unreachable"}, status=502)

    async def startup(app: web.Application) -> None:
        nonlocal session
        session = ClientSession()

    async def cleanup(app: web.Application) -> None:
        if session is not None:
            await session.close()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/panel", redirect)
    app.router.add_get("/panel/", redirect)
    app.router.add_get("/bridge/ws", bridge_ws)
    bcfg, _ = bridge_settings()
    if bcfg and bcfg["path"] != "/bridge/ws":
        app.router.add_get(bcfg["path"], bridge_ws)
    app.router.add_route("*", "/gate/{bot_tok}/{tail:.*}", gate_proxy)
    app.router.add_get(PANEL_PREFIX, legacy)
    app.router.add_route("*", f"{PANEL_PREFIX}/", proxy)
    app.router.add_route("*", f"{PANEL_PREFIX}/{{tail:.*}}", proxy)
