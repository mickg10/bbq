"""The simulator served: a real port, the background stepper running, and
the dashboard (app.main) driving it exactly as it drives the real CyberQ.

Speeds are high (600× by default) so a cook's first hour takes six seconds;
the tests wait on simulated time, not wall time, so they do not depend on how
fast the machine is.

Marked ``slow``: the default pytest run skips this module (see pyproject).
Run it with ``pytest -m slow``.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.device import parse_all_xml, parse_status_xml
from app.main import Settings, create_app
from sim.device import parse_hhmmss

pytestmark = pytest.mark.slow

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _until(predicate, timeout: float = 15.0, message: str = "condition") -> None:
    """Wait (bounded) for the stepping simulator to satisfy ``predicate``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail(f"timed out waiting for {message}")


def _cook_temp(url: str) -> float:
    return parse_all_xml(httpx.get(f"{url}/all.xml", timeout=5).text)["probes"]["cook"]["temp"]


def _status(url: str) -> dict:
    return parse_status_xml(httpx.get(f"{url}/status.xml", timeout=5).text)


# -- the served device --------------------------------------------------------

def test_the_served_device_is_live(serve_sim):
    url, sim = serve_sim(cook_set_f=225.0)
    _until(lambda: sim.steps > 0, message="the first step")
    first = _cook_temp(url)
    assert 68.0 < first < 73.0                    # a cold kettle
    _until(lambda: sim.clock.t >= 900, message="15 simulated minutes")
    second = _cook_temp(url)
    assert second > first + 15.0                  # and rising, 15 minutes in
    status = _status(url)
    assert status["output_pct"] == 100            # far below target: full fan
    assert status["probes"]["cook"]["status"] == 2  # LOW
    assert status["probes"]["food1"]["temp"] is None  # OPEN


def test_speed_is_honoured(serve_sim):
    """At 1x, real seconds are simulated seconds."""
    url, sim = serve_sim(speed=1.0)
    _until(lambda: sim.steps > 0, message="the first step")
    start = sim.clock.t
    time.sleep(2.0)
    assert 1.0 <= sim.clock.t - start <= 4.0


def test_booting_is_recorded_and_nothing_resets(serve_sim):
    url, sim = serve_sim(preheat=215.0, cook_set_f=225.0)
    _until(lambda: sim.steps > 5, message="a few steps")
    response = httpx.post(f"{url}/booting.htm", content="RESET_CPU=1&Yes=Yes", headers=FORM, timeout=5)
    assert response.status_code == 200
    steps, settings = sim.steps, dict(sim.device.settings.__dict__)
    log = httpx.get(f"{url}/_log", timeout=5).json()
    assert dict(log[-1]["form"]) == {"RESET_CPU": "1", "Yes": "Yes"}
    assert settings["cook_set_f"] == 225.0          # nothing was forgotten
    _until(lambda: sim.steps >= steps + 10, message="the simulation to carry on")


def test_the_served_history_stays_bounded(serve_sim):
    _, sim = serve_sim(max_rows=300)
    _until(lambda: sim.clock.t >= 400, message="400 simulated seconds")
    assert len(sim.rows) == 300          # capped: a server can run for days
    assert sim.rows[-1]["t"] == pytest.approx(sim.clock.t)


# -- the dashboard against it ---------------------------------------------------

@pytest.fixture()
def dashboard(serve_sim, tmp_path):
    """The real app, unchanged, pointed at a served simulator."""
    made = []

    def _start(poll_interval: str = "0", **kwargs):
        url, sim = serve_sim(**kwargs)
        settings = Settings(env={
            "CYBERQ_URL": url,
            "DB_PATH": str(tmp_path / f"bbq-{len(made)}.db"),
            "POLL_INTERVAL": poll_interval,
        })
        made.append(url)
        return create_app(settings), sim

    return _start


def test_the_dashboard_reads_a_live_cook(dashboard):
    app, sim = dashboard(poll_interval="0.05", cook_set_f=225.0)
    with TestClient(app) as client:
        first = client.get("/api/status").json()
        assert first["device"]["reachable"] is True
        assert first["device"]["last_error"] is None
        assert 68.0 < first["sample"]["probes"]["cook"]["temp"] < 75.0
        assert first["sample"]["probes"]["cook"]["set"] == 225.0
        assert first["sample"]["output_pct"] == 100
        assert first["sample"]["probes"]["food1"]["temp"] is None  # nothing plugged in
        _until(lambda: sim.clock.t >= 900, message="15 simulated minutes")
        time.sleep(0.1)                            # let the poller pick it up
        second = client.get("/api/status").json()
        assert second["device"]["reachable"] is True
        rise = second["sample"]["probes"]["cook"]["temp"] - first["sample"]["probes"]["cook"]["temp"]
        assert rise > 10.0
        assert second["sample"]["ts"] >= first["sample"]["ts"]


