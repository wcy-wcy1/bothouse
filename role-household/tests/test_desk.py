import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rh.cloudweb import PROGRESS_PREFIX, card_from
from rh.hub import Hub, make_app
from rh.panel_proxy import _is_loopback_ip


def _create_t3_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE orchestration_v2_projection_threads (
            thread_id TEXT PRIMARY KEY,
            title TEXT
        );
        CREATE TABLE orchestration_v2_projection_runs (
            run_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE orchestration_v2_projection_subagents (
            subagent_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            run_id TEXT,
            child_thread_id TEXT,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT
        );
    """)
    now = datetime.now(timezone.utc)
    t_sub_done = (now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    t_sub_done_end = (now - timedelta(minutes=18)).isoformat().replace("+00:00", "Z")
    t_sub_run = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?)", ("main-thread", "主控会话"))
    conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?)", ("child-done", "历史完成子任务"))
    conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?)", ("child-live", "实时运行子任务"))
    conn.execute(
        "INSERT INTO orchestration_v2_projection_runs VALUES (?, ?, ?, ?, ?)",
        ("run-main", "main-thread", "running", t_sub_run, None),
    )
    conn.execute(
        "INSERT INTO orchestration_v2_projection_runs VALUES (?, ?, ?, ?, ?)",
        ("run-child-live", "child-live", "running", t_sub_run, None),
    )
    conn.execute(
        "INSERT INTO orchestration_v2_projection_subagents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("sub-done", "main-thread", "run-main", "child-done", "completed", t_sub_done, t_sub_done_end, t_sub_done_end),
    )
    conn.execute(
        "INSERT INTO orchestration_v2_projection_subagents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("sub-live", "main-thread", "run-main", "child-live", "running", t_sub_run, None, t_sub_run),
    )
    conn.commit()
    conn.close()


class DeskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.t3_db = self.root / "statev2.sqlite"
        _create_t3_db(self.t3_db)
        self._prev_t3 = os.environ.get("RH_T3_STATE_DB")
        self._prev_panel = os.environ.get("RH_PANEL_CONFIG")
        self._prev_wiki = os.environ.get("RH_WIKI_DIR")
        self._prev_gate = os.environ.get("RH_GATE_URL")
        self._prev_ollama = os.environ.get("RH_OLLAMA_URL")
        os.environ["RH_T3_STATE_DB"] = str(self.t3_db)
        os.environ.pop("RH_PANEL_CONFIG", None)
        os.environ.pop("RH_WIKI_DIR", None)
        os.environ.pop("RH_GATE_URL", None)
        os.environ.pop("RH_OLLAMA_URL", None)
        self.hub = Hub(self.root / "hub.sqlite3", "test-token")
        self.app = make_app(self.hub)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.hub.db.close()
        if self._prev_t3 is None:
            os.environ.pop("RH_T3_STATE_DB", None)
        else:
            os.environ["RH_T3_STATE_DB"] = self._prev_t3
        if self._prev_panel is None:
            os.environ.pop("RH_PANEL_CONFIG", None)
        else:
            os.environ["RH_PANEL_CONFIG"] = self._prev_panel
        if self._prev_wiki is None:
            os.environ.pop("RH_WIKI_DIR", None)
        else:
            os.environ["RH_WIKI_DIR"] = self._prev_wiki
        if self._prev_gate is None:
            os.environ.pop("RH_GATE_URL", None)
        else:
            os.environ["RH_GATE_URL"] = self._prev_gate
        if self._prev_ollama is None:
            os.environ.pop("RH_OLLAMA_URL", None)
        else:
            os.environ["RH_OLLAMA_URL"] = self._prev_ollama
        self.temp.cleanup()

    async def test_cookie_auth_and_desk_routes(self):
        for path in ("/collab/api/state", "/collab/api/desk/state", "/collab/api/desk/replay?from=x&to=y", "/collab/api/desk/names", "/collab/panel/api/v1/status"):
            self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.put("/collab/api/desk/names", json={"id": "x", "name": "y"})).status, 401)
        self.assertEqual((await self.client.post("/api/auth/cookie", json={"token": "bad"})).status, 401)

        response = await self.client.post("/api/auth/cookie", json={"token": "test-token"})
        self.assertEqual(response.status, 200)
        self.assertIn("httponly", response.headers.get("Set-Cookie", "").lower())
        self.assertEqual((await self.client.get("/collab/api/state")).status, 200)

        redir = await self.client.get("/collab/desk?token=test-token", allow_redirects=False)
        self.assertEqual(redir.status, 302)
        self.assertEqual(redir.headers["Location"], "/collab/desk")

        page = await self.client.get("/collab/desk")
        self.assertEqual(page.status, 200)
        html = await page.text()
        self.assertIn('id="office"', html)
        self.assertIn("t3-antigravity", html)

    async def test_names_and_avatar(self):
        headers = {"Authorization": "Bearer test-token"}
        av = await self.client.get("/collab/avatars/orange.png", headers=headers)
        self.assertEqual(av.status, 200)
        self.assertEqual(av.headers.get("Content-Type"), "image/png")
        self.assertEqual((await self.client.get("/collab/avatars/..%2Fhub.py", headers=headers)).status, 404)

        result = await self.client.put("/collab/api/desk/names", json={"id": "local-codex", "name": "小码"}, headers=headers)
        self.assertEqual(result.status, 200)
        self.assertEqual((await result.json())["names"]["local-codex"], "小码")
        names_resp = await self.client.get("/collab/api/desk/names", headers=headers)
        self.assertEqual((await names_resp.json())["names"]["local-codex"], "小码")
        self.assertEqual((await self.client.put("/collab/api/desk/names", json={"id": "x", "name": "x" * 25}, headers=headers)).status, 400)
        cleared = await self.client.put("/collab/api/desk/names", json={"id": "local-codex", "name": ""}, headers=headers)
        self.assertNotIn("local-codex", (await cleared.json())["names"])

    async def test_t3_live_polling_lobby_isolation_and_replay(self):
        headers = {"Authorization": "Bearer test-token"}
        await asyncio.sleep(0.2)

        state = await (await self.client.get("/collab/api/state", headers=headers)).json()
        member_ids = [m["id"] for m in state["members"]]
        self.assertIn("t3-antigravity", member_ids)
        self.assertIn("t3-codex", member_ids)

        desk_state = await (await self.client.get("/collab/api/desk/state", headers=headers)).json()
        self.assertEqual([t["id"] for t in desk_state["tasks"]], ["sub-live"])
        self.assertEqual(desk_state["tasks"][0]["title"], "实时运行子任务")

        # Dispatch a Lobby task from remote-ll -> local-codex and ensure T3 polls do NOT evict it.
        room_id = self.hub.first_room()
        msg1, _ = self.hub.create(room_id, {
            "author_kind": "role",
            "author": "remote-ll",
            "name": "小橘",
            "text": "@codex 请跑一下测试\n\n*claude · out 10*",
        })
        await self.hub.notify_new(msg1)
        desk_state_with_lobby = await (await self.client.get("/collab/api/desk/state", headers=headers)).json()
        task_ids = {t["id"] for t in desk_state_with_lobby["tasks"]}
        self.assertIn(f"lobby-{msg1['id']}", task_ids)
        self.assertIn("sub-live", task_ids)

        # Open SSE stream and complete the running T3 subagent; verify returned + done arrive cleanly.
        sse_resp = await self.client.get("/collab/api/events", headers=headers)
        self.assertEqual(sse_resp.status, 200)
        try:
            conn = sqlite3.connect(str(self.t3_db))
            done_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            conn.execute(
                "UPDATE orchestration_v2_projection_subagents SET status='completed', completed_at=? WHERE subagent_id='sub-live'",
                (done_at,),
            )
            conn.commit()
            conn.close()

            kinds = []
            deadline = asyncio.get_running_loop().time() + 4.0
            while asyncio.get_running_loop().time() < deadline:
                raw_line = await asyncio.wait_for(sse_resp.content.readline(), timeout=2.5)
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", "replace").strip()
                if line.startswith("data: "):
                    payload = json.loads(line[6:])
                    if payload.get("type") == "task" and payload.get("id") == "sub-live":
                        kinds.append(payload.get("kind"))
                        if "done" in kinds:
                            break
            self.assertEqual(kinds, ["returned", "done"])
        finally:
            sse_resp.close()

        # Verify Lobby completion & merged chronological replay
        msg2, _ = self.hub.create(room_id, {
            "author_kind": "role",
            "author": "local-codex",
            "name": "codex",
            "text": "@小橘 测试已全部通过！",
        })
        await self.hub.notify_new(msg2)

        now = datetime.now(timezone.utc)
        q_from = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        q_to = (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        replay_resp = await self.client.get("/collab/api/desk/replay", params={"from": q_from, "to": q_to}, headers=headers)
        self.assertEqual(replay_resp.status, 200)
        replay_data = await replay_resp.json()
        events = replay_data["events"]
        self.assertTrue(any(e.get("type") == "message" and e["message"]["author"] == "remote-ll" for e in events))
        self.assertTrue(any(e.get("type") == "task" and e.get("id") == "sub-done" and e.get("kind") == "assigned" for e in events))
        self.assertTrue(any(e.get("type") == "task" and e.get("id") == "sub-done" and e.get("kind") == "done" for e in events))
        self.assertTrue(any(e.get("type") == "task" and e.get("from") == "remote-ll" and e.get("to") == "local-codex" and e.get("kind") == "assigned" for e in events))

        bad_replay = await self.client.get("/collab/api/desk/replay", params={"from": q_to, "to": q_from}, headers=headers)
        self.assertEqual(bad_replay.status, 400)

    async def test_panel_proxy(self):
        captured = {}
        upstream = web.Application()

        async def page(request):
            return web.Response(text="<html><head><base href=\"/\" /></head><body>ok</body></html>", content_type="text/html")

        async def api(request):
            captured.update({"authorization": request.headers.get("Authorization"), "cookie": request.headers.get("Cookie")})
            return web.json_response({"ok": True})

        upstream.router.add_get("/", page)
        upstream.router.add_get("/api/v1/test", api)
        server = TestServer(upstream)
        await server.start_server()
        try:
            config = self.root / "panel.toml"
            config.write_text(f'[management]\nenabled=true\nhost="127.0.0.1"\nport={server.port}\ntoken="mgmt-token"\n', encoding="utf-8")
            await self.client.close()
            os.environ["RH_PANEL_CONFIG"] = str(config)
            self.app = make_app(self.hub)
            self.client = TestClient(TestServer(self.app))
            await self.client.start_server()

            headers = {"Authorization": "Bearer test-token", "Cookie": "browser-secret=1"}
            redirect = await self.client.get("/collab/panel", headers=headers, allow_redirects=False)
            self.assertEqual(redirect.status, 308)
            self.assertEqual(redirect.headers["Location"], "/collab/panel/")

            html = await (await self.client.get("/collab/panel/", headers=headers)).text()
            self.assertIn('<base href="/collab/panel/" />', html)
            self.assertIn('<meta name="bothouse-auth" content="proxy" />', html)

            result = await self.client.get("/collab/panel/api/v1/test", headers=headers)
            self.assertEqual(result.status, 200)
            self.assertEqual(captured["authorization"], "Bearer mgmt-token")
            self.assertIsNone(captured["cookie"])

            # Verify clean 502 error handling when upstream closes
            await server.close()
            down_api = await self.client.get("/collab/panel/api/v1/test", headers=headers)
            self.assertEqual(down_api.status, 502)
        finally:
            await server.close()

    def test_progress_card_cleaning(self):
        payload = {
            "agent": "Codex",
            "state": "completed",
            "steps": 13,
            "elapsed_sec": 382,
            "items": [
                {"kind": "tool_use", "tool": "Bash", "text": '"C:\\Program Files\\PowerShell\\7\\pwsh.exe" -Command \'git add -- slides\''},
                {"kind": "tool_result", "tool": "Bash", "status": "completed", "exit_code": 0, "text": "🧾 Bash 🟢 状态: completed 🔢 退出码: 0 _无输出_"},
                {"kind": "tool_result", "tool": "Bash", "status": "failed", "exit_code": 1, "text": "🧾 Bash 🔴 状态: failed 🔢 退出码: 1 _无输出_"},
                {"kind": "tool_result", "tool": "Bash", "status": "completed", "exit_code": 0, "text": "warning: in the working copy of 'a.py', LF will be replaced by CRLF the next time Git touches it [main abc123] ok"},
                {"kind": "tool_use", "tool": "PowerShell", "text": '{"command":"dir","description":"List files"}'},
            ],
        }
        text, meta_json = card_from(PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False))
        lines = text.splitlines()
        self.assertEqual(lines[0], "第 13 步 · 6分22秒")
        self.assertEqual(lines[1], "🔧 git add -- slides")
        self.assertEqual(lines[2], "✓ Bash")
        self.assertEqual(lines[3], "❌ Bash (exit 1)")
        self.assertIn("[main abc123] ok", lines[4])
        self.assertNotIn("LF will be replaced by CRLF", lines[4])
        self.assertEqual(lines[5], "🔧 List files")
        self.assertEqual(json.loads(meta_json)["state"], "completed")

    async def test_dispatch_mode_and_workspace_inheritance(self):
        headers = {"Authorization": "Bearer test-token"}
        room_id = self.hub.first_room()
        ws_room = self.hub.create_workspace(room_id)

        bad = await self.client.patch(f"/api/rooms/{room_id}", json={"dispatch_mode": "bogus"}, headers=headers)
        self.assertEqual(bad.status, 400)

        patched = await self.client.patch(f"/api/rooms/{room_id}", json={"dispatch_mode": "team"}, headers=headers)
        self.assertEqual(patched.status, 200)
        self.assertEqual((await patched.json())["dispatch_mode"], "team")
        self.assertEqual(self.hub.dispatch_setting(ws_room["id"]), ("team", True))

        solo_res = await self.client.post(f"/api/rooms/{room_id}/messages", json={"text": "/solo"}, headers=headers)
        self.assertEqual(solo_res.status, 200)
        self.assertEqual((await solo_res.json())["author_kind"], "system")
        self.assertEqual(self.hub.dispatch_setting(room_id), ("solo", True))
        self.assertEqual(self.hub.dispatch_setting(ws_room["id"]), ("solo", True))

        oneshot = await self.client.post(
            f"/api/rooms/{room_id}/messages",
            json={"text": "/team 重构前端面板"},
            headers=headers,
        )
        self.assertEqual(oneshot.status, 200)
        body = await oneshot.json()
        self.assertEqual(body["text"], "[团队模式] 重构前端面板")
        self.assertEqual(self.hub.dispatch_setting(room_id), ("solo", True))
        self.assertEqual(self.hub.effective_dispatch_setting(room_id), ("team", True))
        self.assertEqual(self.hub.effective_dispatch_setting(ws_room["id"]), ("team", True))

        state = await (await self.client.get("/collab/api/state", headers=headers)).json()
        main_room = next(r for r in state["rooms"] if r["id"] == f"room-{room_id}")
        self.assertEqual(main_room["kind"], "chat")
        self.assertEqual(main_room["dispatch_mode"], "team")

    async def test_cloudweb_dispatch_mode_and_one_shot_routing(self):
        headers = {"Authorization": "Bearer test-token"}
        room_id = self.hub.first_room()
        ws_ll = await self.client.ws_connect("/cloud-web/ws?bot=remote-ll&name=%E5%B0%8F%E6%A9%98", headers=headers)
        ws_codex = await self.client.ws_connect("/cloud-web/ws?bot=local-codex&name=codex", headers=headers)
        try:
            await ws_ll.send_json({"type": "register", "project": "remote-ll"})
            ack_ll = await ws_ll.receive_json()
            self.assertTrue(ack_ll["ok"])
            self.assertIn("dispatch_mode", ack_ll["capabilities"])

            await ws_codex.send_json({"type": "register", "project": "local-codex"})
            ack_cx = await ws_codex.receive_json()
            self.assertTrue(ack_cx["ok"])

            await ws_ll.send_json({
                "type": "team_workspace",
                "ref_id": "tw-1",
                "op": "dispatch_mode",
                "reply_ctx": f"room-{room_id}",
                "chat_id": f"room-{room_id}",
            })
            tw_ack = await ws_ll.receive_json()
            self.assertEqual(tw_ack["ref_id"], "tw-1")
            self.assertTrue(tw_ack["ok"])
            self.assertFalse(tw_ack["dispatch_explicit"])

            await self.client.post(f"/api/rooms/{room_id}/messages", json={"text": "/team 请排查 Bridge 故障"}, headers=headers)
            ll_frame = await ws_ll.receive_json()
            cx_frame = await ws_codex.receive_json()
            self.assertTrue(ll_frame["mentioned"])
            self.assertFalse(cx_frame["mentioned"])
            self.assertIn("[团队模式] 请排查 Bridge 故障", ll_frame["content"])

            await self.client.post(f"/api/rooms/{room_id}/messages", json={"text": "/team"}, headers=headers)
            await self.client.post(f"/api/rooms/{room_id}/messages", json={"text": "@codex 只看一个报错"}, headers=headers)
            ll_explicit = await ws_ll.receive_json()
            cx_explicit = await ws_codex.receive_json()
            self.assertFalse(ll_explicit["mentioned"])
            self.assertTrue(cx_explicit["mentioned"])
        finally:
            await ws_ll.close()
            await ws_codex.close()

    async def test_wiki_galaxy_routes(self):
        wiki_root = self.root / "wiki"
        (wiki_root / "sub").mkdir(parents=True)
        (wiki_root / "README.md").write_text("---\ndescription: 总览\n---\n# 知识库总览\n\n查看 [[sub/guide]]。\n", encoding="utf-8")
        (wiki_root / "sub" / "guide.md").write_text("# 使用指南\n\n回到 [首页](../README.md)。\n", encoding="utf-8")

        await self.client.close()
        os.environ["RH_WIKI_DIR"] = str(wiki_root)
        self.app = make_app(self.hub)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

        headers = {"Authorization": "Bearer test-token"}
        self.assertEqual((await self.client.get("/collab/wiki-galaxy")).status, 401)
        self.assertEqual((await self.client.get("/collab/api/wiki-galaxy")).status, 401)

        redir = await self.client.get("/collab", headers=headers, allow_redirects=False)
        self.assertEqual(redir.status, 302)
        self.assertEqual(redir.headers["Location"], "/")

        page = await self.client.get("/collab/wiki-galaxy", headers=headers, allow_redirects=False)
        self.assertEqual(page.status, 200)
        self.assertIn("WIKI 星河", await page.text())

        data = await (await self.client.get("/collab/api/wiki-galaxy", headers=headers)).json()
        node_ids = {n["id"] for n in data["nodes"]}
        self.assertEqual(node_ids, {"README.md", "sub/guide.md"})
        self.assertEqual(data["groups"], {"": 1, "sub": 1})
        self.assertEqual(len(data["links"]), 1)

    async def test_bridge_ws_relay(self):
        upstream = web.Application()
        seen_query = {}

        async def bridge_handler(request):
            seen_query["token"] = request.query.get("token")
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "hello", "ok": True})
            async for msg in ws:
                if msg.data == "ping":
                    await ws.send_str("pong")
            return ws

        upstream.router.add_get("/bridge/ws", bridge_handler)
        server = TestServer(upstream)
        await server.start_server()
        try:
            config = self.root / "bridge.toml"
            config.write_text(
                f'[management]\nenabled=true\nhost="127.0.0.1"\nport={server.port}\ntoken="m"\n'
                f'[bridge]\nenabled=true\nport={server.port}\npath="/bridge/ws"\ntoken="br-secret"\n',
                encoding="utf-8",
            )
            await self.client.close()
            os.environ["RH_PANEL_CONFIG"] = str(config)
            self.app = make_app(self.hub)
            self.client = TestClient(TestServer(self.app))
            await self.client.start_server()

            unauthed = await self.client.get("/bridge/ws?token=br-secret")
            self.assertEqual(unauthed.status, 401)

            ws = await self.client.ws_connect("/bridge/ws?token=br-secret", headers={"Authorization": "Bearer test-token"})
            try:
                hello = await ws.receive_json()
                self.assertEqual(hello, {"type": "hello", "ok": True})
                await ws.send_str("ping")
                self.assertEqual(await ws.receive_str(), "pong")
                self.assertEqual(seen_query["token"], "br-secret")
            finally:
                await ws.close()
        finally:
            await server.close()

    async def test_hub_builtin_avatars(self):
        headers = {"Authorization": "Bearer test-token"}
        home = await self.client.get("/", headers=headers)
        self.assertEqual(home.status, 200)
        html = await home.text()
        self.assertIn("BUILTIN_AVATAR_RULES", html)
        self.assertIn("/collab/avatars/", html)
        for slug in ("orange", "dragon", "whale", "rainbow", "grok", "lanxi"):
            resp = await self.client.get(f"/collab/avatars/{slug}.png", headers=headers)
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/png")

    async def test_gate_proxy(self):
        captured = {}
        upstream = web.Application()

        async def whoami(request):
            return web.json_response({"ok": True, "port": 9930})

        async def decide(request):
            captured["bot"] = request.query.get("bot")
            captured["body"] = await request.json()
            return web.json_response({"action": "context", "reason": "mock"})

        async def context(request):
            return web.json_response({"wiki": []})

        upstream.router.add_get("/whoami", whoami)
        upstream.router.add_post("/decide", decide)
        upstream.router.add_post("/context", context)
        server = TestServer(upstream)
        await server.start_server()
        try:
            await self.client.close()
            os.environ["RH_GATE_URL"] = f"http://127.0.0.1:{server.port}"
            self.app = make_app(self.hub)
            self.client = TestClient(TestServer(self.app))
            await self.client.start_server()

            bad_tok = await self.client.get("/gate/wrong-token/whoami")
            self.assertEqual(bad_tok.status, 401)
            bad_path = await self.client.get("/gate/test-token/forum")
            self.assertEqual(bad_path.status, 404)
            bad_trav = await self.client.get("/gate/test-token/../etc")
            self.assertEqual(bad_trav.status, 404)

            ok_who = await self.client.get("/gate/test-token/whoami")
            self.assertEqual(ok_who.status, 200)
            self.assertTrue((await ok_who.json())["ok"])

            ok_dec = await self.client.post("/gate/test-token/decide?bot=codex", json={"content": "hi"})
            self.assertEqual(ok_dec.status, 200)
            self.assertEqual((await ok_dec.json())["action"], "context")
            self.assertEqual(captured["bot"], "codex")
            self.assertEqual(captured["body"], {"content": "hi"})
        finally:
            await server.close()


    async def test_ollama_jev_proxy(self):
        for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            with self.subTest(ip=ip):
                self.assertTrue(_is_loopback_ip(ip))
        for ip in ("100.64.0.1", "192.168.1.5", None, "bad"):
            with self.subTest(ip=ip):
                self.assertFalse(_is_loopback_ip(ip))

        models = {"object": "list", "data": [{"id": "qwen3.5:4b", "object": "model"}]}
        captured = []
        seen_auth = []
        upstream = web.Application()

        async def list_models(request):
            seen_auth.append(request.headers.get("Authorization"))
            return web.json_response(models)

        async def chat_completions(request):
            seen_auth.append(request.headers.get("Authorization"))
            captured.append(await request.json())
            return web.json_response({"choices": []})

        upstream.router.add_get("/v1/models", list_models)
        upstream.router.add_post("/v1/chat/completions", chat_completions)
        async with TestServer(upstream) as server:
            await self.client.close()
            os.environ["RH_OLLAMA_URL"] = str(server.make_url("/"))
            self.app = make_app(self.hub)
            self.client = TestClient(TestServer(self.app))
            await self.client.start_server()

            prefix = "/ollama-jev/v1"
            models_path = f"{prefix}/models"
            chat_path = f"{prefix}/chat/completions"
            headers = {"Authorization": "Bearer test-token"}
            with patch("rh.panel_proxy._is_loopback_request", return_value=False):
                self.assertEqual((await self.client.get(models_path, headers=headers)).status, 403)
                self.assertEqual((await self.client.post(chat_path, json={}, headers=headers)).status, 403)

            for header, value in (
                ("Host", "100.64.0.1:8790"),
                ("Host", "[::1"),
                ("X-Forwarded-For", "100.64.0.1"),
                ("Forwarded", "for=100.64.0.1"),
                ("Tailscale-User-Login", "user@example.com"),
            ):
                with self.subTest(header=header, value=value):
                    response = await self.client.get(models_path, headers={**headers, header: value})
                    self.assertEqual(response.status, 403)

            for invalid_headers in ({}, {"Authorization": "Bearer wrong"}):
                with self.subTest(headers=invalid_headers):
                    self.assertEqual((await self.client.get(models_path, headers=invalid_headers)).status, 401)
                    self.assertEqual((await self.client.post(chat_path, json={}, headers=invalid_headers)).status, 401)

            self.assertEqual((await self.client.get(f"{prefix}/embeddings", headers=headers)).status, 404)
            self.assertEqual((await self.client.get(f"{prefix}/../tags", headers=headers)).status, 404)
            self.assertEqual((await self.client.post(models_path, json={}, headers=headers)).status, 405)
            self.assertEqual((await self.client.get(chat_path, headers=headers)).status, 405)
            self.assertEqual((await self.client.post(chat_path, data=b"{", headers=headers)).status, 400)
            self.assertEqual((await self.client.post(chat_path, json=[], headers=headers)).status, 400)

            response = await self.client.get(models_path, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), models)

            payload = {
                "model": "qwen3.5:4b",
                "chat_template_kwargs": {"enable_thinking": False},
                "max_tokens": 1,
            }
            response = await self.client.post(chat_path, json=payload, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(captured, [{"model": "qwen3.5:4b", "max_tokens": 1, "reasoning_effort": "none"}])

            payload["reasoning_effort"] = "low"
            payload["chat_template_kwargs"]["other_option"] = True
            response = await self.client.post(chat_path, json=payload, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(captured[-1], {
                "model": "qwen3.5:4b",
                "max_tokens": 1,
                "reasoning_effort": "low",
                "chat_template_kwargs": {"other_option": True},
            })
            self.assertTrue(seen_auth)
            self.assertTrue(all(auth is None for auth in seen_auth), seen_auth)


if __name__ == "__main__":
    unittest.main()
