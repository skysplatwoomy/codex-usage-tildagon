import asyncio
import hashlib
import hmac
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
        "discovery_port": 8766,
        "http_timeout_seconds": 8,
        "refresh_seconds": 60,
    }
}
network_state = {
    "url": "http://192.0.2.55:8765",
    "payload": None,
    "discovery_exception": None,
    "fetch_exception": None,
    "discovery_calls": [],
    "fetch_calls": [],
}


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


def fake_discover(token, discovery_port):
    network_state["discovery_calls"].append((token, discovery_port))
    if network_state["discovery_exception"]:
        raise network_state["discovery_exception"]
    return network_state["url"]


def fake_fetch(url, token, timeout_seconds):
    network_state["fetch_calls"].append((url, token, timeout_seconds))
    if network_state["fetch_exception"]:
        raise network_state["fetch_exception"]
    return network_state["payload"]


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
            "discovery_port": 8766,
            "http_timeout_seconds": 8,
            "refresh_seconds": 60,
        }
        network_state["url"] = "http://192.0.2.55:8765"
        network_state["payload"] = sample_payload()
        network_state["discovery_exception"] = None
        network_state["fetch_exception"] = None
        network_state["discovery_calls"] = []
        network_state["fetch_calls"] = []
        usage_app._discover_bridge = fake_discover
        usage_app._fetch_usage_json = fake_fetch

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

    def test_refresh_discovers_bridge_and_uses_shared_token(self):
        application = usage_app.CodexUsageApp()
        asyncio.run(application._refresh(render_update))
        self.assertFalse(application.stale)
        self.assertEqual(network_state["discovery_calls"], [("secret", 8766)])
        self.assertEqual(
            network_state["fetch_calls"],
            [("http://192.0.2.55:8765", "secret", 8)],
        )

    def test_failure_keeps_data_and_marks_it_stale(self):
        application = usage_app.CodexUsageApp()
        application._accept_payload(sample_payload())
        network_state["fetch_exception"] = OSError("offline")
        asyncio.run(application._refresh(render_update))
        self.assertTrue(application.stale)
        self.assertEqual(application.error, "OFFLINE")
        self.assertIn("weekly", application.windows)

    def test_missing_config_is_reported_without_network(self):
        settings_state["codex_usage"] = {}
        application = usage_app.CodexUsageApp()
        asyncio.run(application._refresh(render_update))
        self.assertEqual(application.error, "SET BRIDGE TOKEN")
        self.assertEqual(network_state["discovery_calls"], [])
        self.assertEqual(network_state["fetch_calls"], [])

    def test_discovery_proof_matches_standard_hmac_and_binds_source_ip(self):
        nonce = "0011223344556677"
        host = "192.0.2.55"
        port = 8765
        message = usage_app._discovery_proof_message(nonce, host, port)
        proof = hmac.new(b"secret", message.encode(), hashlib.sha256).hexdigest()
        payload = {
            "type": usage_app.DISCOVERY_RESPONSE_TYPE,
            "version": usage_app.DISCOVERY_VERSION,
            "nonce": nonce,
            "host": host,
            "port": port,
            "proof": proof,
        }
        self.assertEqual(
            usage_app._validated_discovery_url(
                payload, (host, 8766), "secret", nonce
            ),
            "http://192.0.2.55:8765",
        )
        self.assertIsNone(
            usage_app._validated_discovery_url(
                payload, ("192.0.2.99", 8766), "secret", nonce
            )
        )
        payload["proof"] = "0" * 64
        self.assertIsNone(
            usage_app._validated_discovery_url(
                payload, (host, 8766), "secret", nonce
            )
        )

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
