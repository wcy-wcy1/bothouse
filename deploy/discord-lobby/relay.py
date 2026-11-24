"""Connect one Bothouse lobby room to a Discord channel through an existing bot."""

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import tomllib
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import getproxies

import aiohttp

log = logging.getLogger("discord-lobby")
DISCORD_API = "https://discord.com/api/v10"
SNOWFLAKE = re.compile(r"[0-9]{17,20}")
WORKSPACE = re.compile(r"\u5de5\u4f5c\u533a[1-9][0-9]*")
CHANNEL_MENTION = re.compile(r"<#([0-9]{17,20})>")
CHANNEL_LINK = re.compile(r"https://discord\.com/channels/([0-9]{17,20})/([0-9]{17,20})")


class APIError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class API:
    def __init__(self, session, base, token, scheme, proxy=None):
        self.session = session
        self.base = base
        self.headers = {"Authorization": f"{scheme} {token}"}
        self.proxy = proxy

    async def call(self, method, path, **kwargs):
        for attempt in range(4):
            async with self.session.request(
                method, self.base + path, headers=self.headers, proxy=self.proxy,
                allow_redirects=False, **kwargs,
            ) as response:
                if response.status == 429 and attempt < 3:
                    wait = float((await response.json()).get("retry_after", 1))
                elif 200 <= response.status < 300:
                    return await response.json() if response.status != 204 else None
                else:
                    raise APIError(f"{method} {path} returned HTTP {response.status}", response.status)
            await asyncio.sleep(min(max(wait, 0.1), 120))
        raise AssertionError("unreachable")


def load_config(path):
    cfg = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    hub = urlsplit(cfg["hub_url"])
    if hub.scheme != "http" or hub.hostname != "127.0.0.1" or hub.path or hub.query or hub.fragment:
        raise ValueError("hub_url must be a loopback HTTP origin")
    for key in ("guild", "channel", "sender_bot", "target_bot"):
        if not SNOWFLAKE.fullmatch(cfg[key]):
            raise ValueError(f"{key} must be a Discord ID")
    if cfg["sender_bot"] == cfg["target_bot"]:
        raise ValueError("sender_bot and target_bot must be different")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", cfg["alias"]):
        raise ValueError("alias must be a lowercase, single-word role name")
    cfg.setdefault("bot_id", cfg["alias"])
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", cfg["bot_id"]):
        raise ValueError("bot_id must be a lowercase, single-word role ID")
    if not isinstance(cfg["room"], int) or cfg["room"] < 1:
        raise ValueError("room must be a positive lobby room ID")
    return cfg


def credentials(cfg):
    import win32crypt

    credential = Path(os.path.expandvars(cfg["sender_credential"]))
    if not credential.is_file():
        raise ValueError("Discord bot credential file is unavailable")
    secret = win32crypt.CryptUnprotectData(credential.read_bytes(), None, None, None, 0)[1]
    hub_state = Path(os.path.expandvars(cfg["hub_state"]))
    if not hub_state.is_file():
        raise ValueError("Bothouse hub state file is unavailable")
    return secret.decode(), json.loads(hub_state.read_text(encoding="utf-8-sig"))


def discord_text(message):
    sections = [message.get("content", "")]
    for embed in message.get("embeds", []):
        sections.extend(embed.get(key, "") for key in ("title", "description", "url"))
        for field in embed.get("fields", []):
            sections.append(f"**{field.get('name', '')}**\n{field.get('value', '')}")
    for attachment in message.get("attachments", []):
        url = attachment.get("url", "")
        if urlsplit(url).hostname in {"cdn.discordapp.com", "media.discordapp.net"}:
            sections.append(url)
    return "\n\n".join(s for s in sections if s).strip()[:16000]


def outbound_payload(frame, target):
    message = frame.get("content", "").strip()
    if not message:
        raise ValueError("Message body is empty")
    content = f"<@{target}> {message}"
    if len(content.encode("utf-16-le")) // 2 > 2000:
        raise ValueError("Discord accepts up to 2,000 characters per message")
    return {
        "content": content,
        "allowed_mentions": {"parse": [], "users": [target], "replied_user": False},
        "nonce": f"bh-{frame['msg_id']}",
        "enforce_nonce": True,
    }


