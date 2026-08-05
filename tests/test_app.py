import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


APP_PATH = Path(__file__).parents[1] / "app.py"


class FakeClock:
    now = 100_000

    @classmethod
    def ticks_ms(cls):
        return cls.now

    @staticmethod
    def ticks_diff(new, old):
        return new - old


class FakeButton:
    def __init__(self, name):
        self.name = name


class FakeButtons:
    def __init__(self, owner):
        self.owner = owner
        self.down = set()
        self.latched = set()

    def pressed(self, button):
        if button.name in self.down and button.name not in self.latched:
            self.latched.add(button.name)
            return True
        if button.name not in self.down:
            self.latched.discard(button.name)
        return False

    def clear(self):
        self.down.clear()
        self.latched.clear()


class FakeBaseApp:
    def __init__(self):
        self.minimised = False

    def minimise(self):
        self.minimised = True


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class FakeCtx:
    CENTER = "center"
    MIDDLE = "middle"

    def __init__(self):
        self.operations = []
        self.depth = 0
        self.line_width = 1
        self.font_size = 0
        self.font = None
        self.text_align = None
        self.text_baseline = None

    def _op(self, name, *args):
        self.operations.append((name, args))
        return self

    def save(self):
        self.depth += 1
        return self._op("save")

    def restore(self):
        self.depth -= 1
        return self._op("restore")

    def get_font_name(self, index):
        return "font-%d" % index

    def rgb(self, *args):
        return self._op("rgb", *args)

    def rectangle(self, *args):
        return self._op("rectangle", *args)

    def fill(self):
        return self._op("fill")

    def stroke(self):
        return self._op("stroke")

    def arc(self, *args):
        if len(args) != 6:
            raise TypeError("arc requires x, y, radius, start, end, anticlockwise")
        return self._op("arc", *args)

    def move_to(self, *args):
        return self._op("move_to", *args)

    def line_to(self, *args):
        return self._op("line_to", *args)

    def translate(self, *args):
        return self._op("translate", *args)

    def text(self, *args):
        return self._op("text", *args)


settings_state = {
    "codex_usage": {
        "bridge_url": "http://192.0.2.1:8765/",
        "bridge_token": "secret",
        "refresh_seconds": 60,
    }
}
request_state = {"response": None, "exception": None, "calls": []}


def sample_payload(two=True):
    windows = [
        {
            "kind": "weekly",
            "remaining_percent": 64,
            "reset_label": "WED 12 AUG · 13:30 GMT",
        }
    ]
    if two:
        windows.append(
            {
                "kind": "five_hour",
                "remaining_percent": 28,
                "reset_label": "WED 05 AUG · 17:00 GMT",
            }
        )
    return {"schema_version": 1, "timezone": "GMT", "windows": windows}


def install_stubs():
    app_module = types.ModuleType("app")
    app_module.App = FakeBaseApp
    sys.modules["app"] = app_module

    settings_module = types.ModuleType("settings")
    settings_module.get = lambda key, default=None: settings_state.get(key, default)
    sys.modules["settings"] = settings_module

    wifi_module = types.ModuleType("wifi")
    wifi_module.status = lambda: True
    wifi_module.connect = lambda: None

    async def async_wait(timeout):
        return True

    wifi_module.async_wait = async_wait
    sys.modules["wifi"] = wifi_module

    requests_module = types.ModuleType("requests")

    def get(url, headers=None):
        request_state["calls"].append((url, headers))
        if request_state["exception"]:
            raise request_state["exception"]
        return request_state["response"]

    requests_module.get = get
    sys.modules["requests"] = requests_module

    helpers = types.ModuleType("async_helpers")

    async def unblock(function, periodic, *args, **kwargs):
        await periodic()
        return function(*args, **kwargs)

    helpers.unblock = unblock
    sys.modules["async_helpers"] = helpers

    events_package = types.ModuleType("events")
    sys.modules["events"] = events_package
    input_module = types.ModuleType("events.input")
    input_module.Buttons = FakeButtons
    input_module.BUTTON_TYPES = {
        "CONFIRM": FakeButton("CONFIRM"),
        "CANCEL": FakeButton("CANCEL"),
    }
    sys.modules["events.input"] = input_module


install_stubs()
spec = importlib.util.spec_from_file_location("codex_usage_app_under_test", APP_PATH)
usage_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage_app)


async def render_update():
    return None


