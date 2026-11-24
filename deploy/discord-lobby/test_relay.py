"""Integration tests against the official hub with a local Discord fixture."""

import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from relay import API, Relay, outbound_payload
from rh.hub import Hub, make_app


GUILD = "10000000000000001"
CHANNEL = "10000000000000002"
WORKSPACE = "10000000000000003"
UNEXPECTED = "10000000000000004"
BOT = "10000000000000005"
PARENT = "10000000000000006"


class DiscordFixture:
    def __init__(self):
        self.sent = asyncio.Queue()
        self.history = []

    async def call(self, method, path, **kwargs):
        if method == "POST" and path == f"/channels/{CHANNEL}/messages":
            await self.sent.put(kwargs["json"])
            return {"id": "10000000000000007"}
        if method == "GET" and path.endswith("/messages"):
            return self.history
        if method == "GET" and path == f"/channels/{WORKSPACE}":
            return {"guild_id": GUILD, "type": 0, "parent_id": PARENT, "name": "\u5de5\u4f5c\u533a1"}
        if method == "GET" and path == f"/channels/{UNEXPECTED}":
            return {"guild_id": GUILD, "type": 0, "parent_id": PARENT, "name": "other-project"}
        raise AssertionError((method, path))


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hub = Hub(":memory:", "web-test", "bot-test")
        self.server = TestServer(make_app(self.hub))
        self.database = sqlite3.connect(":memory:")

    async def asyncTearDown(self):
        await self.server.close()
        self.hub.db.close()
        self.database.close()

    async def test_forward_mirror_edit_and_discovery(self):
        await self.server.start_server()
        origin = str(self.server.make_url("")).rstrip("/")
        fixture = DiscordFixture()
        cfg = {"guild": GUILD, "channel": CHANNEL, "target_bot": BOT, "room": 1}
        async with aiohttp.ClientSession() as session:
            hub = API(session, origin, "web-test", "Bearer")
            async with session.ws_connect(
                origin + "/cloud-web/ws?bot=ll-discord&name=ll-discord",
                headers={"Authorization": "Bearer bot-test"},
            ) as ws:
                await ws.send_json({"type": "register", "project": "ll-discord"})
                self.assertTrue((await ws.receive_json())["ok"])
                relay = Relay(cfg, fixture, hub, {"parent_id": PARENT}, self.database)
                await relay.initialize()
                receiver = asyncio.create_task(relay.receive(ws))
                forwarder = asyncio.create_task(relay.forward(ws))
                try:
                    await hub.call("POST", "/api/rooms/1/messages", json={"text": "@ll-discord example"})
                    forwarded = await asyncio.wait_for(fixture.sent.get(), 3)
                    self.assertEqual(forwarded["content"], f"<@{BOT}> example")
                    self.assertEqual(forwarded["allowed_mentions"]["users"], [BOT])
                    forged = {"id": "10000000000000008", "content": "not ll", "author": {"id": UNEXPECTED}}
                    await relay.mirror(ws, CHANNEL, forged)
                    message = {
                        "id": "10000000000000009", "author": {"id": BOT},
                        "content": f"Working in <#{WORKSPACE}> <#{UNEXPECTED}>",
                        "embeds": [{"title": "Task #42", "fields": [{"name": "Status", "value": "Working"}]}],
                    }
                    await relay.mirror(ws, CHANNEL, message)
                    message["embeds"][0]["fields"][0]["value"] = "Returned"
                    await relay.mirror(ws, CHANNEL, message)
                    await relay.mirror(ws, CHANNEL, message)
                    self.assertIn(WORKSPACE, relay.rooms)
                    self.assertNotIn(UNEXPECTED, relay.rooms)
                    for _ in range(20):
                        history = (await hub.call("GET", "/api/rooms/1/messages"))["messages"]
                        if any("Returned" in msg["text"] for msg in history):
                            break
                        await asyncio.sleep(0.05)
                    self.assertEqual(len(history), 2)
                    self.assertIn("Task #42", history[-1]["text"])
                    self.assertIn("Returned", history[-1]["text"])
                    self.assertEqual(history[-1]["author"], "ll-discord")
                    self.assertTrue(fixture.sent.empty())
                finally:
                    receiver.cancel()
                    forwarder.cancel()
                    await asyncio.gather(receiver, forwarder, return_exceptions=True)

    def test_outbound_respects_discord_character_limit(self):
        with self.assertRaisesRegex(ValueError, "2,000"):
            outbound_payload({"msg_id": "hub-1", "content": "\U0001f642" * 1000}, BOT)

    async def test_discord_edit_is_mirrored_after_restart(self):
        fixture = DiscordFixture()
        cfg = {"guild": GUILD, "channel": CHANNEL, "target_bot": BOT, "room": 1}
        relay = Relay(cfg, fixture, None, {"parent_id": PARENT}, self.database)
        await relay.initialize()
        relay.publish = AsyncMock()
        message = {
            "id": "10000000000000009", "author": {"id": BOT},
            "content": "Task #42: working",
        }
        fixture.history = [message]
        await relay.poll_channel(None, CHANNEL)
        relay.publish.assert_awaited_once()

        restarted = Relay(cfg, fixture, None, {"parent_id": PARENT}, self.database)
        restarted.publish = AsyncMock()
        message["content"] = "Task #42: returned"
        await restarted.poll_channel(None, CHANNEL)
        restarted.publish.assert_awaited_once()
        self.assertIn("Task #42: returned", restarted.publish.await_args.args[-1])

        fixture.history = [dict(message, id="10000000000000008")]
        await restarted.poll_channel(None, CHANNEL)
        self.assertEqual(restarted.publish.await_count, 1)
        cursor = self.database.execute("SELECT cursor FROM rooms WHERE channel=?", (CHANNEL,)).fetchone()[0]
        self.assertEqual(cursor, message["id"])

    async def test_discord_retry_reuses_nonce(self):
        requests = []

        async def send(request):
            requests.append(await request.json())
            if len(requests) == 1:
                return web.Response(status=502)
            return web.json_response({"id": "10000000000000007"})

        app = web.Application()
        app.router.add_post("/messages", send)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            base = str(server.make_url("/")).rstrip("/")
            discord = API(session, base, "bot-test", "Bot")
            payload = outbound_payload({"msg_id": "hub-1", "content": "example"}, BOT)
            result = await discord.call("POST", "/messages", retry_transient=True, json=payload)
        self.assertEqual(result["id"], "10000000000000007")
        self.assertEqual(requests, [payload, payload])
        self.assertTrue(payload["enforce_nonce"])


if __name__ == "__main__":
    unittest.main()
