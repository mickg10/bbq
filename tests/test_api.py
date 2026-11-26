"""API tests against the fake CyberQ (task 4): status, config, history,
writes with audit, cooks, and the unreachable-device path."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import db as appdb
from app.device import WRITE_FIELDS
from app.main import Settings, create_app


@pytest.fixture()
def make_app(fake_device: str, tmp_path):
    """Factory: an app pointed at the fake device and a temp DB.

    poll_interval="0" disables the background loop (tests drive polling
    via /api/status's lazy seed); a small value runs the real loop."""

    def _make(poll_interval: str = "0"):
        settings = Settings(
            env={
                "CYBERQ_URL": fake_device,
                "DB_PATH": str(tmp_path / "bbq.db"),
                "POLL_INTERVAL": poll_interval,
            }
        )
        return create_app(settings)

    return _make


def test_healthz(make_app) -> None:
    with TestClient(make_app()) as client:
        assert client.get("/healthz").json() == {"ok": True}


def test_app_requires_cyberq_url(tmp_path) -> None:
    """No default device address: startup fails fast with a clear error
    when CYBERQ_URL is unset (nothing to clean up — the check runs before
    the DB or the client is created)."""
    settings = Settings(env={"DB_PATH": str(tmp_path / "x.db"), "POLL_INTERVAL": "0"})
    with pytest.raises(RuntimeError, match="CYBERQ_URL"):
        with TestClient(create_app(settings)):
            pass


def test_status_lazy_seed(make_app) -> None:
    with TestClient(make_app()) as client:
        response = client.get("/api/status")
        assert response.status_code == 200
        body = response.json()
        assert body["device"]["reachable"] is True
        assert body["device"]["last_error"] is None
        sample = body["sample"]
        assert sample["probes"]["cook"]["name"] == "Cook"
        assert 75.0 < sample["probes"]["cook"]["temp"] < 85.0
        assert sample["probes"]["food2"]["temp"] is None  # OPEN
        assert sample["probes"]["food2"]["status"] == 4
        assert isinstance(sample["output_pct"], int)
        assert sample["ts"] > 0


def test_config_has_no_credentials(make_app) -> None:
    with TestClient(make_app()) as client:
        response = client.get("/api/config")
        assert response.status_code == 200
        body = response.json()
        # all four sections per the scope update, fwver included
        assert {"system", "control", "email", "wifi", "fwver"} <= set(body)
        assert body["system"]["lcd_backlight"] == 50
        assert body["email"]["smtp_host"] == "smtp.fixture.invalid"
        assert body["wifi"]["home"]["ssid"] == "fixture-net"
        assert body["wifi"]["away"]["ssid"] == "fixture-away"
        assert body["wifi"]["multi_wifi_choice"] == 0
        assert body["wifi"]["http_port"] == 80
        # credentials are dropped on parse: field names AND values
        text = response.text.lower()
        for banned in ("smtp_pwd", "wifi_key", "http_pass", "fixture-password",
                       "fixture-wifi-key"):
            assert banned not in text, banned


def test_patch_config_control_and_audit(make_app, device_log) -> None:
    app = make_app()
    with TestClient(app) as client:
        assert client.get("/api/config").status_code == 200  # seed cache
        response = client.patch(
            "/api/config/control", json={"cookhold": 205.5, "cyctime": 8}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["set"] is True
        assert body["config"]["control"]["cookhold"] == 2055
        assert body["config"]["control"]["cyctime"] == 8

        rows = app.state.conn.execute(
            "SELECT field, old, new FROM audit ORDER BY rowid"
        ).fetchall()
        assert [row["field"] for row in rows] == ["control.cookhold", "control.cyctime"]
        assert (rows[0]["old"], rows[0]["new"]) == ("2000", "2055")
        assert (rows[1]["old"], rows[1]["new"]) == ("6", "8")

        # cache invalidated: the next read reflects the write immediately
        assert client.get("/api/config").json()["control"]["cookhold"] == 2055

    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts[-1]["path"] == "/control.htm"
    assert dict(posts[-1]["form"])["COOKHOLD"] == "205.5"


def test_patch_config_system(make_app) -> None:
    with TestClient(make_app()) as client:
        response = client.patch(
            "/api/config/system", json={"lcd_backlight": 70, "key_beeps": 1}
        )
        assert response.status_code == 200
        assert response.json()["config"]["system"]["lcd_backlight"] == 70
        # clamped like the client layer
        response = client.patch("/api/config/system", json={"lcd_backlight": 500})
        assert response.json()["config"]["system"]["lcd_backlight"] == 100


def test_patch_config_email_password_is_write_only(make_app) -> None:
    app = make_app()
    with TestClient(app) as client:
        response = client.patch(
            "/api/config/email",
            json={"smtp_host": "relay.example.com", "smtp_port": 587,
                  "smtp_pwd": "brand-new-secret"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["set"] is True
        assert body["config"]["email"]["smtp_host"] == "relay.example.com"
        # the password never rides in any response
        assert "smtp_pwd" not in body["config"]["email"]
        assert "brand-new-secret" not in response.text

        # the audit row records the change with NULL values
        rows = app.state.conn.execute(
            "SELECT field, old, new FROM audit ORDER BY rowid"
        ).fetchall()
        by_field = {row["field"]: row for row in rows}
        assert by_field["email.smtp_pwd"]["old"] is None
        assert by_field["email.smtp_pwd"]["new"] is None
        assert by_field["email.smtp_host"]["new"] == "relay.example.com"


def test_patch_config_wifi_requires_confirm(make_app) -> None:
    with TestClient(make_app()) as client:
        response = client.patch("/api/config/wifi", json={"ssid": "oops"})
        assert response.status_code == 400
        assert "confirm" in response.json()["detail"]


def test_patch_config_wifi_round_trip(make_app, device_log) -> None:
    app = make_app()
    with TestClient(app) as client:
        response = client.patch(
            "/api/config/wifi",
            json={"confirm": True, "ssid": "newnet", "multi_wifi_choice": 1},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["set"] is True
        assert body["config"]["wifi"]["home"]["ssid"] == "newnet"
        assert body["config"]["wifi"]["multi_wifi_choice"] == 1

        posts = [e for e in device_log() if e["method"] == "POST"]
        assert posts[-1]["path"] == "/wifi.htm"
        form = dict(posts[-1]["form"])
        # keys sent as the keep-sentinel; no HTTP auth fields echoed
        assert form["WIFI_KEY"] == "********************"
        assert "HTTP_USER" not in form
        assert "HTTP_PASS" not in form

        # http auth changes go through when explicit, and audit redacts them
        response = client.patch(
            "/api/config/wifi",
            json={"confirm": True, "http_pass": "hush-hush", "http_user": "pitboss"},
        )
        assert response.status_code == 200
        assert "hush-hush" not in response.text
        rows = app.state.conn.execute(
            "SELECT field, old, new FROM audit WHERE field LIKE 'wifi.%'"
            " ORDER BY rowid"
        ).fetchall()
        auth_row = next(r for r in rows if r["field"] == "wifi.http_pass")
        assert auth_row["old"] is None and auth_row["new"] is None


def test_email_test_and_wifi_scan(make_app, device_log) -> None:
    with TestClient(make_app()) as client:
        assert client.post("/api/email/test").json() == {"sent": True}
        assert client.post("/api/wifi/scan").json() == {"started": True}
    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts[-2]["path"] == "/email.htm"
    assert dict(posts[-2]["form"])["SMTP_NOW"] == "1"
    assert posts[-1]["path"] == "/scan.htm"


def test_reboot_disabled_by_default(make_app, device_log) -> None:
    with TestClient(make_app()) as client:
        response = client.post("/api/reboot", json={"confirm": True})
        assert response.status_code == 403
        assert "ALLOW_REBOOT" in response.json()["detail"]
        assert client.get("/api/features").json() == {"reboot": False}
    posts = [e for e in device_log() if e["method"] == "POST"]
    assert all(e["path"] != "/booting.htm" for e in posts)


def test_reboot_when_enabled_needs_confirm_and_audits(fake_device, tmp_path):
    from dev import fake_cyberq

    fake_cyberq.reset()
    settings = Settings(
        env={
            "CYBERQ_URL": fake_device,
            "DB_PATH": str(tmp_path / "bbq.db"),
            "POLL_INTERVAL": "0",
            "ALLOW_REBOOT": "1",
        }
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/features").json() == {"reboot": True}
        assert client.post("/api/reboot", json={}).status_code == 400
        assert client.post("/api/reboot", json={"confirm": True}).json() == {
            "rebooting": True
        }
        rows = app.state.conn.execute(
            "SELECT field FROM audit ORDER BY rowid"
        ).fetchall()
        assert [row["field"] for row in rows] == ["reboot"]
    assert fake_cyberq.STATE["REBOOTS"] == 1


def test_patch_probe_round_trip_and_audit(make_app, device_log) -> None:
    app = make_app()
    with TestClient(app) as client:
        client.get("/api/status")  # seed the current sample
        response = client.patch(
            "/api/probes/cook", json={"name": "Brisket", "set_f": 225}
        )
        assert response.status_code == 200
        sample = response.json()["sample"]
        assert sample["probes"]["cook"]["name"] == "Brisket"
        assert sample["probes"]["cook"]["set"] == 225.0

        rows = app.state.conn.execute(
            "SELECT field FROM audit ORDER BY rowid"
        ).fetchall()
        assert [row["field"] for row in rows] == ["cook.name", "cook.set"]

    # the POST that reached the fake carried only allowlisted fields
    posts = [e for e in device_log() if e["method"] == "POST"]
    assert posts
    for entry in posts:
        for name, _value in entry.get("form", []):
            assert name in WRITE_FIELDS, name


def test_patch_probe_validation(make_app) -> None:
    with TestClient(make_app()) as client:
        client.get("/api/status")
        assert client.patch("/api/probes/oven", json={"set_f": 100}).status_code == 404
        assert client.patch("/api/probes/cook", json={}).status_code == 400
        assert (
            client.patch("/api/probes/cook", json={"name": "x" * 17}).status_code == 400
        )
        response = client.patch("/api/probes/cook", json={"set_f": 999})
        assert response.status_code == 200
        assert response.json()["sample"]["probes"]["cook"]["set"] == 475.0


def test_put_timer(make_app) -> None:
    app = make_app()
    with TestClient(app) as client:
        client.get("/api/status")
        assert (
            client.put("/api/timer", json={"hhmmss": "01:99:00"}).status_code == 400
        )
        response = client.put("/api/timer", json={"hhmmss": "01:30:00"})
        assert response.status_code == 200
        assert response.json()["sample"]["timer_curr"] == "01:30:00"
        rows = app.state.conn.execute(
            "SELECT field FROM audit ORDER BY rowid"
        ).fetchall()
        assert [row["field"] for row in rows] == ["timer"]


def test_history_from_the_running_poller(make_app) -> None:
    with TestClient(make_app(poll_interval="0.05")) as client:
        time.sleep(1.2)  # let the loop collect samples across seconds
        now = int(time.time())
        response = client.get(f"/api/history?from={now - 120}&to={now}&step=1")
        assert response.status_code == 200
        body = response.json()
        assert body["step"] == 1
        points = body["points"]
        assert len(points) >= 2
        for point in points:
            if point["cook"] is not None:
                assert 75.0 < point["cook"] < 85.0


def test_history_validation(make_app) -> None:
    with TestClient(make_app()) as client:
        client.get("/api/status")
        assert client.get("/api/history?from=100&to=50").status_code == 422
        assert client.get("/api/history?from=0&to=2000000&step=1").status_code == 422
        # defaults and explicit ranges work
        body = client.get("/api/history?step=60").json()
        assert body["step"] == 60
        assert body["to"] - body["from"] == 24 * 3600


def _synthetic_sample(cook: float, food1: float | None) -> dict:
    probes = {}
    for probe, temp in (
        ("cook", cook),
        ("food1", food1),
        ("food2", None),
        ("food3", None),
    ):
        probes[probe] = {"name": probe, "temp": temp, "set": 180.0, "status": 0}
    return {
        "probes": probes,
        "output_pct": 50,
        "timer_curr": "00:00:00",
        "timer_status": 0,
        "deg_units": 1,
        "cyctime": 6,
        "propband": 300,
        "ramp": 0,
    }


def test_history_bucketing_unit(tmp_path) -> None:
    conn = appdb.connect(str(tmp_path / "unit.db"))
    base = 1000
    # ts 1000..1050 step 10, cook 100..105, food1 set for even offsets only
    for i in range(6):
        appdb.insert_sample(
            conn, base + i * 10, _synthetic_sample(100.0 + i, 70.0 + i if i % 2 == 0 else None)
        )
    points = appdb.history(conn, base, base + 100, 30)
    # buckets floor(ts/30)*30: [990: ts 1000,1010] [1020: ts 1020,1030,1040] [1050: ts 1050]
    assert [p["ts"] for p in points] == [990, 1020, 1050]
    assert points[0]["cook"] == pytest.approx(100.5)  # avg(100, 101)
    assert points[1]["cook"] == pytest.approx(103.0)  # avg(102, 103, 104)
    assert points[2]["cook"] == pytest.approx(105.0)
    # NULLs do not contribute: food1 present at ts 1000, 1020, 1040 (70, 72, 74)
    assert points[0]["food1"] == pytest.approx(70.0)
    assert points[1]["food1"] == pytest.approx(73.0)  # avg(72, 74)
    assert points[2]["food1"] is None
    with pytest.raises(ValueError):
        appdb.history(conn, 0, 10, 0)


def test_status_when_device_unreachable(tmp_path) -> None:
    settings = Settings(
        env={
            "CYBERQ_URL": "http://127.0.0.1:9",  # nothing listens here
            "DB_PATH": str(tmp_path / "down.db"),
            "POLL_INTERVAL": "0.05",
        }
    )
    with TestClient(create_app(settings)) as client:
        time.sleep(0.3)
        body = client.get("/api/status").json()
        assert body["device"]["reachable"] is False
        assert body["device"]["consecutive_failures"] >= 1
        assert body["device"]["last_error"]
        assert body["sample"] is None
        assert client.get("/api/config").status_code == 503


def test_cooks_crud(make_app) -> None:
    with TestClient(make_app()) as client:
        response = client.post("/api/cooks", json={"name": "Brisket run", "notes": "first"})
        assert response.status_code == 200
        cook = response.json()
        assert cook["id"] == 1 and cook["ended_ts"] is None
        assert cook["started_ts"] > 0

        assert client.get("/api/cooks").json() == [cook]

        ended = client.patch("/api/cooks/1", json={"end": True}).json()
        assert ended["ended_ts"] >= cook["started_ts"]

        renamed = client.patch("/api/cooks/1", json={"name": "Brisket 2"}).json()
        assert renamed["name"] == "Brisket 2"

        assert client.patch("/api/cooks/999", json={"end": True}).status_code == 404
        assert client.post("/api/cooks", json={"name": ""}).status_code == 422