def test_history_collects_from_the_poller(dashboard):
    app, sim = dashboard(poll_interval="0.05", cook_set_f=225.0)
    with TestClient(app) as client:
        _until(lambda: sim.clock.t >= 1200, message="20 simulated minutes")
        time.sleep(1.2)  # the poller stamps samples with whole seconds
        now = int(time.time())
        body = client.get(f"/api/history?from={now - 120}&to={now}&step=1").json()
    points = body["points"]
    assert len(points) >= 2
    cooks = [p["cook"] for p in points if p["cook"] is not None]
    assert len(cooks) == len(points)
    assert max(cooks) - min(cooks) > 5.0            # a rising kettle, not a frozen one
    assert all(p["food1"] is None for p in points)  # OPEN stays NULL


def test_config_sections_come_from_the_simulated_device(dashboard):
    app, _ = dashboard()
    with TestClient(app) as client:
        body = client.get("/api/config").json()
    assert {"system", "control", "email", "wifi", "fwver"} <= set(body)
    assert body["control"]["propband"] == 300      # the documented 30 °F band
    assert body["control"]["cookhold"] == 2000
    assert body["wifi"]["home"]["ssid"] == "fixture-net"
    for banned in ("smtp_pwd", "wifi_key", "http_pass", "fixture-password", "fixture-wifi-key"):
        assert banned not in str(body).lower()


def test_dashboard_writes_reach_the_simulated_device(dashboard):
    app, sim = dashboard(poll_interval="0.05", preheat=215.0, cook_set_f=225.0)
    with TestClient(app) as client:
        url = app.state.settings.cyberq_url
        # a setpoint below the pit stops the fan, as it would on the real device
        response = client.patch("/api/probes/cook", json={"set_f": 150.0})
        assert response.status_code == 200
        assert response.json()["sample"]["probes"]["cook"]["set"] == 150.0
        assert sim.device.settings.cook_set_f == 150.0
        _until(lambda: sim.device.output.fan_pct == 0, message="the fan to stop")
        assert _status(url)["output_pct"] == 0
        # the control page: a narrower band is stored, served and run on
        response = client.patch("/api/config/control", json={"propband": 10.0})
        assert response.status_code == 200
        assert response.json()["config"]["control"]["propband"] == 100
        assert sim.device.settings.propband_f == 10.0
        assert client.get("/api/config").json()["control"]["propband"] == 100
        # the timer counts simulated time. At this speed a scheduling hiccup
        # is a dozen simulated seconds, so these are ranges — and what matters
        # is that the write took and that the dashboard shows the countdown.
        response = client.put("/api/timer", json={"hhmmss": "00:30:00"})
        assert response.status_code == 200
        written = response.json()["sample"]
        # a running timer is not a status: TIMER_STATUS stays OK (0) until a timeout
        assert (written["timer_status"], written["output_pct"]) == (0, 0)
        written_s = parse_hhmmss(written["timer_curr"])
        assert 1770 <= written_s <= 1800
        _until(lambda: sim.device.timer.remaining_s <= 1200, message="ten simulated minutes")
        time.sleep(0.1)  # let the poller pick the new value up
        sample = client.get("/api/status").json()["sample"]
        seen_s = parse_hhmmss(sample["timer_curr"])
        assert sample["timer_status"] == 0
        assert written_s - 700 < seen_s < written_s - 500  # ten minutes gone, still running


def test_reboot_stays_denied_against_the_simulator(dashboard):
    app, _ = dashboard(preheat=215.0)
    with TestClient(app) as client:
        assert client.post("/api/reboot", json={"confirm": True}).status_code == 403
        assert client.get("/api/features").json() == {"reboot": False}
    posts = [entry for entry in httpx.get(f"{app.state.settings.cyberq_url}/_log", timeout=5).json()
             if entry["method"] == "POST"]
    assert all(entry["path"] != "/booting.htm" for entry in posts)
