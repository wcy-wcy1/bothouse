"""Set up a Bothouse Discord team from a local TOML manifest."""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit("Python 3.11 or newer is required. Install it and retry with that interpreter.")

from ollama_jev_shim import NoRedirects

KIT_DIR = Path(__file__).resolve().parent
PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"
DISCORD_API = "https://discord.com/api/v10"
PROJECT_PATTERN = r"[a-z0-9][a-z0-9_-]{0,31}"
ID_PATTERN = r"[0-9]{17,20}"


def bothouse_home():
    return Path.home() / ".bothouse"


def read_toml(path):
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        raise ValueError(f"Invalid TOML in {path.name}") from None


def load_personas():
    path = PERSONAS_DIR / "personas.json"
    return {preset["id"]: preset for preset in json.loads(path.read_text(encoding="utf-8"))["presets"]}


def field(table, key, kind=str, default=None, label=""):
    value = table.get(key, default)
    if key in table and type(value) is not kind:
        raise ValueError(f"{label}{key} must be {kind.__name__}")
    return value


def checked_url(value, label):
    if not value:
        return value
    try:
        parts = urlsplit(value)
        if parts.scheme in {"http", "https"} and parts.hostname and not any(c.isspace() for c in value):
            parts.port
            return value
    except ValueError:
        pass
    raise ValueError(f"{label} must be an HTTP or HTTPS URL")


def resolve_secret(table, key, env_key, label, required=False):
    direct = field(table, key, label=label)
    env_name = field(table, env_key, label=label)
    secret = direct or (os.environ.get(env_name, "") if env_name else "")
    if not secret.strip() or re.fullmatch(r"<[^<>]+>", secret.strip()):
        if required or direct or env_name:
            raise ValueError(f"{label}{key} is missing. Supply {key} or set the variable named by {env_key}.")
        return ""
    if any(c.isspace() for c in secret):
        raise ValueError(f"{label}{key} contains whitespace")
    return secret


def load_manifest(path):
    manifest = read_toml(path)
    discord = field(manifest, "discord", dict, {})
    guild = field(discord, "guild_id", label="discord.") or ""
    channel = field(discord, "channel_id", default="", label="discord.")
    if not re.fullmatch(ID_PATTERN, guild):
        raise ValueError("discord.guild_id must contain 17–20 digits")
    if channel and not re.fullmatch(ID_PATTERN, channel):
        raise ValueError("discord.channel_id must contain 17–20 digits or be empty")
    for key in ("allow_from", "language", "fallback_role"):
        field(discord, key, label="discord.")
    checked_url(field(discord, "proxy", label="discord."), "discord.proxy")
    field(discord, "ack_reaction", bool, True, "discord.")

    gate = field(manifest, "gate", dict, {})
    field(gate, "enabled", bool, True, "gate.")
    if field(gate, "mode", default="addressed", label="gate.") not in {"addressed", "smart"}:
        raise ValueError("gate.mode must be addressed or smart")

    jev = field(manifest, "jev", dict, {})
    checked_url(field(jev, "base_url", label="jev."), "jev.base_url")
    field(jev, "model", label="jev.")
    remove_jev = field(jev, "remove", bool, False, "jev.")
    if "jev" in manifest:
        key = resolve_secret(jev, "api_key", "api_key_env", "jev.") if not remove_jev else ""
        if key:
            jev["api_key"] = key

    roles = field(manifest, "roles", list, [])
    if not roles:
        raise ValueError("At least one [[roles]] entry is required")
    projects = set()
    for index, role in enumerate(roles, 1):
        label = f"roles[{index}]."
        if not isinstance(role, dict):
            raise ValueError(f"roles[{index}] must be a table")
        project = field(role, "project", default="", label=label)
        if not re.fullmatch(PROJECT_PATTERN, project):
            raise ValueError(f"{label}project must match {PROJECT_PATTERN}")
        if project in projects:
            raise ValueError(f"Duplicate role project: {project}")
        projects.add(project)
        role["token"] = resolve_secret(role, "token", "token_env", label, required=True)
        for key in ("agent_type", "name", "model", "work_dir", "persona", "persona_file",
                    "avatar", "avatar_file", "codex_home"):
            field(role, key, label=label)
        if not field(role, "agent_type", default="claudecode", label=label):
            raise ValueError(f"{label}agent_type must be non-empty")
        field(role, "edit_persona", bool, label=label)
        field(role, "codex_home_isolate", bool, label=label)
        provider = field(role, "provider", dict, {}, label)
        for key in ("vendor", "base_url", "api_key", "model"):
            field(provider, key, label=label + "provider.")
        field(provider, "max_tokens", int, label=label + "provider.")
        if role.get("avatar") and not re.fullmatch(r"[a-zA-Z0-9_-]+", role["avatar"]):
            raise ValueError(f"{label}avatar must be a library slug")
    if discord.get("fallback_role") and discord["fallback_role"] not in projects:
        raise ValueError("discord.fallback_role must identify a role in the manifest")
    return manifest


