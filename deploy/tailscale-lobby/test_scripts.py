import json
from pathlib import Path
import re
import subprocess
import tomllib
import unittest


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SCRIPTS = (
    "deploy/windows-team/Start-Lobby.ps1",
    "deploy/tailscale-lobby/Enable-TailscaleLobbyServe.ps1",
    "deploy/tailscale-lobby/Test-TailscaleLobby.ps1",
)
TOKEN_REF = "${BOTHOUSE_LOBBY_BOT_TOKEN}"


class DeploymentTests(unittest.TestCase):
    def run_powershell(self, *args):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_powershell_ast(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                command = f"""
$tokens = $null
$errors = $null
$path = Join-Path (Get-Location) '{script}'
$null = [System.Management.Automation.Language.Parser]::ParseFile(
    $path, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{
    $errors | Out-String | Write-Error
    exit 1
}}
"""
                self.run_powershell("-Command", command)

    def load_toml(self, name):
        return tomllib.loads((HERE / name).read_text(encoding="utf-8"))["projects"]

    def assert_cloud_web(self, platform):
        self.assertEqual(platform["type"], "cloud_web")
        options = platform["options"]
        self.assertEqual(options["transport"], "websocket")
        self.assertEqual(options["token"], TOKEN_REF)
        self.assertEqual(options["allow_from"], "user")
        self.assertIs(options["share_session_in_channel"], True)
        self.assertIs(options["group_reply_all"], False)
        return options

    def test_remote_toml(self):
        project = self.load_toml("remote-cloud-web.toml.example")
        (platform,) = project["platforms"]
        options = self.assert_cloud_web(platform)
        self.assertEqual(
            options["ws_url"],
            "ws://<LOCAL_TAILSCALE_IP>:8790/cloud-web/ws?"
            "bot=<LEAD_BOT_ID>&mention_id=<LEAD_MENTION_ID>&name=<LEAD_NAME>",
        )

    def test_local_toml(self):
        project = self.load_toml("local-codex-cloud-web.toml.example")
        agent = project["agent"]
        self.assertEqual(agent["type"], "codex")
        self.assertEqual(agent["options"]["work_dir"], "D:/CC+CODEX")
        self.assertEqual(agent["options"]["mode"], "full-auto")
        self.assertTrue(agent["options"]["codex_home"])
        self.assertTrue(agent["options"]["cmd"])
        (platform,) = project["platforms"]
        options = self.assert_cloud_web(platform)
        self.assertEqual(
            options["ws_url"],
            "ws://127.0.0.1:8790/cloud-web/ws?"
            "bot=<CODEX_BOT_ID>&mention_id=<CODEX_MENTION_ID>&name=<CODEX_NAME>",
        )
        self.assertEqual(options["team_lead_bot"], "<LEAD_MENTION_ID>")

    def test_hujson_grant(self):
        source = (HERE / "tailscale-acl.grants.example.json").read_text(encoding="utf-8")
        source = re.sub(r'("(?:[^"\\]|\\.)*")|//[^\r\n]*', r"\1", source)
        source = re.sub(r",\s*(?=[}\]])", "", source)
        self.assertEqual(
            json.loads(source),
            {"grants": [
                {"src": ["100.64.0.20/32"], "dst": ["100.64.0.10/32"], "ip": ["tcp:8790"]},
            ]},
        )

    def run_dry_run(self, *flags):
        output = self.run_powershell(
            "-File", SCRIPTS[1], "-DryRun", "-TailscaleExe", "tailscale-test-no-executable", *flags
        )
        self.assertIn("Dry run: Tailnet-only Tailscale Serve.", output)
        self.assertNotIn("funnel", output.lower())
        return output

    def test_tcp_dry_run(self):
        output = self.run_dry_run()
        self.assertIn("serve --bg --yes --tcp 8790 tcp://127.0.0.1:8790", output)
        self.assertIn("serve status --json", output)
        self.assertNotIn("--https", output)

    def test_https_dry_run(self):
        output = self.run_dry_run("-EnableHttps")
        self.assertIn("serve --bg --yes --tcp 8790 tcp://127.0.0.1:8790", output)
        self.assertIn("serve --bg --yes --https=443 http://127.0.0.1:8790", output)
        self.assertIn("restore TCP if needed", output)

    def test_disable_dry_run(self):
        output = self.run_dry_run("-Disable")
        self.assertIn("serve --tcp=8790 off", output)
        self.assertIn("serve --https=443 off", output)
        self.assertNotIn("--bg", output)


if __name__ == "__main__":
    unittest.main()
