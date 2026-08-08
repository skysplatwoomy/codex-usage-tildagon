import json
from datetime import datetime
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


sys.path.insert(0, str(Path(__file__).parents[1]))
import bridge


class FakeAppServer:
    def __init__(self, results):
        self.results = list(results)
        self.closed = 0

    def read_rate_limits(self):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed += 1


def sample_response():
    return {
        "rateLimits": {"primary": None, "secondary": None},
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "usedPercent": 31,
                    "windowDurationMins": 300,
                    "resetsAt": 1785925800,
                },
                "secondary": {
                    "usedPercent": 82,
                    "windowDurationMins": 10080,
                    "resetsAt": 1786357800,
                },
            }
        },
    }


class NormalizeTests(unittest.TestCase):
    def test_normalizes_and_orders_weekly_first_in_host_timezone(self):
        payload = bridge.normalize_rate_limits(sample_response(), fetched_at=123)
        self.assertEqual(payload["timezone"], "host-local")
        self.assertEqual(payload["fetched_at"], 123)
        self.assertEqual([row["kind"] for row in payload["windows"]], ["weekly", "five_hour"])
        self.assertEqual(payload["windows"][0]["remaining_percent"], 18)
        local = datetime.fromtimestamp(1786357800).astimezone()
        expected = "%s %02d %s · %02d:%02d" % (
            bridge.WEEKDAYS[local.weekday()],
            local.day,
            bridge.MONTHS[local.month - 1],
            local.hour,
            local.minute,
        )
        self.assertEqual(payload["windows"][0]["reset_label"], expected)
        self.assertNotIn("GMT", payload["windows"][0]["reset_label"])

    def test_falls_back_to_compatibility_bucket_and_order(self):
        payload = bridge.normalize_rate_limits(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 110, "resetsAt": None},
                    "secondary": {"usedPercent": -4, "resetsAt": None},
                }
            },
            fetched_at=1,
        )
        rows = {row["kind"]: row for row in payload["windows"]}
        self.assertEqual(rows["five_hour"]["remaining_percent"], 0)
        self.assertEqual(rows["weekly"]["remaining_percent"], 100)
        self.assertEqual(rows["weekly"]["reset_label"], "RESET UNKNOWN")

    def test_allows_optional_five_hour_window_to_be_absent(self):
        payload = bridge.normalize_rate_limits(
            {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 12,
                        "windowDurationMins": 10080,
                        "resetsAt": 1786357800,
                    }
                }
            }
        )
        self.assertEqual(len(payload["windows"]), 1)
        self.assertEqual(payload["windows"][0]["kind"], "weekly")

    def test_rejects_missing_windows(self):
        with self.assertRaises(bridge.AppServerError):
            bridge.normalize_rate_limits({"rateLimits": {}})

    def test_provider_restarts_once(self):
        server = FakeAppServer([RuntimeError("first"), sample_response()])
        payload = bridge.UsageProvider(server).fetch()
        self.assertEqual(server.closed, 1)
        self.assertEqual(payload["schema_version"], 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        class Provider:
            def fetch(self):
                return {"schema_version": 1, "timezone": "host-local", "windows": []}

        self.server = bridge.ThreadingHTTPServer(
            ("127.0.0.1", 0), bridge.make_handler(Provider(), "test-token")
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/v1/usage" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_requires_bearer_token(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.url, timeout=2)
        self.assertEqual(raised.exception.code, 401)

    def test_serves_sanitized_json(self):
        request = Request(self.url, headers={"Authorization": "Bearer test-token"})
        with urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