class AppTests(unittest.TestCase):
    def setUp(self):
        FakeClock.now = 100_000
        usage_app.time.ticks_ms = FakeClock.ticks_ms
        usage_app.time.ticks_diff = FakeClock.ticks_diff
        settings_state["codex_usage"] = {
            "bridge_url": "http://192.0.2.1:8765/",
            "bridge_token": "secret",
            "refresh_seconds": 60,
        }
        request_state["response"] = None
        request_state["exception"] = None
        request_state["calls"] = []

    def test_accepts_weekly_and_optional_five_hour(self):
        application = usage_app.CodexUsageApp()
        application._accept_payload(sample_payload())
        self.assertEqual(application.windows["weekly"]["remaining_percent"], 64)
        self.assertEqual(application.windows["five_hour"]["remaining_percent"], 28)

        application._accept_payload(sample_payload(two=False))
        self.assertEqual(set(application.windows), {"weekly"})

    def test_rejects_malformed_payload_without_replacing_data(self):
        application = usage_app.CodexUsageApp()
        application._accept_payload(sample_payload())
        with self.assertRaises(ValueError):
            application._accept_payload({"schema_version": 9, "windows": []})
        self.assertIn("weekly", application.windows)

    def test_refresh_closes_response_and_uses_bearer(self):
        response = FakeResponse(sample_payload())
        request_state["response"] = response
        application = usage_app.CodexUsageApp()
        asyncio.run(application._refresh(render_update))
        self.assertTrue(response.closed)
        self.assertFalse(application.stale)
        self.assertEqual(request_state["calls"][0][0], "http://192.0.2.1:8765/v1/usage")
        self.assertEqual(request_state["calls"][0][1]["Authorization"], "Bearer secret")

    def test_failure_keeps_data_and_marks_it_stale(self):
        application = usage_app.CodexUsageApp()
        application._accept_payload(sample_payload())
        request_state["exception"] = OSError("offline")
        asyncio.run(application._refresh(render_update))
        self.assertTrue(application.stale)
        self.assertEqual(application.error, "OFFLINE")
        self.assertIn("weekly", application.windows)

    def test_missing_config_is_reported_without_network(self):
        settings_state["codex_usage"] = {}
        application = usage_app.CodexUsageApp()
        asyncio.run(application._refresh(render_update))
        self.assertEqual(application.error, "SET BRIDGE CONFIG")
        self.assertEqual(request_state["calls"], [])

    def test_manual_and_timed_refresh_and_cancel(self):
        application = usage_app.CodexUsageApp()
        application.refresh_requested = False
        application.last_attempt_ms = FakeClock.now
        self.assertFalse(application.update(10))

        application.button_states.down.add("CONFIRM")
        self.assertTrue(application.update(10))
        self.assertTrue(application.refresh_requested)

        application.button_states.down.clear()
        application.update(10)
        FakeClock.now += 60_000
        application.refresh_requested = False
        self.assertTrue(application.update(10))

        application.button_states.down.add("CANCEL")
        self.assertFalse(application.update(10))
        self.assertTrue(application.minimised)
        self.assertEqual(application.button_states.down, set())

    def test_draw_is_balanced_and_within_operation_budget(self):
        application = usage_app.CodexUsageApp()
        application._accept_payload(sample_payload())
        ctx = FakeCtx()
        application.draw(ctx)
        self.assertEqual(ctx.depth, 0)
        self.assertLessEqual(len(ctx.operations), 100)
        self.assertIn(("translate", (0, 20)), ctx.operations)
        header_index = ctx.operations.index(("text", ("CODEX USAGE",)))
        translate_index = ctx.operations.index(("translate", (0, 20)))
        self.assertLess(header_index, translate_index)
        text = [args[0] for name, args in ctx.operations if name == "text"]
        self.assertIn("64%", text)
        self.assertIn("WEEKLY  WED 12 AUG  13:30 GMT", text)
        self.assertIn("5 HOUR  WED 05 AUG  17:00 GMT", text)
        self.assertNotIn("LIVE · GMT", text)
        self.assertNotIn("C REFRESH   F BACK", text)

    def test_single_window_reset_stays_on_one_line(self):
        application = usage_app.CodexUsageApp()
        application._accept_payload(sample_payload(two=False))
        ctx = FakeCtx()
        application.draw(ctx)
        text = [args[0] for name, args in ctx.operations if name == "text"]
        self.assertIn("WED 12 AUG  13:30 GMT", text)
        self.assertNotIn("WED 12 AUG", text)
        self.assertNotIn("13:30 GMT", text)


if __name__ == "__main__":
    unittest.main()
