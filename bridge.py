#!/usr/bin/env python3
"""Read Codex usage from the local app-server and serve a badge-safe view."""

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import selectors
import subprocess
import threading
import time


SCHEMA_VERSION = 1
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


class AppServerError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(self, codex_command="codex", timeout=15):
        self.codex_command = codex_command
        self.timeout = timeout
        self.process = None
        self.selector = None
        self.next_id = 1
        self.lock = threading.Lock()

    def start(self):
        self.close()
        self.process = subprocess.Popen(
            [self.codex_command, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self._rpc(
            "initialize",
            {
                "clientInfo": {
                    "name": "tildagon-codex-usage-bridge",
                    "title": "Tildagon Codex Usage Bridge",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": False},
            },
        )
        self._write({"method": "initialized"})

    def close(self):
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process = None

    def _write(self, message):
        if self.process is None or self.process.poll() is not None:
            raise AppServerError("Codex app-server is not running")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _read_message(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerError("Codex app-server timed out")
        if not self.selector.select(remaining):
            raise AppServerError("Codex app-server timed out")
        line = self.process.stdout.readline()
        if not line:
            raise AppServerError("Codex app-server closed its output")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise AppServerError("Codex app-server returned invalid JSON") from exc

    def _rpc(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        request = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self._write(request)
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._read_message(deadline)
            if message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                error = message["error"]
                if isinstance(error, dict):
                    error = error.get("message", error)
                raise AppServerError(str(error))
            if "result" not in message:
                raise AppServerError("Codex app-server response has no result")
            return message["result"]

    def read_rate_limits(self):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.start()
            return self._rpc("account/rateLimits/read")


def _reset_label(timestamp):
    if not isinstance(timestamp, int):
        return "RESET UNKNOWN"
    value = datetime.fromtimestamp(timestamp).astimezone()
    return "%s %02d %s · %02d:%02d" % (
        WEEKDAYS[value.weekday()],
        value.day,
        MONTHS[value.month - 1],
        value.hour,
        value.minute,
    )


def _kind_for(duration, fallback):
    if isinstance(duration, int):
        if 240 <= duration <= 360:
            return "five_hour"
        if 9000 <= duration <= 11000:
            return "weekly"
    return fallback


def normalize_rate_limits(result, fetched_at=None):
    if not isinstance(result, dict):
        raise AppServerError("rate-limit response is not an object")
    buckets = result.get("rateLimitsByLimitId")
    snapshot = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(snapshot, dict):
        snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        raise AppServerError("Codex rate-limit bucket is missing")

    windows = []
    seen = set()
    for field, fallback in (("primary", "five_hour"), ("secondary", "weekly")):
        source = snapshot.get(field)
        if not isinstance(source, dict):
            continue
        duration = source.get("windowDurationMins")
        kind = _kind_for(duration, fallback)
        if kind in seen:
            continue
        try:
            used = int(source["usedPercent"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AppServerError("%s window has no usage percentage" % kind) from exc
        reset_at = source.get("resetsAt")
        windows.append(
            {
                "kind": kind,
                "window_minutes": duration,
                "remaining_percent": max(0, min(100, 100 - used)),
                "resets_at": reset_at,
                "reset_label": _reset_label(reset_at),
            }
        )
        seen.add(kind)

    if not windows:
        raise AppServerError("Codex returned no usage windows")
    windows.sort(key=lambda row: 0 if row["kind"] == "weekly" else 1)
    return {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": int(time.time()) if fetched_at is None else int(fetched_at),
        "timezone": "host-local",
        "windows": windows,
    }


class UsageProvider:
    def __init__(self, app_server):
        self.app_server = app_server

    def fetch(self):
        try:
            return normalize_rate_limits(self.app_server.read_rate_limits())
        except Exception:
            self.app_server.close()
            return normalize_rate_limits(self.app_server.read_rate_limits())


def make_handler(provider, token):
    class UsageHandler(BaseHTTPRequestHandler):
        server_version = "CodexUsageBridge/1.0"

        def _json(self, status, payload):
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/v1/usage":
                self._json(404, {"error": "not found"})
                return
            supplied = self.headers.get("Authorization", "")
            expected = "Bearer " + token
            if not secrets.compare_digest(supplied, expected):
                self._json(401, {"error": "unauthorized"})
                return
            try:
                self._json(200, provider.fetch())
            except Exception as exc:
                self._json(503, {"error": str(exc)[:160] or "usage unavailable"})

        def log_message(self, message, *args):
            print("bridge: " + (message % args))

    return UsageHandler


def _load_local_config():
    path = Path(__file__).with_name("bridge.local.json")
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def parse_args(argv=None):
    local = _load_local_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bind", default=local.get("bind", "127.0.0.1"), help="LAN address to bind"
    )
    parser.add_argument("--port", type=int, default=int(local.get("port", 8765)))
    parser.add_argument(
        "--token", default=os.environ.get("CODEX_USAGE_TOKEN", local.get("token"))
    )
    parser.add_argument("--codex-command", default=local.get("codex_command", "codex"))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.token:
        raise SystemExit("set CODEX_USAGE_TOKEN or pass --token")
    app_server = CodexAppServer(args.codex_command)
    provider = UsageProvider(app_server)
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(provider, args.token))
    print("Codex usage bridge listening on http://%s:%d" % (args.bind, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app_server.close()


if __name__ == "__main__":
    main()