def manifest_file(value, directory):
    return (directory / Path(value).expanduser()).resolve()


def build_apply_payload(manifest, manifest_dir=Path(".")):
    discord = manifest["discord"]
    gate = manifest.get("gate", {})
    presets = load_personas()
    payload = {
        "platform": "discord",
        "language": discord.get("language", "zh"),
        "discord": {"guild_id": discord["guild_id"], "channel_id": discord.get("channel_id", "")},
        "gate": {"enabled": gate.get("enabled", True), "mode": gate.get("mode", "addressed")},
        "roles": [],
    }
    payload.update({key: discord[key] for key in ("allow_from", "fallback_role") if key in discord})
    if "jev" in manifest:
        jev = manifest["jev"]
        payload["jev"] = {key: jev[key] for key in ("base_url", "model", "api_key", "remove") if key in jev}
    for role in manifest["roles"]:
        fields = ("project", "token", "name", "model", "work_dir", "persona", "avatar", "edit_persona")
        entry = {key: role[key] for key in fields if key in role}
        entry["agent_type"] = role.get("agent_type", "claudecode")
        if role.get("provider") is not None:
            provider_fields = ("vendor", "base_url", "api_key", "model", "max_tokens")
            entry["provider"] = {key: role["provider"][key] for key in provider_fields if key in role["provider"]}
        preset = presets.get(role.get("persona"))
        if preset:
            slug = preset["id"]
            entry["persona"] = (PERSONAS_DIR / "prompts" / f"{slug}.md").read_text(encoding="utf-8")
            entry["persona_preset"] = slug
            if preset.get("avatar"):
                entry.setdefault("avatar", preset["avatar"])
        if role.get("persona_file"):
            entry["persona"] = manifest_file(role["persona_file"], manifest_dir).read_text(encoding="utf-8")
        payload["roles"].append(entry)
    return payload


def init_manifest(output, force):
    source = (KIT_DIR / "team.toml.example").read_bytes()
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(output, flags, 0o600)
    except FileExistsError:
        raise ValueError("Manifest already exists. Use --force to replace it.") from None
    with os.fdopen(fd, "wb") as stream:
        if os.name == "posix":
            os.fchmod(stream.fileno(), 0o600)
        stream.write(source)
    print(f"Manifest created: {output}")


def toml_value(value):
    # TOML basic strings and JSON strings share the escapes used here.
    return json.dumps(value, ensure_ascii=False).replace(chr(127), r"\u007f")


def table_headers(lines):
    """Find actual headers, including ones after multiline TOML values."""
    for index, line in enumerate(lines):
        match = re.fullmatch(r"\s*(\[\[?)(.*?)\]\]?\s*(?:#.*)?", line)
        if match is None:
            continue
        try:
            tomllib.loads("".join(lines[:index + 1]))
        except tomllib.TOMLDecodeError:
            continue
        yield index, re.sub(r"\s*\.\s*", ".", match[2].strip()), match[1] == "[["


