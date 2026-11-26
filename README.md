# BBQ — CyberQ dashboard

Private-network web GUI, API and temperature history for the BBQ Guru
CyberQ WiFi (FW 3.1). One Python 3.12 container:
FastAPI + uvicorn + httpx, SQLite from the stdlib, vanilla-JS UI with
[vendored uPlot](static/uPlot.LICENSE) graphs.

**Safety model (see [DESIGN.md](DESIGN.md))** — there is no raw proxy:

* the only device requests this code can make are GETs to `/all.xml`,
  `/status.xml`, `/config.xml` and the setup pages, plus mainForm POSTs to
  `/`, `control.htm`, `system.htm`, `email.htm`, `wifi.htm` and `scan.htm`
  (a WiFi scan), all built from fixed per-page field allowlists with
  validated and clamped values;
* `booting.htm` and `reboot.htm` are in neither allowlist (test-asserted).
  A plain reboot exists only behind `ALLOW_REBOOT=1` + `{"confirm": true}`
  (`POST /api/reboot`); **factory/firmware reset has no code path at
  all**, and reset-like field names (`FACTORY|DEFAULT|FIRMWARE|UPGRADE|
  FLASH|RESET|REBOOT|BOOTING`) are denied on every write;
* credential fields (`SMTP_PWD`, `WIFI_KEY*`, `HTTP_PASS`) are write-only:
  dropped by the config parser, never extracted from the wifi page, never
  logged (test-asserted with a DEBUG log capture), never returned by the
  API, and audited with NULL values. The one exception is the stock-parity
  write echo: `write_email` re-sends the device-served password in its
  POST body, transiently, exactly like the stock browser form.

## Development

```sh
uv venv --python 3.12 .venv
uv pip install -p .venv -e '.[dev]'
.venv/bin/python -m pytest -q          # 72 tests, all against the fake device
uvicorn dev.fake_cyberq:app --port 8099              # fake CyberQ (optional)
CYBERQ_URL=http://127.0.0.1:8099 DB_PATH=bbq-dev.db \
  .venv/bin/uvicorn app.main:app --port 8080          # app against the fake
```

No test ever touches the real device: everything runs against `dev/fake_cyberq.py`.

## Configuration

| Env             | Default                 | Meaning                                   |
|-----------------|-------------------------|-------------------------------------------|
| `CYBERQ_URL`    | `http://192.168.17.6` (set yours) | device base URL                           |
| `DB_PATH`       | `bbq.db` (`/data/bbq.db` in the container) | SQLite file            |
| `POLL_INTERVAL` | `5`                     | seconds between polls; `0` disables the loop |
| `CONFIG_TTL`    | `30`                    | seconds to cache `/api/config`            |
| `ALLOW_REBOOT`  | `0`                     | `1` enables the confirm-gated reboot (API and UI button) |

## API

- `GET /healthz`
- `GET /api/status` — latest sample + device reachable/stale flags
- `GET /api/config` — system/control/email/wifi sections (home+away profiles,
  HTTP settings), credentials dropped at parse time
- `GET /api/history?from=&to=&step=` — bucketed averages (default 24 h @ 300 s)
- `PATCH /api/probes/{cook|food1|food2|food3}` — `{"name": …, "set_f": …}`
- `PUT /api/timer` — `{"hhmmss": "HH:MM:SS"}`
- `PATCH /api/config/{control|system|email}` — per-section settings
- `PATCH /api/config/wifi` — network settings; **requires
  `{"confirm": true}`** (a bad write can strand the device off the network)
- `POST /api/email/test` — send a test e-mail (SMTP_NOW)
- `POST /api/wifi/scan` — trigger a WiFi scan (WIFI_SCAN_START)
- `POST /api/reboot` — `{"confirm": true}`; **403 unless ALLOW_REBOOT=1**.
  A factory/firmware reset does not exist in this codebase.
- `POST /api/cooks` / `GET /api/cooks` / `PATCH /api/cooks/{id}` — named cook sessions

Every write is validated and clamped (cook 32–475 °F, food 32–300 °F,
control values per the module constants), mirrors the stock forms (same
fields, same wire format), and is recorded in the `audit` table — with
NULL old/new values for credential fields.

## Deploy

Run it on a host on the same LAN as the controller. Create a `.env` next to
`docker-compose.yml`:

```sh
BIND_IP=100.x.y.z              # e.g. the host's Tailscale IP; never 0.0.0.0
HOST_PORT=8080
DATA_DIR=/srv/bbq              # SQLite history (WAL)
CYBERQ_URL=http://192.168.1.50 # your CyberQ
ALLOW_REBOOT=0                 # 1 shows the confirm-gated reboot
```

```sh
docker compose config          # sanity check: resolves env, prints the final config
docker compose up -d --build
docker compose ps; docker compose logs -f bbq
```

There is no login: restrict access at the network layer (Tailscale, VPN, or
a firewalled interface). The CyberQ itself has no auth worth relying on.

## Repo layout

```
app/        main.py, device.py, poller.py, db.py, models.py
static/     index.html, app.js, app.css, vendored uPlot
tests/      test_device_allowlist.py, test_parse.py, test_api.py, test_fake_cyberq.py
dev/        fake_cyberq.py (test double), make_fixtures.py, fixtures/ (sanitized pages)
recon/      raw device captures (config.xml, wifi.htm, email.htm are gitignored: credentials)
```