class Relay:
    def __init__(self, cfg, discord, hub, channel, database):
        self.cfg, self.discord, self.hub, self.channel = cfg, discord, hub, channel
        self.store = database
        self.store.executescript("""
            CREATE TABLE IF NOT EXISTS sent (hub_id TEXT PRIMARY KEY, discord_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rooms (channel TEXT PRIMARY KEY, room INTEGER UNIQUE, cursor TEXT NOT NULL);
        """)
        self.rooms = {row[0]: row[1] for row in self.store.execute("SELECT channel, room FROM rooms")}
        mismatch = self.rooms.get(cfg["channel"]) not in (None, cfg["room"])
        if mismatch or cfg["room"] in {v for k, v in self.rooms.items() if k != cfg["channel"]}:
            raise ValueError("state database belongs to another bridge configuration")
        self.seen = {}
        self.acks = {}
        self.inbox = asyncio.Queue(maxsize=128)

    def record_room(self, channel, room, cursor):
        with self.store:
            self.store.execute("INSERT OR IGNORE INTO rooms VALUES(?, ?, ?)", (channel, room, cursor))
        self.rooms[channel] = room

    async def initialize(self):
        channel = self.cfg["channel"]
        if channel not in self.rooms:
            last = await self.discord.call("GET", f"/channels/{channel}/messages", params={"limit": 1})
            self.record_room(channel, self.cfg["room"], last[0]["id"] if last else "0")

    async def discover_rooms(self, text):
        cfg = self.cfg
        candidates = set(CHANNEL_MENTION.findall(text))
        candidates.update(channel for guild, channel in CHANNEL_LINK.findall(text) if guild == cfg["guild"])
        for channel in sorted(candidates - self.rooms.keys())[:8]:
            try:
                candidate = await self.discord.call("GET", f"/channels/{channel}")
            except APIError:
                continue
            if (
                candidate.get("guild_id") != cfg["guild"] or candidate.get("type") != 0
                or candidate.get("parent_id") != self.channel.get("parent_id")
                or not WORKSPACE.fullmatch(candidate.get("name", ""))
            ):
                continue
            name = f"Discord {candidate['name']} ({channel[-6:]})"
            rooms = await self.hub.call("GET", "/api/rooms")
            room = next((r for r in rooms if r["name"] == name), None)
            if room is None:
                room = await self.hub.call("POST", "/api/rooms", json={"name": name})
            self.record_room(channel, room["id"], "0")

    async def publish(self, ws, room, reference, text):
        future = asyncio.get_running_loop().create_future()
        self.acks[reference] = future
        try:
            await ws.send_json({
                "type": "preview_start", "ref_id": reference,
                "reply_ctx": f"room-{room}", "content": text,
            })
            handle = await asyncio.wait_for(future, 12)
            if not handle:
                raise APIError(f"Lobby rejected the preview in room {room}")
            await ws.send_json({"type": "update_message", "preview_handle": handle, "content": text})
        finally:
            self.acks.pop(reference, None)

    async def mirror(self, ws, channel, message):
        if message.get("webhook_id") or message.get("author", {}).get("id") != self.cfg["target_bot"]:
            return
        reference = f"discord-{message['id']}"
        text = discord_text(message)
        if not text or self.seen.get(reference) == text:
            return
        if channel == self.cfg["channel"]:
            await self.discover_rooms(text)
        link = f"https://discord.com/channels/{self.cfg['guild']}/{channel}/{message['id']}"
        await self.publish(ws, self.rooms[channel], reference, f"{text}\n\n[Discord]({link})")
        self.seen[reference] = text
        if len(self.seen) > 1000:
            self.seen.pop(next(iter(self.seen)))

    async def poll(self, ws):
        while True:
            for channel in list(self.rooms):
                cursor = self.store.execute("SELECT cursor FROM rooms WHERE channel=?", (channel,)).fetchone()[0]
                path = f"/channels/{channel}/messages"
                try:
                    page = await self.discord.call("GET", path, params={"limit": 50})
                except APIError as exc:
                    if channel == self.cfg["channel"] or exc.status not in (403, 404):
                        raise
                    log.warning("Skipping unavailable Discord workspace %s", channel)
                    continue
                latest_id = max((int(m["id"]) for m in page), default=int(cursor))
                pending = [page]
                while len(page) == 50 and min(int(m["id"]) for m in page) > int(cursor) and cursor != "0":
                    before = min((m["id"] for m in page), key=int)
                    page = await self.discord.call("GET", path, params={"limit": 50, "before": before})
                    pending.append(page)
                for batch in reversed(pending):
                    for message in sorted(batch, key=lambda m: int(m["id"])):
                        if int(message["id"]) > int(cursor) or f"discord-{message['id']}" in self.seen:
                            await self.mirror(ws, channel, message)
                with self.store:
                    self.store.execute("UPDATE rooms SET cursor=? WHERE channel=?", (str(latest_id), channel))
            await asyncio.sleep(5)

    async def receive(self, ws):
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            frame = message.json()
            if frame.get("type") == "preview_ack":
                future = self.acks.get(frame.get("ref_id"))
                if future is not None and not future.done():
                    future.set_result(frame.get("preview_handle"))
            elif (
                frame.get("type") == "message" and frame.get("mentioned") is True
                and frame.get("from_bot") is False and frame.get("user_id") == "user"
                and re.fullmatch(r"hub-[1-9][0-9]*", frame.get("msg_id", ""))
            ):
                try:
                    self.inbox.put_nowait(frame)
                except asyncio.QueueFull:
                    log.error("Lobby message queue full: %s", frame["msg_id"])
        raise ConnectionError("Lobby connection closed")

    async def forward(self, ws):
        while True:
            frame = await self.inbox.get()
            room = next((k for k, v in self.rooms.items() if frame.get("chat_id") == f"room-{v}"), None)
            if room is None or self.store.execute("SELECT 1 FROM sent WHERE hub_id=?", (frame["msg_id"],)).fetchone():
                continue
            try:
                payload = outbound_payload(frame, self.cfg["target_bot"])
                sent = await self.discord.call("POST", f"/channels/{room}/messages", json=payload)
            except (ValueError, aiohttp.ClientError, TimeoutError, APIError) as exc:
                detail = str(exc) if isinstance(exc, (ValueError, APIError)) else type(exc).__name__
                await self.publish(ws, self.rooms[room], f"error-{frame['msg_id']}", f"Discord forwarding failed. {detail}")
                continue
            with self.store:
                self.store.execute("INSERT INTO sent VALUES(?, ?)", (frame["msg_id"], sent["id"]))
            log.info("Discord accepted %s as %s", frame["msg_id"], sent["id"])

    async def run(self, session, hub_token):
        cfg = self.cfg
        await self.initialize()
        params = {"bot": cfg["bot_id"], "name": cfg["alias"]}
        while True:
            try:
                async with session.ws_connect(
                    cfg["hub_url"] + "/cloud-web/ws", params=params, heartbeat=25,
                    headers={"Authorization": f"Bearer {hub_token}"},
                ) as ws:
                    await ws.send_json({"type": "register", "project": cfg["bot_id"]})
                    ack = await ws.receive_json(timeout=10)
                    if ack.get("type") != "register_ack" or not ack.get("ok"):
                        raise APIError("Lobby rejected relay registration")
                    log.info("Lobby role %s connected to Discord channel %s", cfg["alias"], cfg["channel"])
                    tasks = [asyncio.create_task(coro(ws)) for coro in (self.receive, self.forward, self.poll)]
                    try:
                        first, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in first:
                            task.result()
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            except (APIError, aiohttp.ClientError, TimeoutError, OSError) as exc:
                detail = str(exc) if isinstance(exc, APIError) else type(exc).__name__
                log.warning("Relay disconnected: %s", detail)
            await asyncio.sleep(5)


