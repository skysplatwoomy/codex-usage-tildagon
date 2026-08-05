# Codex Usage

`Codex Usage` is a Tildagon 2026 dashboard for the weekly and optional
five-hour Codex usage windows. Percentages show allowance remaining and reset
times are always rendered in fixed GMT (UTC+00:00).

The badge never receives OpenAI credentials. `bridge.py` uses the locally
authenticated Codex CLI and exposes only the normalized usage snapshot through
a bearer-protected, read-only HTTP endpoint.

## Run the bridge

Choose the laptop address reachable from the badge and a strong persistent
token. Bind to that one LAN address rather than every network interface. Keep
the values in the ignored, host-only `bridge.local.json` file:

```json
{
  "bind": "192.168.1.23",
  "port": 8765,
  "token": "replace-with-a-long-random-token"
}
```

Then start the bridge with:

```sh
python3 codex_usage/bridge.py
```

Command-line options and `CODEX_USAGE_TOKEN` override the local file.

The bridge and badge must share a reachable, trusted LAN. The bearer token is
not encrypted over HTTP, but it grants access only to the sanitized read-only
snapshot. Client isolation on public Wi-Fi may prevent this design from
working.

## Badge configuration

Store the following object under the `codex_usage` key in `/settings.json`:

```json
{
  "bridge_url": "http://192.168.1.23:8765",
  "bridge_token": "replace-with-a-long-random-token",
  "refresh_seconds": 60
}
```

The deployment workflow writes this configuration with `settings.set()` rather
than checking a token into the app source. Press C to refresh and F to return to
the launcher.

## Host tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s codex_usage/tests -p 'test_*.py'
```

Only `__init__.py`, `app.py`, `main.py`, and `metadata.json` belong on the
badge. The bridge, tests, and this README are host-only.
