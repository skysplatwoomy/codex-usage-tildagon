import asyncio
import time

import app
import async_helpers
import requests
import settings
import wifi
from events.input import BUTTON_TYPES, Buttons


CONFIG_KEY = "codex_usage"
DEFAULT_REFRESH_SECONDS = 60
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


def load_config():
    saved = settings.get(CONFIG_KEY, {})
    if not isinstance(saved, dict):
        saved = {}
    try:
        refresh_seconds = int(saved.get("refresh_seconds", DEFAULT_REFRESH_SECONDS))
    except (TypeError, ValueError):
        refresh_seconds = DEFAULT_REFRESH_SECONDS
    return {
        "bridge_url": str(saved.get("bridge_url", "")).rstrip("/"),
        "bridge_token": str(saved.get("bridge_token", "")),
        "refresh_seconds": _clamp(refresh_seconds, 15, 3600),
    }


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
        url = self.config["bridge_url"]
        token = self.config["bridge_token"]
        if not url or not token:
            self.error = "SET BRIDGE CONFIG"
            self.stale = bool(self.windows)
            return

        self.loading = True
        self.error = None
        await render_update()
        response = None
        try:
            if not wifi.status():
                wifi.connect()
                if not await wifi.async_wait(12):
                    raise OSError("wifi unavailable")

            response = await async_helpers.unblock(
                requests.get,
                render_update,
                url + "/v1/usage",
                headers={"Authorization": "Bearer " + token},
            )
            if getattr(response, "status_code", 0) != 200:
                raise OSError("bridge HTTP %s" % getattr(response, "status_code", "?"))
            payload = response.json()
            self._accept_payload(payload)
            self.last_success_ms = _ticks_ms()
            self.stale = False
        except Exception as exc:
            self.error = self._short_error(exc)
            self.stale = bool(self.windows)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
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
