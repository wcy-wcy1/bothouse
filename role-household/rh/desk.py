"""Authenticated collaboration desk endpoints and T3 Code + Lobby event bridge."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web

from .hub import STATIC, is_web_authed, make_auth_gate_html, set_auth_cookie

NOTICE_PREFIXES = ("📬", "✅", "↪", "⏳", "🔄", "⏹", "⏸")
FENCE_RE = re.compile(r"```.*?```", re.S)
FOOTER_RE = re.compile(r"(?:^|\n+)\*[^*\n]*·[^*]*\*\s*$", re.S)
CODEX_CALL_RE = re.compile(
    r"(?:@\s*(?:codex|local-codex)(?![A-Za-z0-9_])|(?:^|\n)\s*codex(?:[，,：:\s]|$))",
    re.I,
)
XIAOJU_CALL_RE = re.compile(
    r"(?:@\s*(?:小橘|橘|ll|remote-ll)(?![A-Za-z0-9_])|(?:^|\n)\s*(?:小橘|橘)(?:[，,：:\s]|$))",
    re.I,
)


def _clean_preview(text: object, limit: int = 120) -> str:
    """Strip code fences and Bot footers, collapse whitespace, and truncate safely."""
    raw = str(text or "")
    raw = FOOTER_RE.sub("", raw)
    raw = FENCE_RE.sub(" [代码] ", raw)
    compact = " ".join(raw.split())
    return compact[:limit] if len(compact) > limit else compact


def _strip_fences(text: object) -> str:
    return FENCE_RE.sub(" ", str(text or ""))


def _mentions_in(author_id: str, text: object) -> list[str]:
    stripped = _strip_fences(text)
    out: list[str] = []
    if author_id != "local-codex" and CODEX_CALL_RE.search(stripped):
        out.append("local-codex")
    if author_id != "remote-ll" and XIAOJU_CALL_RE.search(stripped):
        out.append("remote-ll")
    return out


def _epoch_of(stamp: object) -> float | None:
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        val = float(stamp)
        return val / 1000.0 if val > 1e12 else val
    if isinstance(stamp, str) and stamp.strip():
        try:
            dt = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _iso_of(stamp: object = None, tz=timezone.utc) -> str:
    epoch = _epoch_of(stamp)
    if epoch is None:
        return datetime.now(tz).isoformat().replace("+00:00", "Z")
    iso = datetime.fromtimestamp(epoch, tz).isoformat()
    return iso.replace("+00:00", "Z") if tz is timezone.utc else iso


def _terminal_kind(status: str) -> str:
    if status == "failed":
        return "rejected"
    if status in ("cancelled", "interrupted"):
        return "paused"
    return "done"


def attach(app: web.Application, hub, t3_db_path: str | Path | None = None,
           names_path: str | Path | None = None) -> None:
    raw_t3 = t3_db_path if t3_db_path is not None else os.environ.get("RH_T3_STATE_DB")
    t3_path = Path(raw_t3) if raw_t3 else None
    name_file = Path(names_path) if names_path is not None else (
        hub.db_path.parent / "desk-names.json" if getattr(hub, "db_path", None) else None
    )
    queues: set[asyncio.Queue] = set()
    sse_tasks: set[asyncio.Task] = set()
    t3_active: dict[str, dict] = {}
    lobby_active: dict[str, dict] = {}
    seen_t3: set[str] = set()
    thinking: list[str] = []
    names_mem: dict[str, str] = {}
    polling: asyncio.Task | None = None

    def emit(event: dict) -> None:
        for queue in tuple(queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass

    def roster() -> list[dict]:
        members = []
        for bot in hub.bot_list():
            kind, avatar = ({"remote-ll": ("dsh", "orange"), "local-codex": ("bot", "dragon")}
                            .get(bot["id"], ("bot", "")))
            members.append({**bot, "kind": kind, "avatar": avatar})
        if t3_path and t3_path.is_file():
            members.extend([
                {"id": "t3-antigravity", "name": "T3 主控", "kind": "dsh", "avatar": "rainbow", "online": True},
                {"id": "t3-codex", "name": "T3 Codex", "kind": "bot", "avatar": "whale", "online": True},
            ])
        return members

    def lead(members: list[dict]) -> str | None:
        ids = [item["id"] for item in members]
        return next((i for i in ("remote-ll", "t3-antigravity") if i in ids), ids[0] if ids else None)

    def auth(handler):
        async def guarded(request: web.Request):
            if not is_web_authed(hub, request):
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)
        return guarded

    async def desk_page(request: web.Request):
        token = request.query.get("token")
        if token and hub.token_ok(token):
            query = [(k, v) for k, v in request.query.items() if k != "token"]
            location = request.path + (("?" + urlencode(query)) if query else "")
            response = web.HTTPFound(location=location)
            set_auth_cookie(response, hub, request)
            raise response
        if not is_web_authed(hub, request):
            return web.Response(
                text=make_auth_gate_html(request.path_qs),
                status=401,
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )
        return web.FileResponse(STATIC / "desk.html", headers={"Cache-Control": "no-store"})

    async def avatar(request: web.Request):
        if not is_web_authed(hub, request):
            return web.json_response({"error": "unauthorized"}, status=401)
        name = request.match_info["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.png", name):
            raise web.HTTPNotFound()
        path = STATIC / "collab-avatars" / name
        return web.FileResponse(path) if path.is_file() else web.Response(status=404)

    async def wiki(request: web.Request):
        if not is_web_authed(hub, request):
            return web.Response(
                text=make_auth_gate_html(request.path_qs),
                status=401,
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )
        raise web.HTTPFound("/collab/panel/wiki-galaxy")

    @auth
    async def state(request: web.Request):
        members = roster()
        rooms = [{"id": f"room-{r['id']}", "name": r["name"]} for r in hub.rooms()]
        rooms_thinking = {f"room-{rid}": list(bots) for rid, bots in hub.bot_thinking.items() if bots}
        if thinking:
            rooms_thinking["t3"] = list(thinking)
        lead_id = lead(members)
        return web.json_response({
            "me": "owner",
            "lead_id": lead_id,
            "effective_lead_id": lead_id,
            "rooms": rooms,
            "members": members,
            "thinking": rooms_thinking,
        })

    @auth
    async def task_state(request: web.Request):
        tasks = list(lobby_active.values()) + list(t3_active.values())
        return web.json_response({"tasks": tasks, "returned": [], "paused": []})

    def _read_names() -> dict[str, str]:
        if name_file is None:
            return dict(names_mem)
        try:
            data = json.loads(name_file.read_text("utf-8")) if name_file.exists() else {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @auth
    async def names_get(request: web.Request):
        return web.json_response({"names": _read_names()})

    @auth
    async def names_put(request: web.Request):
        try:
            data = await request.json()
        except (ValueError, Exception):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"].strip() or not isinstance(data.get("name"), str):
            return web.json_response({"error": "invalid name"}, status=400)
        member_id = data["id"].strip()
        name = data["name"].strip()
        if len(name) > 24:
            return web.json_response({"error": "name too long"}, status=400)
        if name_file is None:
            if name:
                names_mem[member_id] = name
            else:
                names_mem.pop(member_id, None)
            return web.json_response({"names": dict(names_mem)})
        name_file.parent.mkdir(parents=True, exist_ok=True)
        names = _read_names()
        if name:
            names[member_id] = name
        else:
            names.pop(member_id, None)
        temp = name_file.with_suffix(name_file.suffix + ".tmp")
        temp.write_text(json.dumps(names, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(name_file)
        return web.json_response({"names": names})

    @auth
    async def replay(request: web.Request):
        try:
            start = datetime.fromisoformat(request.query["from"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(request.query["to"].replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None or start > end:
                raise ValueError
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid time range"}, status=400)
        start_ep, end_ep = start.timestamp(), end.timestamp()
        tz = start.tzinfo
        timeline: list[tuple[float, int, dict]] = []
        seq = 0

        def push(epoch: float, ev: dict) -> None:
            nonlocal seq
            seq += 1
            timeline.append((epoch, seq, ev))

        rows = hub.db.execute(
            "SELECT id, room_id, author, author_kind, name, text, created_at, card "
            "FROM messages WHERE created_at BETWEEN ? AND ? ORDER BY created_at, id",
            (start_ep, end_ep),
        ).fetchall()
        lobby_pending: list[dict] = []
        for row in rows:
            raw_text = str(row["text"] or "")
            if row["author_kind"] == "system" or row["card"] is not None or raw_text.lstrip().startswith(NOTICE_PREFIXES):
                continue
            author = row["author"]
            kind_out = "bot" if row["author_kind"] == "role" else row["author_kind"]
            preview = _clean_preview(raw_text, 120)
            if not preview:
                continue
            created = float(row["created_at"])
            stamp = _iso_of(created, tz)
            msg = {
                "id": row["id"],
                "room_id": row["room_id"],
                "author": author,
                "author_kind": kind_out,
                "name": row["name"],
                "text": preview,
                "mentions": _mentions_in(author, raw_text),
                "created_at": created,
            }
            push(created, {"type": "message", "ts": stamp, "message": msg})
            if author == "remote-ll" and "local-codex" in msg["mentions"]:
                task_title = _clean_preview(raw_text, 48) or "Lobby 协作任务"
                task = {"id": f"lobby-{row['id']}", "from": "remote-ll", "to": "local-codex", "title": task_title}
                lobby_pending.append(task)
                push(created, {"type": "task", **task, "kind": "assigned", "ts": stamp})
            elif author == "local-codex" and lobby_pending:
                while lobby_pending:
                    task = lobby_pending.pop(0)
                    push(created, {"type": "task", **task, "kind": "returned", "ts": stamp})
                    push(created, {"type": "task", **task, "kind": "done", "ts": stamp})

        if t3_path and t3_path.is_file():
            conn = None
            try:
                conn = sqlite3.connect(f"file:{t3_path.resolve().as_posix()}?mode=ro", uri=True, timeout=1.5)
                sub_rows = conn.execute(
                    "SELECT s.subagent_id, s.status, s.started_at, s.completed_at, s.child_thread_id, t.title "
                    "FROM orchestration_v2_projection_subagents s "
                    "LEFT JOIN orchestration_v2_projection_threads t ON t.thread_id = s.child_thread_id"
                ).fetchall()
                for sid, status, started, completed, _child_id, title in sub_rows:
                    task_title = _clean_preview(title or sid or "T3 子任务", 48) or str(sid)
                    start_e = _epoch_of(started)
                    if start_e is not None and start_ep <= start_e <= end_ep:
                        sts = _iso_of(start_e, tz)
                        push(start_e, {
                            "type": "task",
                            "id": sid,
                            "kind": "assigned",
                            "from": "t3-antigravity",
                            "to": "t3-codex",
                            "title": task_title,
                            "ts": sts,
                        })
                        push(start_e, {"type": "thinking", "room_id": "t3", "members": ["t3-codex"], "ts": sts})
                    comp_e = _epoch_of(completed)
                    if comp_e is not None and start_ep <= comp_e <= end_ep:
                        cts = _iso_of(comp_e, tz)
                        base = {"type": "task", "id": sid, "from": "t3-antigravity", "to": "t3-codex", "title": task_title, "ts": cts}
                        push(comp_e, {**base, "kind": "returned"})
                        push(comp_e, {**base, "kind": _terminal_kind(str(status or "completed"))})
                        push(comp_e, {"type": "thinking", "room_id": "t3", "members": [], "ts": cts})
            except (sqlite3.Error, OSError):
                pass
            finally:
                if conn is not None:
                    conn.close()

        timeline.sort(key=lambda item: (item[0], item[1]))
        events = [item[2] for item in timeline[-200:]] if len(timeline) > 200 else [item[2] for item in timeline]
        return web.json_response({"from": request.query["from"], "to": request.query["to"], "events": events})

    @auth
    async def event_stream(request: web.Request):
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        task = asyncio.current_task()
        if task is not None:
            sse_tasks.add(task)
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        queues.add(queue)
        try:
            await response.write(("data: " + json.dumps({"type": "members", "members": roster()}, ensure_ascii=False) + "\n\n").encode())
            for rid, bots in hub.bot_thinking.items():
                if bots:
                    await response.write(("data: " + json.dumps({"type": "thinking", "room_id": f"room-{rid}", "members": list(bots)}, ensure_ascii=False) + "\n\n").encode())
            if thinking:
                await response.write(("data: " + json.dumps({"type": "thinking", "room_id": "t3", "members": list(thinking)}, ensure_ascii=False) + "\n\n").encode())
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 20)
                    await response.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            queues.discard(queue)
            if task is not None:
                sse_tasks.discard(task)
        return response

    def db_snapshot():
        if not t3_path or not t3_path.is_file():
            return [], False
        uri = f"file:{t3_path.resolve().as_posix()}?mode=ro"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1.5)
            subs = conn.execute(
                "SELECT s.subagent_id, s.status, s.started_at, s.completed_at, s.child_thread_id, t.title "
                "FROM orchestration_v2_projection_subagents s "
                "LEFT JOIN orchestration_v2_projection_threads t ON t.thread_id = s.child_thread_id"
            ).fetchall()
            main_running = conn.execute(
                "SELECT 1 FROM orchestration_v2_projection_runs "
                "WHERE status='running' AND thread_id NOT IN ("
                "SELECT child_thread_id FROM orchestration_v2_projection_subagents WHERE child_thread_id IS NOT NULL"
                ") LIMIT 1"
            ).fetchone() is not None
            return subs, main_running
        except (sqlite3.Error, OSError):
            return [], False
        finally:
            if conn is not None:
                conn.close()

    async def poll_t3():
        first = True
        while True:
            subs, main_running = db_snapshot()
            current: dict[str, dict] = {}
            terminal_status: dict[str, str] = {}
            for sid, status, started, completed, _child_id, title in subs:
                if status == "running":
                    current[sid] = {
                        "id": sid,
                        "kind": "assigned",
                        "title": _clean_preview(title or sid or "T3 子任务", 48) or str(sid),
                        "from": "t3-antigravity",
                        "to": "t3-codex",
                        "ts": _iso_of(started),
                    }
                elif status in ("completed", "failed", "cancelled", "interrupted"):
                    terminal_status[sid] = status
            if first:
                t3_active.update(current)
                seen_t3.update(terminal_status.keys())
                first = False
            else:
                for sid, task in current.items():
                    if sid not in t3_active and sid not in seen_t3:
                        t3_active[sid] = task
                        emit({"type": "task", **task, "kind": "assigned"})
                for sid in list(t3_active):
                    if sid not in current:
                        task = t3_active.pop(sid)
                        now_ts = _iso_of()
                        emit({"type": "task", **task, "kind": "returned", "ts": now_ts})
                        final_kind = _terminal_kind(terminal_status.get(sid, "completed"))
                        emit({"type": "task", **task, "kind": final_kind, "ts": now_ts})
                        seen_t3.add(sid)
            new_thinking = (["t3-antigravity"] if main_running else []) + (["t3-codex"] if current else [])
            if new_thinking != thinking:
                thinking[:] = new_thinking
                emit({"type": "thinking", "room_id": "t3", "members": list(thinking), "ts": _iso_of()})
            await asyncio.sleep(1.5)

    async def lobby_message(message: dict) -> None:
        raw_text = str(message.get("text") or "")
        if message.get("author_kind") == "system" or message.get("card") is not None or raw_text.lstrip().startswith(NOTICE_PREFIXES):
            return
        author = str(message.get("author") or "")
        mentions = _mentions_in(author, raw_text)
        preview = _clean_preview(raw_text, 120)
        created = message.get("created_at") or time.time()
        ts = _iso_of(created)
        clean_msg = {
            "id": message.get("id"),
            "room_id": message.get("room_id"),
            "author": author,
            "author_kind": "bot" if message.get("author_kind") == "role" else message.get("author_kind"),
            "name": message.get("name"),
            "text": preview,
            "mentions": mentions,
            "created_at": created,
        }
        emit({"type": "message", "ts": ts, "message": clean_msg})
        if author == "remote-ll" and "local-codex" in mentions:
            task_id = f"lobby-{message.get('id', time.time_ns())}"
            task = {
                "id": task_id,
                "kind": "assigned",
                "title": _clean_preview(raw_text, 48) or "Lobby 协作任务",
                "from": "remote-ll",
                "to": "local-codex",
                "ts": ts,
            }
            lobby_active[task_id] = task
            emit({"type": "task", **task})
        elif author == "local-codex" and lobby_active:
            for task_id in list(lobby_active):
                task = lobby_active.pop(task_id)
                emit({"type": "task", **task, "kind": "returned", "ts": ts})
                emit({"type": "task", **task, "kind": "done", "ts": ts})

    async def lobby_bots() -> None:
        emit({"type": "members", "members": roster()})

    async def lobby_thinking(room_id: int, bots: list[str]) -> None:
        emit({"type": "thinking", "room_id": f"room-{room_id}", "members": list(bots), "ts": _iso_of()})

    hub.listeners.append(lobby_message)
    hub.bot_listeners.append(lobby_bots)
    hub.thinking_listeners.append(lobby_thinking)

    async def startup(app: web.Application) -> None:
        nonlocal polling
        if t3_path:
            polling = asyncio.create_task(poll_t3())

    async def shutdown(app: web.Application) -> None:
        for t in list(sse_tasks):
            t.cancel()

    async def cleanup(app: web.Application) -> None:
        for listeners, callback in (
            (hub.listeners, lobby_message),
            (hub.bot_listeners, lobby_bots),
            (hub.thinking_listeners, lobby_thinking),
        ):
            if callback in listeners:
                listeners.remove(callback)
        if polling:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)

    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    app.on_cleanup.append(cleanup)
    app.router.add_get("/collab/desk", desk_page)
    app.router.add_get("/collab/avatars/{name}", avatar)
    app.router.add_get("/collab/wiki-galaxy", wiki)
    app.router.add_get("/collab/api/state", state)
    app.router.add_get("/collab/api/desk/state", task_state)
    app.router.add_get("/collab/api/desk/replay", replay)
    app.router.add_get("/collab/api/desk/names", names_get)
    app.router.add_put("/collab/api/desk/names", names_put)
    app.router.add_get("/collab/api/events", event_stream)
