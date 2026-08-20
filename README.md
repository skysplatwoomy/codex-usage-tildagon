# Codex Usage

`Codex Usage` is a Tildagon 2026 dashboard for the weekly and optional
five-hour Codex usage windows. Percentages show allowance remaining and reset
times follow the host computer's local timezone; the timezone suffix is hidden
on the badge.

The badge never receives OpenAI credentials. `bridge.py` uses the locally
authenticated Codex CLI and exposes only the normalized usage snapshot through
a bearer-protected, read-only HTTP endpoint.

## Run the bridge

Choose a strong persistent token. The bridge binds to all current and future
interfaces so DHCP address changes do not break startup, while an authenticated
UDP discovery reply tells the badge which LAN address received its request.
Keep the values in the ignored, host-only `bridge.local.json` file:

```json
{
  "bind": "0.0.0.0",
  "port": 8765,
  "discovery_port": 8766,
  "token": "replace-with-a-long-random-token"
}
```

Then start the bridge with:

```sh
python3 codex_usage/bridge.py
```

Command-line options and `CODEX_USAGE_TOKEN` override the local file.

The badge broadcasts a nonce on UDP port `8766`. The bridge replies with its
route-selected address, HTTP port, and an HMAC-SHA256 proof over those values.
The badge verifies the proof and packet source before sending its bearer token,
then rediscovers on every refresh. This avoids fixed IP addresses without
leaking the token to an unauthenticated discovery responder.

The bridge and badge must share a reachable, trusted LAN. Permit inbound TCP
`8765` and UDP `8766` on that LAN if a host firewall is enabled. The bearer
token is not encrypted over HTTP, but it grants access only to the sanitized
read-only snapshot. Client isolation or blocked broadcasts on public Wi-Fi may
prevent discovery.

## Badge configuration

Store the following object under the `codex_usage` key in `/settings.json`:

```json
{
  "bridge_token": "replace-with-a-long-random-token",
  "discovery_port": 8766,
  "http_timeout_seconds": 8,
  "refresh_seconds": 60
}
```

The deployment workflow writes this configuration with `settings.set()` rather
than checking a token into the app source. Set `discovery_port` to `0` and add a
`bridge_url` only when a network blocks UDP broadcast and a fixed manual
endpoint is intentionally required. Press C to refresh and F to return to the
launcher.

Discovery attempts are bounded, and the HTTP client uses a socket timeout, so a
missing bridge produces an on-screen error instead of leaving the app on
`REFRESHING...` indefinitely.

## Host tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s codex_usage/tests -p 'test_*.py'
```

Only `__init__.py`, `app.py`, `main.py`, and `metadata.json` belong on the
badge. The bridge, tests, and this README are host-only.
