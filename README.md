# BBQ — CyberQ dashboard

Private-network web GUI, API and temperature history for the BBQ Guru
CyberQ WiFi (FW 3.1). One Python 3.12 container:
FastAPI + uvicorn + httpx + PyYAML, SQLite from the stdlib, vanilla-JS UI with
[vendored uPlot](static/uPlot.LICENSE) graphs. The simulator (`sim/`) ships in
the image: its food model, scorecard and control-law fit back the ETA and
the cook report.

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
.venv/bin/python -m pytest -q          # the fast suite; no test touches a real device
.venv/bin/python -m pytest -q -m slow  # the tests that serve the simulator on a real port
uvicorn dev.fake_cyberq:app --port 8099              # fake CyberQ: static fixture values
.venv/bin/python -m sim serve --speed 60 --port 8099 # simulated CyberQ: a live model
CYBERQ_URL=http://127.0.0.1:8099 DB_PATH=bbq-dev.db \
  .venv/bin/uvicorn app.main:app --port 8080          # the app, against either of them
```

No test ever touches the real device: everything runs against `dev/fake_cyberq.py`
or the simulator.

The simulator ([`sim/`](sim/), see [docs/SIMULATOR.md](docs/SIMULATOR.md)) puts a
thermal model of the cooker — and the CyberQ's own control loop — behind the
device's wire format. It serves the same pages, XML and forms as the real box,
byte for byte, so the dashboard can be developed against a cook that heats up,
stalls and recovers instead of static fixtures. `--speed 60` runs an hour of
cooking in a minute.

It also runs headless, against scenarios:

```sh
.venv/bin/python -m sim run                                   # scorecard: every scenario, its own controller
.venv/bin/python -m sim run --controller proportional,bias,pi  # every scenario under each law
.venv/bin/python -m sim run lid-open --set opendetect=false    # one cook, with a setting overridden
.venv/bin/python -m sim run brisket-12h --controller pi --out run.csv --json run.json
.venv/bin/python -m sim compare --db bbq.db --food food1=brisket --skip 4h55m:5h40m
.venv/bin/python -m sim fit --db bbq.db --out cooker.yaml
```

The scenarios are in [`scenarios/`](scenarios/); [`sim/scenario.py`](sim/scenario.py)
says what one can contain. `run` gives each cook a summary — overshoot, where it
settled and for how long, the steady error, the fan, the fuel — and, with more
than one run, a table to compare them by. `compare` replays a recorded cook from
the dashboard's history through the model and scores it against what was
measured; `fit` estimates the cooker's parameters from such a recording, and
says so when the recording has no heat-up to learn from. Both open the database
read-only and leave nothing beside it.

## Configuration

| Env             | Default                 | Meaning                                   |
|-----------------|-------------------------|-------------------------------------------|
| `CYBERQ_URL`    | **required** — e.g. `http://192.0.2.6` | device base URL (no default: the app fails fast when unset) |
| `DB_PATH`       | `bbq.db` (`/data/bbq.db` in the container) | SQLite file            |
| `POLL_INTERVAL` | `5`                     | seconds between polls; `0` disables the loop |
| `CONFIG_TTL`    | `30`                    | seconds to cache `/api/config`            |
| `ETA_TTL`       | `30`                    | seconds to cache `/api/eta`               |
| `REPORT_TTL`    | `60`                    | seconds to cache each `/api/report` query |
| `ALLOW_REBOOT`  | `0`                     | `1` enables the confirm-gated reboot (API and UI button) |

## API

- `GET /healthz`
- `GET /api/status` — latest sample + device reachable/stale flags
- `GET /api/config` — system/control/email/wifi sections (home+away profiles,
  HTTP settings), credentials dropped at parse time
- `GET /api/history?from=&to=&step=` — bucketed averages (default 24 h @ 300 s)
- `GET /api/eta` — for each food probe, when it will reach its setpoint, estimated
  from the stored history with the simulator's food model (`eta`, `stalled` or
  `none`, with a band in minutes); it asks nothing of the device
- `GET /api/report?cook=<id>` or `?hours=<n>` — a cook as it went, from the stored
  history: the simulator's scorecard (reached setpoint, overshoot, steady error,
  fan, lid events, food done times) and the control law the fan output shows, or
  the reason it cannot be read. Shown by `report.html`; read-only
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
CYBERQ_URL=http://192.0.2.6 # your CyberQ (TEST-NET example)
ALLOW_REBOOT=0                 # 1 shows the confirm-gated reboot
POLL_INTERVAL=5                # seconds between device reads (2 is fine for a CyberQ)
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
app/        main.py, device.py, poller.py, db.py, models.py, eta.py, report.py
static/     index.html, app.js, report.html, report.js, common.js, chart.js, app.css, vendored uPlot
tests/      test_device_allowlist.py, test_parse.py, test_api.py, test_fake_cyberq.py, test_eta_api.py,
            test_report_api.py
tests/sim/  the simulator's tests, including the dashboard run against it
dev/        fake_cyberq.py (test double), make_fixtures.py, fixtures/ (sanitized pages)
sim/        the simulator: plant, food, probes, controllers, device, clock, engine, server,
            scenario, run, summary, history, fit, identify, eta, cli
scenarios/  the cooks `sim run` can play: brisket-12h, ribs-6h, lid-open, fuel-out, cold-windy
recon/      raw device captures (config.xml, wifi.htm, email.htm are gitignored: credentials)
```
