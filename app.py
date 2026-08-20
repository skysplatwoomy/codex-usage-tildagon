import asyncio
import binascii
import hashlib
import json
import os
import socket
import time

import app
import async_helpers
import settings
import wifi
from events.input import BUTTON_TYPES, Buttons


CONFIG_KEY = "codex_usage"
DEFAULT_REFRESH_SECONDS = 60
DEFAULT_DISCOVERY_PORT = 8766
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 1
DEFAULT_DISCOVERY_ATTEMPTS = 3
DEFAULT_HTTP_TIMEOUT_SECONDS = 8
DISCOVERY_VERSION = 1
DISCOVERY_REQUEST_TYPE = "codex_usage_discover"
DISCOVERY_RESPONSE_TYPE = "codex_usage_bridge"
DISCOVERY_PROOF_PREFIX = "CODEX_USAGE_DISCOVERY_V1"
MAX_HTTP_RESPONSE_BYTES = 16 * 1024
TAU = 2 * 3.141592653589793


def _ticks_ms():
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.monotonic() * 1000)


def _ticks_diff(new, old):
    try:
        return time.ticks_diff(new, old)
    except AttributeError:
        return new - old


def _clamp(value, low, high):
    return max(low, min(high, value))


def _int_setting(saved, key, default, low, high):
    try:
        value = int(saved.get(key, default))
    except (TypeError, ValueError):
        value = default
    return _clamp(value, low, high)


def load_config():
    saved = settings.get(CONFIG_KEY, {})
    if not isinstance(saved, dict):
        saved = {}
    return {
        "bridge_url": str(saved.get("bridge_url", "")).rstrip("/"),
        "bridge_token": str(saved.get("bridge_token", "")),
        "discovery_port": _int_setting(
            saved, "discovery_port", DEFAULT_DISCOVERY_PORT, 0, 65535
        ),
        "http_timeout_seconds": _int_setting(
            saved,
            "http_timeout_seconds",
            DEFAULT_HTTP_TIMEOUT_SECONDS,
            2,
            30,
        ),
        "refresh_seconds": _int_setting(
            saved, "refresh_seconds", DEFAULT_REFRESH_SECONDS, 15, 3600
        ),
    }


def _sha256(data):
    digest = hashlib.sha256()
    digest.update(data)
    return digest.digest()


def _hmac_sha256_hex(key, message):
    if isinstance(key, str):
        key = key.encode()
    if isinstance(message, str):
        message = message.encode()
    if len(key) > 64:
        key = _sha256(key)

    padded = bytearray(64)
    padded[: len(key)] = key
    inner_pad = bytearray(64)
    outer_pad = bytearray(64)
    for index in range(64):
        inner_pad[index] = padded[index] ^ 0x36
        outer_pad[index] = padded[index] ^ 0x5C
    inner = _sha256(bytes(inner_pad) + message)
    return binascii.hexlify(_sha256(bytes(outer_pad) + inner)).decode()


def _discovery_proof_message(nonce, host, port):
    return "%s\n%s\n%s\n%d" % (DISCOVERY_PROOF_PREFIX, nonce, host, port)


