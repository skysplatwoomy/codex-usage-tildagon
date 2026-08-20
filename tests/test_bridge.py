import json
from datetime import datetime
import hashlib
import hmac
from pathlib import Path
import socket
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


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.server = bridge.DiscoveryServer(
            "test-token", 8765, 0, bind="127.0.0.1"
        )
        self.server.start()

    def tearDown(self):
        self.server.close()

    def test_replies_with_source_bound_authenticated_address(self):
        nonce = "0011223344556677"
        request = json.dumps(
            {
                "type": bridge.DISCOVERY_REQUEST_TYPE,
                "version": bridge.DISCOVERY_VERSION,
                "nonce": nonce,
            }
        ).encode()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(2)
        try:
            client.sendto(request, ("127.0.0.1", self.server.port))
            raw, source = client.recvfrom(1024)
        finally:
            client.close()

        payload = json.loads(raw)
        self.assertEqual(source[0], "127.0.0.1")
        self.assertEqual(payload["host"], source[0])
        self.assertEqual(payload["port"], 8765)
        self.assertEqual(payload["nonce"], nonce)
        message = bridge.discovery_proof_message(nonce, source[0], 8765)
        expected = hmac.new(
            b"test-token", message.encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(payload["proof"], expected)

    def test_ignores_malformed_requests(self):
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.2)
        try:
            client.sendto(b"not-json", ("127.0.0.1", self.server.port))
            with self.assertRaises(socket.timeout):
                client.recvfrom(1024)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
