"""Loopback OpenAI-compatible shim for Ollama's thinking models."""

import argparse
import hmac
import http.client
import ipaddress
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def is_loopback_ip(raw_ip: str | None) -> bool:
    if raw_ip is None:
        return False
    try:
        address = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def is_loopback_request(peer_ip: str | None, headers) -> bool:
    if not is_loopback_ip(peer_ip):
        return False
    forwarded = {"x-forwarded-for", "forwarded", "tailscale-user-login"}
    hosts = []
    for name, value in headers.items():
        name = name.lower()
        if name in forwarded:
            return False
        if name == "host":
            hosts.append(value)
    if len(hosts) != 1 or any(character.isspace() for character in hosts[0]):
        return False
    try:
        authority = urlsplit(f"//{hosts[0]}")
        hostname = authority.hostname
        # Read port to detect malformed values, even though it is not used.
        authority.port
    except ValueError:
        return False
    if authority.username is not None or any(c in hosts[0] for c in "/?#"):
        return False
    return hostname == "localhost" or is_loopback_ip(hostname)


def rewrite_ollama_jev_body(payload: dict) -> dict:
    out = payload.copy()
    ctk = payload.get("chat_template_kwargs")
    if isinstance(ctk, dict) and ctk.get("enable_thinking") is False:
        out.setdefault("reasoning_effort", "none")
        remaining = {key: value for key, value in ctk.items() if key != "enable_thinking"}
        if remaining:
            out["chat_template_kwargs"] = remaining
        else:
            out.pop("chat_template_kwargs")
    return out


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ShimHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if sys.stderr is not None:
            super().log_message(format, *args)

    def __getattr__(self, name):
        # Route every HTTP method through the same access and path checks.
        if name.startswith("do_"):
            return self.handle_route
        raise AttributeError(name)

    def send_body(self, status, content_type, body):
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def reject(self, status, error):
        body = json.dumps({"ok": False, "error": error}).encode("utf-8")
        self.send_body(status, "application/json", body)

    def handle_route(self):
        if not is_loopback_request(self.client_address[0], self.headers):
            return self.reject(403, "loopback only")

        token = self.server.token
        if token:
            auth = self.headers.get_all("Authorization", [])
            scheme, _, supplied = auth[0].partition(" ") if len(auth) == 1 else ("", "", "")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                supplied.encode("utf-8"), token.encode("utf-8")
            ):
                return self.reject(401, "unauthorized")

        raw_target = self.requestline.split()[1]
        path, separator, query = raw_target.partition("?")
        method = {"/v1/models": "GET", "/v1/chat/completions": "POST"}.get(path)
        if method is None:
            return self.reject(404, "not found")
        if method != self.command:
            return self.reject(405, "method not allowed")

        data = None
        headers = {}
        if method == "POST":
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise ValueError
                length = int(lengths[0])
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError
                data = json.dumps(rewrite_ollama_jev_body(payload)).encode("utf-8")
            except (ValueError, OverflowError, RecursionError):
                return self.reject(400, "invalid json")
            headers["Content-Type"] = "application/json"

        request = Request(self.server.upstream + path + separator + query,
                          data=data, headers=headers, method=method)
        try:
            opener = build_opener(ProxyHandler({}), NoRedirects())
            try:
                response = opener.open(request, timeout=30.0)
            except HTTPError as error:
                response = error
            with response:
                status, content_type = response.code, response.headers.get("Content-Type")
                body = response.read()
        except (URLError, TimeoutError, OSError, http.client.HTTPException, ValueError):
            return self.reject(502, "ollama unreachable")
        self.send_body(status, content_type, body)


class IPv6HTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def create_server(host, port, upstream, token="") -> ThreadingHTTPServer:
    if not is_loopback_ip(host):
        raise ValueError("host must be a loopback IP")
    server_type = IPv6HTTPServer if ":" in host else ThreadingHTTPServer
    server = server_type((host, port), ShimHandler)
    server.token = token
    server.upstream = upstream.rstrip("/")
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=os.environ.get("OLLAMA_JEV_PORT", "11435"))
    parser.add_argument("--upstream", default=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    parser.add_argument("--token", default=os.environ.get("OLLAMA_JEV_TOKEN", ""))
    args = parser.parse_args(argv)
    if not is_loopback_ip(args.host):
        parser.error("--host must be a loopback IP")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        with create_server(args.host, args.port, args.upstream, args.token) as server:
            if sys.stdout is not None:
                print(f"Ollama JEV shim listening on {args.host}:{server.server_port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as error:
        parser.exit(2, f"Cannot start Ollama JEV shim: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