def _constant_time_equal(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    different = len(left) ^ len(right)
    length = min(len(left), len(right))
    for index in range(length):
        different |= ord(left[index]) ^ ord(right[index])
    return different == 0


def _validated_discovery_url(payload, source, token, nonce):
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != DISCOVERY_RESPONSE_TYPE:
        return None
    if payload.get("version") != DISCOVERY_VERSION or payload.get("nonce") != nonce:
        return None

    host = payload.get("host")
    proof = payload.get("proof")
    try:
        port = int(payload.get("port"))
    except (TypeError, ValueError):
        return None
    if not isinstance(host, str) or not source or source[0] != host:
        return None
    if port < 1 or port > 65535:
        return None

    expected = _hmac_sha256_hex(
        token, _discovery_proof_message(nonce, host, port)
    )
    if not _constant_time_equal(proof, expected):
        return None
    return "http://%s:%d" % (host, port)


def _discover_bridge(token, discovery_port):
    nonce = binascii.hexlify(os.urandom(8)).decode()
    request = json.dumps(
        {
            "type": DISCOVERY_REQUEST_TYPE,
            "version": DISCOVERY_VERSION,
            "nonce": nonce,
        }
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(
            socket.SOL_SOCKET, getattr(socket, "SO_BROADCAST", 0x20), 1
        )
        sock.settimeout(DEFAULT_DISCOVERY_TIMEOUT_SECONDS)
        for _ in range(DEFAULT_DISCOVERY_ATTEMPTS):
            sock.sendto(request, ("255.255.255.255", discovery_port))
            try:
                raw, source = sock.recvfrom(1024)
            except OSError:
                continue
            try:
                payload = json.loads(raw.decode())
            except (TypeError, ValueError):
                continue
            url = _validated_discovery_url(payload, source, token, nonce)
            if url:
                return url
    finally:
        sock.close()
    raise OSError("bridge discovery failed")


def _split_http_url(url):
    if not url.startswith("http://"):
        raise ValueError("bridge URL must use HTTP")
    authority = url[7:].split("/", 1)[0]
    if ":" in authority:
        host, raw_port = authority.rsplit(":", 1)
        port = int(raw_port)
    else:
        host = authority
        port = 80
    if not host or port < 1 or port > 65535:
        raise ValueError("bad bridge URL")
    return host, port


def _fetch_usage_json(url, token, timeout_seconds):
    host, port = _split_http_url(url)
    address = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
    sock = socket.socket()
    try:
        sock.settimeout(timeout_seconds)
        sock.connect(address)
        request = (
            "GET /v1/usage HTTP/1.0\r\n"
            "Host: %s:%d\r\n"
            "Authorization: Bearer %s\r\n"
            "Connection: close\r\n\r\n"
        ) % (host, port, token)
        sock.sendall(request.encode())

        chunks = []
        size = 0
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_HTTP_RESPONSE_BYTES:
                raise ValueError("bridge response too large")
            chunks.append(chunk)
    finally:
        sock.close()

    raw = b"".join(chunks)
    try:
        header, body = raw.split(b"\r\n\r\n", 1)
        status = int(header.split(b"\r\n", 1)[0].split()[1])
    except (IndexError, TypeError, ValueError):
        raise ValueError("bad bridge response")
    if status != 200:
        raise OSError("bridge HTTP %d" % status)
    return json.loads(body.decode())


class CodexUsageApp(app.App):
    """Foreground-only dashboard for Codex rolling usage limits."""

    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)
        self.config = load_config()
        self.windows = {}
        self.loading = False
        self.error = None
        self.stale = False
        self.refresh_requested = True
        now = _ticks_ms()
        self.last_attempt_ms = now - self.config["refresh_seconds"] * 1000
        self.last_success_ms = None

    async def run(self, render_update):
        last_time = _ticks_ms()
        while True:
            if self.refresh_requested:
                self.refresh_requested = False
                await self._refresh(render_update)
                await render_update()
                last_time = _ticks_ms()

            now = _ticks_ms()
            delta = _ticks_diff(now, last_time)
            if self.update(delta) is not False:
                await render_update()
            else:
                await asyncio.sleep(0.05)
            last_time = now

    def update(self, delta):
        del delta
        if self.button_states.pressed(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self.minimise()
            return False

        if self.button_states.pressed(BUTTON_TYPES["CONFIRM"]):
            self.refresh_requested = True
            return True

        if (
            not self.loading
            and _ticks_diff(_ticks_ms(), self.last_attempt_ms)
            >= self.config["refresh_seconds"] * 1000
        ):
            self.refresh_requested = True
            return True
        return False

    async def _refresh(self, render_update):
        self.last_attempt_ms = _ticks_ms()
        token = self.config["bridge_token"]
        if not token:
            self.error = "SET BRIDGE TOKEN"
            self.stale = bool(self.windows)
            return

        self.loading = True
        self.error = None
        await render_update()
        try:
            if not wifi.status():
                wifi.connect()
                if not await wifi.async_wait(12):
                    raise OSError("wifi unavailable")

            discovery_port = self.config["discovery_port"]
            if discovery_port:
                url = await async_helpers.unblock(
                    _discover_bridge, render_update, token, discovery_port
                )
            else:
                url = self.config["bridge_url"]
                if not url:
                    raise ValueError("set bridge config")

            payload = await async_helpers.unblock(
                _fetch_usage_json,
                render_update,
                url,
                token,
                self.config["http_timeout_seconds"],
            )
            self._accept_payload(payload)
            self.last_success_ms = _ticks_ms()
            self.stale = False
        except Exception as exc:
            self.error = self._short_error(exc)
            self.stale = bool(self.windows)
        finally:
            self.loading = False

    @staticmethod
    def _short_error(exc):
        text = str(exc).upper().strip()
        if not text:
            return "REFRESH FAILED"
        if len(text) > 26:
            text = text[:26]
        return text

    def _accept_payload(self, payload):
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("bad bridge payload")
        rows = payload.get("windows")
        if not isinstance(rows, list):
            raise ValueError("missing windows")

        parsed = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = row.get("kind")
            if kind not in ("weekly", "five_hour"):
                continue
            try:
                remaining = _clamp(int(row["remaining_percent"]), 0, 100)
            except (KeyError, TypeError, ValueError):
                continue
            reset_label = row.get("reset_label") or "RESET UNKNOWN"
            parsed[kind] = {
                "remaining_percent": remaining,
                "reset_label": str(reset_label).upper(),
            }
        if not parsed:
            raise ValueError("no usage windows")
        self.windows = parsed

    @staticmethod
    def _colour_for(ctx, remaining):
        if remaining <= 10:
            return ctx.rgb(1.0, 0.22, 0.18)
        if remaining <= 30:
            return ctx.rgb(1.0, 0.68, 0.10)
        return ctx.rgb(0.08, 0.92, 0.82)

    @staticmethod
    def _one_line_reset(label):
        return label.replace(" · ", "  ")

    def _draw_gauge(self, ctx, x, y, radius, title, window):
        remaining = window["remaining_percent"]
        start = -3.141592653589793 / 2
        ctx.line_width = 9 if radius >= 50 else 8
        ctx.rgb(0.07, 0.16, 0.19).arc(x, y, radius, 0, TAU, False).stroke()
        if remaining:
            self._colour_for(ctx, remaining).arc(
                x,
                y,
                radius,
                start,
                start + TAU * remaining / 100,
                False,
            ).stroke()

        ctx.font_size = 32 if radius >= 50 else 22
        ctx.rgb(0.90, 1.0, 0.98).move_to(x, y - 6).text("%d%%" % remaining)
        ctx.font_size = 13 if radius >= 50 else 11
        ctx.rgb(0.43, 0.72, 0.70).move_to(x, y + 17).text(title)

    def draw(self, ctx):
        ctx.save()
        ctx.rgb(0.006, 0.025, 0.030).rectangle(-120, -120, 240, 240).fill()
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        try:
            ctx.font = ctx.get_font_name(0)
        except Exception:
            pass

        ctx.font_size = 18
        ctx.rgb(0.72, 1.0, 0.96).move_to(0, -94).text("CODEX USAGE")
        ctx.line_width = 2
        ctx.rgb(0.08, 0.42, 0.40).move_to(-80, -75).line_to(80, -75).stroke()
        ctx.translate(0, 20)

        weekly = self.windows.get("weekly")
        five_hour = self.windows.get("five_hour")
        if weekly and five_hour:
            self._draw_gauge(ctx, -56, -23, 39, "WEEKLY LEFT", weekly)
            self._draw_gauge(ctx, 56, -23, 39, "5 HOUR LEFT", five_hour)
            ctx.font_size = 10
            ctx.rgb(0.68, 0.82, 0.81).move_to(0, 39).text(
                "WEEKLY  " + self._one_line_reset(weekly["reset_label"])
            )
            ctx.move_to(0, 57).text(
                "5 HOUR  " + self._one_line_reset(five_hour["reset_label"])
            )
        elif weekly:
            self._draw_gauge(ctx, 0, -17, 58, "WEEKLY LEFT", weekly)
            ctx.font_size = 12
            ctx.rgb(0.68, 0.82, 0.81).move_to(0, 56).text(
                self._one_line_reset(weekly["reset_label"])
            )
        elif five_hour:
            self._draw_gauge(ctx, 0, -17, 58, "5 HOUR LEFT", five_hour)
            ctx.font_size = 12
            ctx.rgb(0.68, 0.82, 0.81).move_to(0, 56).text(
                self._one_line_reset(five_hour["reset_label"])
            )
        elif self.loading:
            ctx.font_size = 19
            ctx.rgb(0.10, 0.88, 0.80).move_to(0, -10).text("REFRESHING...")
        else:
            ctx.font_size = 15
            ctx.rgb(0.68, 0.82, 0.81).move_to(0, -10).text("NO USAGE DATA")
            if self.error:
                ctx.font_size = 11
                ctx.rgb(1.0, 0.68, 0.10).move_to(0, 15).text(self.error)
        ctx.restore()


__app_export__ = CodexUsageApp
