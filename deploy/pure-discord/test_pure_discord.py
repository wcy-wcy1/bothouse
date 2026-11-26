"""Pure Discord tests using loopback mocks and temporary files."""

import base64
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

import ollama_jev_shim as shim
import setup_discord as setup


# Generate an all-zero synthetic ID without embedding anyone's Discord ID.
GUILD = "0" * 18
BOT_TOKEN = "test-bot-fixture"
PROXY = "http://127.0.0.1:7897"


class MockService(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_call(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length)) if length else None
        self.server.calls.append((self.command, self.path, dict(self.headers), payload))
        status, content_type, body = self.server.response
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PATCH = handle_call


@contextmanager
def running(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@contextmanager
def mock_service():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockService)
    server.calls = []
    server.response = (200, "application/json", b'{"data":[]}')
    with running(server):
        yield server


def local_url(server):
    return f"http://127.0.0.1:{server.server_port}"


class ShimTests(unittest.TestCase):
    def setUp(self):
        self.upstream = self.enterContext(mock_service())
        self.server = self.enterContext(running(shim.create_server(
            "127.0.0.1", 0, local_url(self.upstream) + "/", "test-shim-fixture"
        )))
        self.enterContext(patch.object(shim.ShimHandler, "log_message"))

    def request(self, method="GET", path="/v1/models", *, headers=None, body=None, authorize=True):
        headers = dict(headers or {})
        if authorize:
            headers.setdefault("Authorization", "bEaReR test-shim-fixture")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            with connection.getresponse() as response:
                return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def assert_error(self, expected_status, error, *args, **kwargs):
        status, content_type, body = self.request(*args, **kwargs)
        self.assertEqual(status, expected_status)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(body), {"ok": False, "error": error})
        self.assertEqual(self.upstream.calls, [])

    def test_loopback_helpers_and_startup_guard(self):
        for address in ("127.0.0.1", "127.8.9.10", "::1", "::1%lo0", "::ffff:127.0.0.1"):
            with self.subTest(address=address):
                self.assertTrue(shim.is_loopback_ip(address))
        for address in (None, "", "localhost", "bad-ip", "0.0.0.0", "::ffff:203.0.113.1"):
            with self.subTest(address=address):
                self.assertFalse(shim.is_loopback_ip(address))
        self.assertTrue(shim.is_loopback_request("::1", {"HOST": "LOCALHOST:11435"}))
        self.assertTrue(shim.is_loopback_request("::1", {"Host": "[::1]:11435"}))
        self.assertFalse(shim.is_loopback_request("203.0.113.1", {"Host": "localhost"}))
        self.assertFalse(shim.is_loopback_request("::1", {}))
        with redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as raised:
            shim.main(["--host", "0.0.0.0", "--port", "0"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("loopback IP", output.getvalue())

    def test_rejects_nonlocal_or_malformed_host_and_forwarding_headers(self):
        headers = [
            {"Host": "example.invalid"},
            {"Host": "[::1"},
            {"Host": "localhost:noport"},
            {"Host": "localhost/path"},
            {"x-forwarded-for": ""},
            {"FoRwArDeD": "for=127.0.0.1"},
            {"tailscale-user-login": ""},
        ]
        for candidate in headers:
            with self.subTest(headers=candidate):
                self.assert_error(403, "loopback only", headers=candidate, authorize=False)

    def test_rejects_bad_authentication(self):
        self.assert_error(401, "unauthorized", authorize=False)
        for authorization in ("Bearer incorrect", "Basic test-shim-fixture"):
            with self.subTest(authorization=authorization):
                self.assert_error(401, "unauthorized", headers={"Authorization": authorization})

    def test_rejects_unknown_paths_methods_and_invalid_json(self):
        for path in ("/", "/v1/models/", "//v1/models", "/v1/../v1/models", "/v1/%2e%2e/models"):
            with self.subTest(path=path):
                self.assert_error(404, "not found", path=path)
        for method, path in (("DELETE", "/v1/models"), ("OPTIONS", "/v1/models"),
                             ("POST", "/v1/models"), ("GET", "/v1/chat/completions")):
            with self.subTest(method=method, path=path):
                self.assert_error(405, "method not allowed", method, path)
        for body in ("invalid", "[]", "null", b"\xff", ""):
            with self.subTest(body=body):
                self.assert_error(400, "invalid json", "POST", "/v1/chat/completions", body=body)

    def test_models_preserves_query_and_strips_authorization(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            self.assertEqual(self.request(path="/v1/models?test=1"),
                             (200, "application/json", b'{"data":[]}'))
        method, path, headers, _ = self.upstream.calls[0]
        self.assertEqual((method, path), ("GET", "/v1/models?test=1"))
        self.assertIsNone(headers.get("Authorization"))
        self.assertIsNone(headers.get("Content-Type"))

    def test_completions_rewrite_preserves_other_fields(self):
        base = {"model": "qwen3.5:4b", "messages": []}
        cases = [
            ({"chat_template_kwargs": {"enable_thinking": False}}, {"reasoning_effort": "none"}),
            ({"reasoning_effort": "low", "chat_template_kwargs": {"enable_thinking": False, "other": 1}},
             {"reasoning_effort": "low", "chat_template_kwargs": {"other": 1}}),
            ({"chat_template_kwargs": {"enable_thinking": True}},
             {"chat_template_kwargs": {"enable_thinking": True}}),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                data = base | source
                self.assertEqual(shim.rewrite_ollama_jev_body(data), base | expected)
                self.assertEqual(data, base | source)
                status, _, _ = self.request("POST", "/v1/chat/completions", body=json.dumps(data))
                self.assertEqual(status, 200)
                method, path, headers, payload = self.upstream.calls[-1]
                self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
                self.assertIsNone(headers.get("Authorization"))
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(payload, base | expected)

    def test_upstream_status_body_and_unreachable(self):
        self.upstream.response = (429, "text/plain", b"slow down")
        self.assertEqual(self.request(), (429, "text/plain", b"slow down"))
        self.upstream.response = (200, None, b"no content type")
        self.assertEqual(self.request(), (200, None, b"no content type"))
        self.upstream.calls.clear()
        with patch.object(shim, "build_opener") as opener:
            opener.return_value.open.side_effect = shim.URLError("mock connection failure")
            self.assert_error(502, "ollama unreachable")

    def test_unconfigured_token_allows_loopback(self):
        with running(shim.create_server("127.0.0.1", 0, local_url(self.upstream))) as server:
            self.server = server
            self.assertEqual(self.request(authorize=False)[0], 200)


EXISTING_CONFIG = f'''# Existing comment
[log]
level = "info"

[[projects]]
name = "ju"
[projects.agent]
type = "claudecode"
[[projects.platforms]]
type = "discord"
[projects.platforms.options]
guild_id = "{GUILD}"
group_reply_all = false
proxy = "" # keep this comment
ack_reaction = false

[[projects.platforms]]
type = "other"
[projects.platforms.options]
proxy = "unchanged"

[[projects]]
name = "longniang"
[projects.agent]
type = "codex"
[[projects.platforms]]
type = "discord"
[projects.platforms.options]
guild_id = "{GUILD}"
group_reply_all = true

[[projects]]
name = "unmanaged"
[[projects.platforms]]
type = "discord"
[projects.platforms.options]
proxy = "unchanged"
'''

MOCK_BOTHOUSE = r'''
import json
import os
from pathlib import Path
import stat
import sys

command = sys.argv[1:]
if command == ["setup", "clis"]:
    print(json.dumps([{"agent_type": "codex", "login": "logged_in", "supported": True}]))
    sys.exit(0)
assert command[:3] == ["setup", "apply", "--json"]
request = Path(command[3])
assert request.is_file()
if os.name == "posix":
    assert stat.S_IMODE(request.stat().st_mode) == 0o600
payload = json.loads(request.read_text(encoding="utf-8"))
config = Path(command[command.index("--config") + 1])
dry_run = "--dry-run" in command
if not dry_run:
    fixture = Path(os.environ["PD_TEST_CONFIG"])
    config.write_bytes(fixture.read_bytes())
capture = Path(os.environ["PD_TEST_CAPTURE"])
capture.write_text(json.dumps({
    "payload": payload, "dry_run": dry_run, "request_path": str(request),
    "http_proxy": os.environ.get("HTTP_PROXY"), "https_proxy": os.environ.get("HTTPS_PROXY"),
}), encoding="utf-8")
print(json.dumps({
    "saved": not dry_run, "needs_user_action": dry_run, "dry_run": dry_run,
    "roles": [{"project": "ju", "name": "Orange", "agent_type": "claudecode"}],
    "invite_urls": ["https://example.invalid/invite"],
    "warnings": [{"code": "discord_avatar_not_set", "project": "Orange",
                  "message": "warning " + payload["roles"][0]["token"],
                  "fix": "fix the problem", "url": "https://example.invalid/fix"}],
    "next_steps": ["invite bots"],
}))
sys.exit(int(dry_run))
'''


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="pure-discord-test-")))
        self.manifest = self.root / "team.toml"
        self.config = self.root / "config.toml"
        self.data_dir = self.root / "data"
        self.enterContext(patch.object(setup, "bothouse_home", return_value=self.data_dir))
        fixture = self.root / "fixture.toml"
        fixture.write_text(EXISTING_CONFIG, encoding="utf-8")
        self.capture = self.root / "capture.json"
        self.enterContext(patch.dict(os.environ, {
            "DISCORD_JU_TOKEN": BOT_TOKEN,
            "PD_TEST_CONFIG": str(fixture),
            "PD_TEST_CAPTURE": str(self.capture),
        }))

    def write_manifest(self, guild=GUILD, token='token_env = "DISCORD_JU_TOKEN"'):
        self.manifest.write_text(f'''
[discord]
guild_id = "{guild}"
proxy = "{PROXY}"
allow_from = "*"
fallback_role = "ju"

[gate]
mode = "smart"

[jev]
base_url = "http://127.0.0.1:11435/v1"
model = "qwen3.5:4b"

[[roles]]
project = "ju"
persona = "ju"
{token}

[[roles]]
project = "longniang"
agent_type = "codex"
persona = "longniang"
token = "test-codex-fixture"
''', encoding="utf-8")

    def command(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = setup.main([str(arg) for arg in args])
        return status, output.getvalue(), errors.getvalue()

    def make_fake_bothouse(self):
        source = self.root / "mock_bothouse.py"
        source.write_text(MOCK_BOTHOUSE, encoding="utf-8")
        if os.name == "nt":
            executable = self.root / "bothouse.cmd"
            executable.write_text(f'@"{sys.executable}" -B "%~dp0mock_bothouse.py" %*\n', encoding="utf-8")
        else:
            executable = self.root / "bothouse"
            executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" -B "$(dirname "$0")/mock_bothouse.py" "$@"\n',
                                  encoding="utf-8")
            executable.chmod(0o700)
        return executable

    def test_personas_command_and_library_assets(self):
        status, output, errors = self.command("personas")
        self.assertEqual((status, errors), (0, ""))
        presets = setup.load_personas()
        self.assertEqual(len(output.splitlines()), 12)
        self.assertEqual({line.split("\t")[0] for line in output.splitlines()}, presets.keys())
        for preset in presets.values():
            with self.subTest(persona=preset["id"]):
                self.assertIn("\t".join(preset[k] for k in ("id", "name", "model", "avatar", "intro")), output)
                if preset["avatar"]:
                    self.assertTrue((setup.PERSONAS_DIR / "avatars" / (preset["avatar"] + ".png")).is_file())

    def test_init_manifest_and_force(self):
        output = self.root / "new" / "team.toml"
        self.assertEqual(self.command("init-manifest", "--output", output)[0], 0)
        self.assertEqual(tomllib.loads(output.read_text(encoding="utf-8"))["discord"]["guild_id"], "<DISCORD_SERVER_ID>")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        output.write_text("# keep me", encoding="utf-8")
        status, _, errors = self.command("init-manifest", "--output", output)
        self.assertEqual(status, 1)
        self.assertIn("--force", errors)
        self.assertEqual(output.read_text(encoding="utf-8"), "# keep me")
        self.assertEqual(self.command("init-manifest", "--output", output, "--force")[0], 0)
        self.assertEqual(output.read_bytes(), (setup.KIT_DIR / "team.toml.example").read_bytes())

    def test_manifest_validation(self):
        self.write_manifest(guild="not-an-id")
        with self.assertRaisesRegex(ValueError, "discord.guild_id"):
            setup.load_manifest(self.manifest)
        self.write_manifest(token='token_env = "PD_TEST_MISSING_TOKEN"')
        with patch.dict(os.environ, {"PD_TEST_MISSING_TOKEN": ""}):
            with self.assertRaisesRegex(ValueError, "token is missing"):
                setup.load_manifest(self.manifest)
        self.write_manifest(token='token = "<BOT_TOKEN>"')
        with self.assertRaisesRegex(ValueError, "token is missing"):
            setup.load_manifest(self.manifest)

    def test_payload_resolves_preset_and_optional_fields(self):
        self.write_manifest()
        payload = setup.build_apply_payload(setup.load_manifest(self.manifest), self.root)
        self.assertEqual(payload["platform"], "discord")
        self.assertEqual(payload["language"], "zh")
        self.assertEqual(payload["discord"], {"guild_id": GUILD, "channel_id": ""})
        self.assertEqual(payload["fallback_role"], "ju")
        self.assertEqual(payload["gate"], {"enabled": True, "mode": "smart"})
        self.assertEqual(payload["jev"]["model"], "qwen3.5:4b")
        first, second = payload["roles"]
        self.assertEqual(first["persona"], (setup.PERSONAS_DIR / "prompts/ju.md").read_text(encoding="utf-8"))
        self.assertEqual((first["persona_preset"], first["avatar"], first["agent_type"]), ("ju", "orange", "claudecode"))
        self.assertEqual((second["persona_preset"], second["avatar"], second["agent_type"]), ("longniang", "dragon", "codex"))
        self.assertNotIn("codex_home_isolate", second)

    def test_apply_dry_run_and_real_save_with_mock_binary(self):
        self.write_manifest()
        executable = self.make_fake_bothouse()
        command = ["apply", self.manifest, "--bothouse", executable, "--config", self.config, "--no-avatars"]
        status, output, errors = self.command(*command, "--dry-run")
        self.assertEqual((status, errors), (1, ""))
        self.assertIn("Saved: no", output)
        self.assertIn("User action required", output)
        self.assertIn("https://example.invalid/invite", output)
        self.assertIn("fix the problem", output)
        self.assertIn("https://example.invalid/fix", output)
        self.assertIn("Next: invite bots", output)
        self.assertNotIn(BOT_TOKEN, output)
        recorded = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertTrue(recorded["dry_run"])
        self.assertEqual(recorded["payload"]["roles"][0]["token"], BOT_TOKEN)
        self.assertEqual((recorded["http_proxy"], recorded["https_proxy"]), (PROXY, PROXY))
        self.assertFalse(Path(recorded["request_path"]).exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(self.data_dir.exists())

        status, output, errors = self.command(*command)
        self.assertEqual((status, errors), (0, ""))
        self.assertIn("Saved: yes", output)
        self.assertNotIn(BOT_TOKEN, output)
        recorded = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertFalse(recorded["dry_run"])
        self.assertFalse(Path(recorded["request_path"]).exists())
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        ju, codex, unmanaged = parsed["projects"]
        self.assertEqual(ju["platforms"][0]["options"], {
            "guild_id": GUILD, "group_reply_all": False, "proxy": PROXY, "ack_reaction": True,
        })
        self.assertEqual(ju["platforms"][1]["options"], {"proxy": "unchanged"})
        self.assertEqual(unmanaged["platforms"][0]["options"], {"proxy": "unchanged"})
        home = self.data_dir / "codex-home" / "longniang"
        self.assertEqual(codex["agent"]["options"]["codex_home"], home.as_posix())
        self.assertEqual(tomllib.loads((home / "config.toml").read_text(encoding="utf-8")),
                         {"sqlite_home": home.as_posix()})
        self.assertIn("# keep this comment", self.config.read_text(encoding="utf-8"))

    def test_patch_updates_existing_codex_home_and_is_idempotent(self):
        self.write_manifest()
        manifest = setup.load_manifest(self.manifest)
        home = self.root / "custom home"
        home.mkdir()
        manifest["roles"][1]["codex_home"] = str(home)
        manifest["discord"]["ack_reaction"] = False
        home_config = home / "config.toml"
        home_config.write_text('sqlite_home = "old" # SQLite\n[features]\nflag = true\n', encoding="utf-8")
        self.config.write_text(EXISTING_CONFIG.replace(
            'type = "codex"', 'type = "codex"\n[projects.agent.options]\ncodex_home = "old"\nkeep = true'
        ), encoding="utf-8")
        setup.patch_bothouse_config(self.config, manifest, self.data_dir)
        self.assertEqual(tomllib.loads(home_config.read_text(encoding="utf-8")),
                         {"sqlite_home": home.as_posix(), "features": {"flag": True}})
        self.assertIn("# SQLite", home_config.read_text(encoding="utf-8"))
        config_bytes = self.config.read_bytes()
        home_bytes = home_config.read_bytes()
        parsed = tomllib.loads(config_bytes.decode("utf-8"))
        self.assertFalse(parsed["projects"][0]["platforms"][0]["options"]["ack_reaction"])
        self.assertEqual(parsed["projects"][1]["agent"]["options"], {"codex_home": home.as_posix(), "keep": True})
        setup.patch_bothouse_config(self.config, manifest, self.data_dir)
        self.assertEqual(self.config.read_bytes(), config_bytes)
        self.assertEqual(home_config.read_bytes(), home_bytes)

    def test_apply_avatar_warning_matches_discord_display_name(self):
        self.write_manifest()
        executable = self.make_fake_bothouse()
        with patch.object(setup, "upload_discord_avatar") as upload:
            status, _, errors = self.command(
                "apply", self.manifest, "--bothouse", executable, "--config", self.config
            )
        self.assertEqual((status, errors), (0, ""))
        upload.assert_called_once_with(BOT_TOKEN, setup.PERSONAS_DIR / "avatars/orange.png", PROXY)

    def test_set_avatars_dry_run_upload_and_no_rate_limit_retry(self):
        self.write_manifest()
        with mock_service() as server:
            command = ["set-avatars", self.manifest, "--role", "ju", "--api-base", local_url(server), "--proxy", ""]
            status, output, errors = self.command(*command, "--dry-run")
            self.assertEqual((status, errors), (0, ""))
            self.assertIn("would upload orange.png", output)
            self.assertEqual(server.calls, [])
            self.assertEqual(self.command(*command)[0], 0)
            [(method, path, headers, payload)] = server.calls
            self.assertEqual((method, path), ("PATCH", "/users/@me"))
            self.assertEqual(headers["Authorization"], "Bot " + BOT_TOKEN)
            prefix, image = payload["avatar"].split(",", 1)
            self.assertEqual(prefix, "data:image/png;base64")
            self.assertEqual(base64.b64decode(image), (setup.PERSONAS_DIR / "avatars/orange.png").read_bytes())
            server.response = (429, "text/plain", b"rate limited")
            status, _, errors = self.command(*command)
            self.assertEqual(status, 1)
            self.assertIn("HTTP 429", errors)
            self.assertEqual(len(server.calls), 2)

    def test_doctor_reads_projects_and_probes_only_mock_services(self):
        self.write_manifest()
        self.config.write_text(EXISTING_CONFIG, encoding="utf-8")
        setup.patch_bothouse_config(self.config, setup.load_manifest(self.manifest), self.data_dir)
        executable = self.make_fake_bothouse()
        with mock_service() as server:
            probe = setup.probe_endpoint
            urls = []
            def local_probe(url):
                urls.append(url)
                return probe(local_url(server))
            with patch.object(setup, "probe_endpoint", side_effect=local_probe):
                status, output, errors = self.command("doctor", "--bothouse", executable, "--config", self.config)
            self.assertEqual(len(server.calls), 4)
        self.assertEqual((status, errors), (0, ""))
        report = json.loads(output)
        self.assertTrue(report["bothouse"]["setup_clis_ok"])
        ju, codex, _ = report["config"]["discord_projects"]
        self.assertTrue(ju["guild_id_valid"] and ju["ack_reaction"] and ju["proxy_configured"])
        self.assertFalse(ju["group_reply_all"])
        self.assertTrue(codex["group_reply_all"] and codex["codex_home_isolated"])
        self.assertTrue(report["gate"]["ok"])
        self.assertTrue(all(entry["ok"] for entry in report["jev"]))
        self.assertEqual(urls, [
            "http://127.0.0.1:9930/whoami", "http://127.0.0.1:9941/v1/models",
            "http://127.0.0.1:11435/v1/models", "http://127.0.0.1:11434/v1/models",
        ])


class HygieneTests(unittest.TestCase):
    def test_shell_scripts_use_lf(self):
        for name in ("start-mlx-jev.sh", "install-bothouse-mac.sh"):
            data = (setup.KIT_DIR / "mac" / name).read_bytes()
            with self.subTest(script=name):
                self.assertNotIn(b"\r", data)
                self.assertTrue(data.startswith(b"#!/usr/bin/env bash\n"))
                self.assertTrue(data.endswith(b"\n"))

    def test_powershell_script_parses(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell unavailable")
        script = setup.KIT_DIR / "windows" / "Install-PureDiscord.ps1"
        command = (
            "$tokens = $null; $errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$env:PD_TEST_PARSE_FILE, [ref]$tokens, [ref]$errors); "
            "if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }"
        )
        env = os.environ | {"PD_TEST_PARSE_FILE": str(script)}
        result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, env=env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_snowflakes_or_private_paths_in_kit(self):
        for path in setup.KIT_DIR.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            with self.subTest(file=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotRegex(source, r"(?<!\d)[1-9]\d{16,19}(?!\d)")
                self.assertNotRegex(source, r"(?i)(?:[a-z]:[\\/](?:users|documents and settings)[\\/]|/(?:Users|home)/)[\w.-]+")
                self.assertNotRegex(source, r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b")


if __name__ == "__main__":
    unittest.main()