async def main(cfg, state, check):
    token, hub = credentials(cfg)
    proxy = getproxies().get("https")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        discord = API(session, DISCORD_API, token, "Bot", proxy)
        lobby = API(session, cfg["hub_url"], hub["web_token"], "Bearer")
        identity = await discord.call("GET", "/users/@me")
        if identity.get("id") != cfg["sender_bot"] or not identity.get("bot"):
            raise ValueError("Unexpected Discord bot identity")
        channel = await discord.call("GET", f"/channels/{cfg['channel']}")
        if channel.get("guild_id") != cfg["guild"] or channel.get("type") != 0:
            raise ValueError("Discord channel does not match configured server and channel type")
        await discord.call("GET", f"/channels/{cfg['channel']}/messages", params={"limit": 1})
        rooms = await lobby.call("GET", "/api/rooms")
        if cfg["room"] not in {room["id"] for room in rooms if room["kind"] == "chat"}:
            raise ValueError("Configured lobby room is missing or archived")
        log.info("Verified Discord bot, channel history, and lobby room %s", cfg["room"])
        if not check:
            state.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(state) as database:
                await Relay(cfg, discord, lobby, channel, database).run(session, hub["bot_token"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--check", action="store_true", help="verify access without registering or posting")
    args = parser.parse_args()
    handlers = None
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        handlers = [RotatingFileHandler(args.log, maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    try:
        asyncio.run(main(load_config(args.config), args.state, args.check))
    except (APIError, ValueError, KeyError, aiohttp.ClientError, TimeoutError, OSError) as exc:
        detail = str(exc) if isinstance(exc, (APIError, ValueError, KeyError)) else type(exc).__name__
        log.error("Relay startup failed: %s", detail)
        raise SystemExit(1) from None
