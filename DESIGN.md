# BBQ — CyberQ WiFi dashboard

## Goal
Private-network web GUI and API for the BBQ Guru CyberQ WiFi (FW 3.1). It shows live status, exposes every setting the device's own web UI offers, and keeps a history with graphs. There is no raw proxy, and no firmware or factory reset path; a plain reboot is opt-in.

## Architecture
```
browser (private network) ──> bbq container :8080 (bound to BIND_IP only)
                        ├─ FastAPI: /api/*, static UI
                        ├─ poller task: GET /all.xml every 5 s ──> SQLite (WAL)
                        └─ device client: ALLOWLISTED form POSTs ──> CyberQ :80 (CYBERQ_URL)
```
- **One container**: Python 3.12, FastAPI + uvicorn, httpx, sqlite3 from the stdlib. No ORM.
- **Host**: any Docker host on the same LAN as the controller. History lives in `$DATA_DIR/bbq.db` via a volume.
- **Private only**: publish the port on a private interface (`BIND_IP`, e.g. a Tailscale IP), never 0.0.0.0. No auth beyond network membership.

## Safety: no raw proxy
We do **not** pass arbitrary requests through to the device. The device client exposes only the functions below. Each one builds a POST from an **allowlist** of field names, and values are validated and clamped. Unit tests assert that no request ever goes to a URL outside the allowlist, and that `booting.htm`/`reboot.htm` are only reachable through the flag-gated reboot.

Read endpoints: `GET /all.xml` (polled), and `GET /config.xml` plus the page HTML for current form values, both parsed server-side. Secret fields are redacted at parse time and never stored or returned.

Writable fields in v1 (the form POST to `/`, as the stock mainForm does it):
- `COOK_NAME`, `FOOD1..3_NAME`: string, ≤ 16 chars, printable ASCII
- `COOK_SET`: °F, 32–475. `FOOD1..3_SET`: °F, 32–300 (in whole or tenth °F, as the device accepts)
- `COOK_TIMER`: HH:MM:SS (confirm the field name from index.html: `_COOK_TIMER` is the display copy)

### Device function inventory (CyberQ WiFi FW 3.1)
Scope: everything except firmware reset. Every form POSTs back to its own page (`mainForm` has no action), and each one includes `EEAUTOFLUSH`.

| Page | Fields | Plan |
|---|---|---|
| `/` (index) | COOK/FOOD1-3 `_NAME`, `_SET`, `COOK_TIMER` | v1 |
| `control.htm` | CYCTIME, PROPBAND, COOKHOLD, ALARMDEV (hidden real plus `_` display copies), COOK_RAMP, OPENDETECT, TIMEOUT_ACTION | v1 |
| `system.htm` | LCD_BACKLIGHT, LCD_CONTRAST, DEG_UNITS, ALARM_BEEPS, KEY_BEEPS, MENU_SCROLLING | v1 |
| `email.htm` | SMTP_HOST, SMTP_PORT, SMTP_USER, **SMTP_PWD**, SMTP_FROM, SMTP_TO, SMTP_SUBJ, SMTP_ALERT, SMTP_NOW (send test) | v1. The password is write-only and redacted on read. |
| `wifi.htm` | profile 1 and 2: SSID, **WIFI_KEY**, WIFIMODE, WIFI_ENC, DHCP, IP, NM, GW, DNS; MULTI_WIFI_CHOICE, HTTP_PORT, HTTP_USER, **HTTP_PASS**; `scan.htm` (WIFI_SCAN_START) | v1 behind a "dangerous" confirm. Keys and passwords are write-only. A bad write here can strand the device off the network. |
| `reboot.htm` → `booting.htm` | `RESET_CPU=1` (reboot) | opt-in via `ALLOW_REBOOT=1`, confirm-gated; a plain reboot, not a factory reset |

No factory-reset or firmware-update endpoint appears in the web UI; those seem to be keypad or service-tool only. The server-side denylist still blocks `booting.htm`/`RESET_CPU`, any field matching `/FACTORY|DEFAULT|FIRMWARE|UPGRADE|FLASH/i`, and any path outside the allowlist, until Mick decides.

Credentials (SMTP_PWD, WIFI_KEY*, HTTP_PASS) are never logged, stored or returned. The API reports only `{"set": true|false}`.
Note: if HTTP_USER/HTTP_PASS gets set, the device client will need basic auth.

## Data model
`samples(ts INTEGER, cook REAL, food1 REAL, food2 REAL, food3 REAL, cook_set REAL, food1_set..food3_set REAL, output_pct INTEGER, cook_status, food1_status..food3_status INTEGER, timer TEXT, timer_status INTEGER)`
- Temperatures are stored in °F as floats: the device's tenths divided by 10. `OPEN` is stored as NULL.
- `cooks(id, name, started_ts, ended_ts, notes)`: optional named sessions, so graphs can scope to one cook.
- Writes are logged in `audit(ts, field, old, new, client_ip)`.

## API
- `GET /api/status`: the latest parsed sample plus the device's reachable/stale flag
- `GET /api/history?from=&to=&step=`: downsampled series (bucketed avg)
- `GET /api/config`: control, system, email and wifi sections, with secrets redacted
- `PATCH /api/config/{control|system|email|wifi}` (wifi needs `"confirm": true`), `POST /api/email/test`, `POST /api/wifi/scan`
- `PATCH /api/probes/{cook|food1|food2|food3}` with `{name?, set_f?}`
- `PUT /api/timer` with `{hhmmss}`
- `POST /api/cooks`, `PATCH /api/cooks/{id}` (start or end a session)
- `GET /healthz`

## UI
A single static page: vanilla JS plus uPlot, vendored. Four probe cards with temp, setpoint, status and name, a fan % gauge, the timer, edit dialogs, and a history chart with 1h/6h/24h/cook ranges. Status codes 0–7 mean OK/HIGH/LOW/DONE/ERROR/HOLD/ALARM/SHUTDOWN.

## Testing
- `fake_cyberq.py`: a tiny FastAPI app that serves the recon XML and accepts the allowlisted POSTs. All dev work and tests run against it.
- Tests never touch a real device.

## Repo layout
```
app/        main.py, device.py, poller.py, db.py, models.py
static/     index.html, app.js, uplot.*
tests/      test_device_allowlist.py, test_parse.py, test_api.py
dev/        fake_cyberq.py
recon/      raw captures (config.xml is gitignored: it holds secrets)
Dockerfile, docker-compose.yml, pyproject.toml
```