def set_toml_key(lines, start, stop, key, value):
    assignment = f"{key} = {toml_value(value)}"
    key_pattern = re.compile(rf"""\s*(?:{key}|'{key}'|"{key}")\s*=""")
    for index in range(start, stop):
        if not key_pattern.match(lines[index]):
            continue
        try:
            tomllib.loads("".join(lines[:index]))
        except tomllib.TOMLDecodeError:
            continue
        for end in range(index + 1, stop + 1):
            old = "".join(lines[index:end]).lstrip()
            try:
                tomllib.loads(old)
            except tomllib.TOMLDecodeError:
                continue
            for comment in re.finditer("#", old.splitlines()[-1]):
                offset = len(old.rstrip("\n")) - len(old.splitlines()[-1]) + comment.start()
                try:
                    tomllib.loads(old[:offset])
                except tomllib.TOMLDecodeError:
                    continue
                assignment += " " + old[offset:].rstrip("\n")
                break
            lines[index:end] = [assignment + "\n"]
            return
    if start and not lines[start - 1].endswith("\n"):
        lines[start - 1] += "\n"
    lines.insert(start, assignment + "\n")


def set_section_options(lines, headers, start, stop, table, values):
    """Edit an options table within the selected project or platform block."""
    section = table + ".options"
    for index, (position, name, _) in enumerate(headers):
        if start <= position < stop and name == section:
            end = headers[index + 1][0] if index + 1 < len(headers) else stop
            for key, value in values.items():
                set_toml_key(lines, position + 1, end, key, value)
                # Recompute after insertion or replacement of a multiline value.
                end = next((i for i, _, _ in table_headers(lines) if i > position), len(lines))
            return
    if stop and not lines[stop - 1].endswith("\n"):
        lines[stop - 1] += "\n"
    lines[stop:stop] = [f"\n[{section}]\n", *(f"{k} = {toml_value(v)}\n" for k, v in values.items())]


def replace_toml(path, content, original):
    path = path.resolve()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            if original is not None and os.name == "posix":
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(content.encode("utf-8"))
            stream.close()
            if (path.read_bytes() if path.exists() else None) != original:
                raise ValueError(f"{path.name} changed while it was being updated. Retry the command.")
            temporary.replace(path)
        finally:
            if not stream.closed:
                stream.close()
            temporary.unlink(missing_ok=True)


def patch_bothouse_config(config_path: Path, manifest: dict, data_dir: Path) -> list[str]:
    """Patch matching wizard project tables while preserving the rest of the file."""
    original = config_path.read_bytes()
    source = original.decode("utf-8")
    if "\r\n" in source and "\n" not in source.replace("\r\n", ""):
        newline = "\r\n"
    else:
        newline = "\n"
    parsed = tomllib.loads(source)
    expected = deepcopy(parsed)
    lines = source.replace("\r\n", "\n").splitlines(keepends=True)
    roles = {role["project"]: role for role in manifest["roles"]}
    discord = manifest["discord"]
    discord_updates = {}
    if discord.get("proxy"):
        discord_updates["proxy"] = discord["proxy"]
    if discord.get("ack_reaction", True):
        discord_updates["ack_reaction"] = True
    messages = []
    codex_configs = {}

    for index, project in enumerate(expected.get("projects", [])):
        name = project.get("name")
        role = roles.get(name)
        if not role:
            continue

        def project_headers():
            headers = list(table_headers(lines))
            starts = [i for i, name, array in headers if name == "projects" and array]
            return headers, starts[index], (starts + [len(lines)])[index + 1]

        platforms = project.get("platforms", [])
        for platform_index, platform in enumerate(platforms):
            if platform.get("type") != "discord" or not discord_updates:
                continue
            headers, start, stop = project_headers()
            platforms_at = [i for i, name, array in headers
                            if start < i < stop and name == "projects.platforms" and array]
            set_section_options(lines, headers, platforms_at[platform_index],
                                (platforms_at + [stop])[platform_index + 1],
                                "projects.platforms", discord_updates)
            platform.setdefault("options", {}).update(discord_updates)
            messages.append(f"{name}: updated Discord options")

        if role.get("agent_type") != "codex" or not role.get("codex_home_isolate", True):
            continue
        configured_home = role.get("codex_home")
        home = (Path(configured_home).expanduser() if configured_home
                else data_dir / "codex-home" / name).resolve()
        home_config = home / "config.toml"
        home_original = home_config.read_bytes() if home_config.exists() else None
        home_source = home_original.decode("utf-8") if home_original is not None else ""
        home_lines = home_source.replace("\r\n", "\n").splitlines(keepends=True)
        end = next((i for i, _, _ in table_headers(home_lines)), len(home_lines))
        expected_home = tomllib.loads(home_source)
        expected_home["sqlite_home"] = home.as_posix()
        set_toml_key(home_lines, 0, end, "sqlite_home", home.as_posix())
        home_content = "".join(home_lines)
        if tomllib.loads(home_content) != expected_home:
            raise ValueError("Unable to safely update the Codex sqlite_home")
        codex_configs[home_config] = (home_content, home_original)

        headers, start, stop = project_headers()
        set_section_options(lines, headers, start, stop, "projects.agent",
                            {"codex_home": home.as_posix()})
        project.setdefault("agent", {}).setdefault("options", {})["codex_home"] = home.as_posix()
        messages.append(f"{name}: isolated Codex home")

    content = "".join(lines)
    if tomllib.loads(content) != expected:
        raise ValueError("Unable to safely update the Bothouse project options")
    for path, (home_content, home_original) in codex_configs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if b"\r\n" in (home_original or b""):
            home_content = home_content.replace("\n", "\r\n")
        if home_content.encode("utf-8") != home_original:
            replace_toml(path, home_content, home_original)
    if newline == "\r\n":
        content = content.replace("\n", "\r\n")
    if content.encode("utf-8") != original:
        replace_toml(config_path, content, original)
    return messages

IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}


def avatar_path(role, manifest_dir, presets):
    if role.get("avatar_file"):
        return manifest_file(role["avatar_file"], manifest_dir)
    slug = role.get("avatar", presets.get(role.get("persona"), {}).get("avatar"))
    return PERSONAS_DIR / "avatars" / f"{slug}.png" if slug else None


def upload_discord_avatar(token, image_path, proxy="", api_base=DISCORD_API):
    image_type = IMAGE_TYPES.get(image_path.suffix.lower())
    if image_type is None:
        raise ValueError("Avatar must be a PNG, JPEG, or WebP image")
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    data = json.dumps({"avatar": f"data:image/{image_type};base64,{encoded}"}).encode("utf-8")
    request = Request(api_base.rstrip("/") + "/users/@me", data=data, method="PATCH",
                      headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"})
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    opener = build_opener(ProxyHandler(proxies), NoRedirects())
    try:
        with opener.open(request, timeout=15.0):
            pass
    except HTTPError as error:
        error.close()
        raise ValueError(f"Discord rejected the avatar upload (HTTP {error.code})") from None
    except (URLError, OSError):
        raise ValueError("Could not connect to the Discord API") from None


def set_avatars(args, manifest, manifest_dir):
    roles = [role for role in manifest["roles"] if not args.role or args.role == role["project"]]
    if not roles:
        raise ValueError("No role matches --role")
    proxy = checked_url(args.proxy, "--proxy") if args.proxy is not None else manifest["discord"].get("proxy", "")
    api_base = checked_url(args.api_base, "--api-base")
    if not api_base:
        raise ValueError("--api-base must be an HTTP or HTTPS URL")
    presets = load_personas()
    for role in roles:
        name = role["project"]
        image = avatar_path(role, manifest_dir, presets)
        if image is None:
            print(f"{name}: no avatar configured")
            continue
        if not image.is_file() or image.suffix.lower() not in IMAGE_TYPES:
            raise ValueError(f"{name}: avatar file is missing or has an unsupported extension")
        if args.dry_run:
            print(f"{name}: would upload {image.name}")
        else:
            upload_discord_avatar(role["token"], image, proxy, api_base)
            print(f"{name}: avatar uploaded")
    return 0


def summarize_apply(result, redact):
    print(f"Saved: {'yes' if result.get('saved') else 'no'}"
          + (" (dry run)" if result.get("dry_run") else ""))
    if result.get("needs_user_action"):
        print("User action required.")
    if result.get("error"):
        print(redact(f"Error: {result['error']}"))
    for role in result.get("roles") or []:
        print(redact(f"Role: {role['project']} ({role.get('name', '')}, {role.get('agent_type', '')})"))
    invites = dict.fromkeys(result.get("invite_urls") or [])
    for role in result.get("roles") or []:
        if role.get("invite_url"):
            invites[role["invite_url"]] = None
    for url in invites:
        print(redact(f"Invite: {url}"))
    for warning in result.get("warnings") or []:
        scope = warning.get("project") or warning.get("code", "setup")
        print(redact(f"Warning [{scope}]: {warning.get('message', '')}"))
        for key in ("fix", "url"):
            if warning.get(key):
                print(redact(f"  {warning[key]}"))
    for step in result.get("next_steps") or []:
        print(redact(f"Next: {step}"))


def apply_manifest(args, manifest, manifest_dir):
    payload = build_apply_payload(manifest, manifest_dir)
    proxy = manifest["discord"].get("proxy")
    env = os.environ.copy()
    if proxy:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(payload, stream)
            stream.close()
            command = [args.bothouse, "setup", "apply", "--json", str(temporary)]
            if args.config is not None:
                command.extend(["--config", str(args.config.expanduser())])
            if args.dry_run:
                command.append("--dry-run")
            run = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", env=env)
        finally:
            if not stream.closed:
                stream.close()
            temporary.unlink(missing_ok=True)
    try:
        result = json.loads(run.stdout)
        if not isinstance(result, dict):
            raise ValueError
    except ValueError:
        raise ValueError(f"Bothouse setup apply returned no JSON result (exit {run.returncode})") from None

    secrets = {role["token"] for role in payload["roles"]}
    secrets.update(role.get("provider", {}).get("api_key") for role in payload["roles"])
    secrets.add(payload.get("jev", {}).get("api_key"))
    if proxy:
        secrets.add(urlsplit(proxy).password)
    ordered_secrets = sorted(filter(None, secrets), key=len, reverse=True)

    def redact(message):
        for secret in ordered_secrets:
            message = message.replace(secret, "<redacted>")
        return message

    summarize_apply(result, redact)
    if args.dry_run:
        return run.returncode
    if result.get("saved") is True:
        path = (args.config or bothouse_home() / "config.toml").expanduser()
        if path.is_file():
            configured_data_dir = read_toml(path).get("data_dir")
            data_dir = Path(configured_data_dir).expanduser() if configured_data_dir else bothouse_home()
            for message in patch_bothouse_config(path, manifest, data_dir):
                print(message)
    if args.no_avatars:
        return run.returncode

    warned = {w.get("project") for w in result.get("warnings") or []
              if w.get("code") == "discord_avatar_not_set"}
    display_names = {r["project"]: r.get("name") for r in result.get("roles") or []}
    presets = load_personas()
    avatar_failed = False
    for role in manifest["roles"]:
        project = role["project"]
        if not (role.get("avatar_file") or project in warned or display_names.get(project) in warned):
            continue
        try:
            image = avatar_path(role, manifest_dir, presets)
            if image is not None:
                upload_discord_avatar(role["token"], image, proxy or "")
                print(f"{project}: avatar uploaded")
        except (ValueError, OSError) as error:
            print(redact(f"{project}: avatar upload failed: {error}"), file=sys.stderr)
            avatar_failed = True
    return run.returncode or int(avatar_failed)


def probe_endpoint(url):
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(url, timeout=1.5) as response:
            return {"url": url, "ok": True, "status": response.status}
    except HTTPError as error:
        error.close()
        return {"url": url, "ok": False, "status": error.code}
    except (URLError, OSError):
        return {"url": url, "ok": False}


def doctor(args):
    checks = {"python": sys.version.split()[0], "bothouse": {"found": bool(shutil.which(args.bothouse))}}
    if checks["bothouse"]["found"]:
        try:
            run = subprocess.run([args.bothouse, "setup", "clis"], capture_output=True, encoding="utf-8",
                                 errors="replace", timeout=30)
            checks["bothouse"]["setup_clis_ok"] = run.returncode == 0
            if run.returncode == 0:
                clis = json.loads(run.stdout)
                checks["bothouse"]["clis"] = [
                    {key: cli[key] for key in ("agent_type", "binary", "version", "login", "supported") if key in cli}
                    for cli in (clis or [])
                ]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            checks["bothouse"]["setup_clis_ok"] = False

    path = (args.config or bothouse_home() / "config.toml").expanduser()
    checks["config"] = {"exists": path.is_file(), "discord_projects": []}
    if path.is_file():
        try:
            config = read_toml(path)
            for project in config.get("projects", []):
                agent = project.get("agent", {})
                home = agent.get("options", {}).get("codex_home")
                for platform in project.get("platforms", []):
                    if platform.get("type") != "discord":
                        continue
                    opts = platform.get("options", {})
                    entry = {
                        "name": project.get("name"),
                        "guild_id_valid": bool(re.fullmatch(ID_PATTERN, str(opts.get("guild_id", "")))),
                        "group_reply_all": opts.get("group_reply_all", False) is True,
                        "proxy_configured": bool(opts.get("proxy")),
                        "ack_reaction": opts.get("ack_reaction", False) is True,
                    }
                    if agent.get("type") == "codex":
                        entry["codex_home_isolated"] = False
                        if home:
                            home_path = Path(home).expanduser().resolve()
                            codex_config = home_path / "config.toml"
                            if codex_config.is_file():
                                sql_home = read_toml(codex_config).get("sqlite_home")
                                entry["codex_home_isolated"] = (
                                    bool(sql_home) and Path(sql_home).expanduser().resolve() == home_path
                                )
                    checks["config"]["discord_projects"].append(entry)
        except (OSError, ValueError, TypeError):
            checks["config"]["error"] = "Unable to read configuration"

    checks["gate"] = probe_endpoint("http://127.0.0.1:9930/whoami")
    jev_urls = [f"http://127.0.0.1:{port}/v1/models" for port in (9941, 11435, 11434)]
    if args.jev_url:
        checked_url(args.jev_url, "--jev-url")
        parts = urlsplit(args.jev_url)
        if parts.username is not None or parts.query or parts.fragment:
            raise ValueError("--jev-url must omit credentials, queries, and fragments")
        url = args.jev_url.rstrip("/")
        if not url.endswith("/models"):
            url += "/models"
        jev_urls.append(url)
    checks["jev"] = [probe_endpoint(url) for url in dict.fromkeys(jev_urls)]
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("personas", help="list bundled personas")
    init = commands.add_parser("init-manifest", help="copy the example manifest")
    init.add_argument("--output", type=Path, default=bothouse_home() / "pure-discord" / "team.toml")
    init.add_argument("--force", action="store_true")
    apply = commands.add_parser("apply", help="configure the Discord team with Bothouse")
    apply.add_argument("manifest", type=Path)
    apply.add_argument("--config", type=Path)
    apply.add_argument("--bothouse", default="bothouse")
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--no-avatars", action="store_true")
    avatars = commands.add_parser("set-avatars", help="upload the configured Discord avatars")
    avatars.add_argument("manifest", type=Path)
    avatars.add_argument("--role")
    avatars.add_argument("--proxy")
    avatars.add_argument("--api-base", default=DISCORD_API)
    avatars.add_argument("--dry-run", action="store_true")
    diagnosis = commands.add_parser("doctor", help="diagnose local Bothouse, Gate, and JEV")
    diagnosis.add_argument("--config", type=Path)
    diagnosis.add_argument("--bothouse", default="bothouse")
    diagnosis.add_argument("--jev-url")
    args = parser.parse_args(argv)

    # Use UTF-8 when stdout is captured, including on Windows with a legacy code page.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.command == "personas":
            for preset in load_personas().values():
                print("\t".join(preset.get(key, "") for key in ("id", "name", "model", "avatar", "intro")))
        elif args.command == "init-manifest":
            init_manifest(args.output, args.force)
        elif args.command == "doctor":
            return doctor(args)
        else:
            args.manifest = args.manifest.expanduser().resolve()
            manifest = load_manifest(args.manifest)
            if args.command == "apply":
                return apply_manifest(args, manifest, args.manifest.parent)
            return set_avatars(args, manifest, args.manifest.parent)
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
