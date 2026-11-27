"""``GET /api/eta``: when each food probe will be done, from the stored history.

The estimating is tested on its own (tests/sim/test_eta.py). Here it is the
feeding from the database, the refusals, the cache, and that the endpoint asks
the device for nothing.
"""

from __future__ import annotations

import math
import pathlib
import re
import shutil
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import db as appdb
from app import eta as app_eta
from app.main import Settings, create_app

ROOT = pathlib.Path(__file__).resolve().parent.parent
MINUTE = 60
PIT, SET = 225.0, 203.0
#: how long a lump with a 3 h time constant, nothing evaporating, takes from
#: 190 °F to 203 °F in a 225 °F pit: tau · ln(35 / 22)
FROM_190_MIN = 3.0 * 60.0 * math.log((PIT - 190.0) / (PIT - SET))


@pytest.fixture()
def make_app(tmp_path):
    """Factory: an app on a temp database, its poller off and its device a
    dead address. The estimate must never reach the device, so any call there
    would fail the test that made it."""

    def _make(env: dict[str, str] | None = None):
        settings = Settings(env={
            "CYBERQ_URL": "http://127.0.0.1:9", "DB_PATH": str(tmp_path / "bbq.db"), "POLL_INTERVAL": "0", **(env or {}),
        })
        return create_app(settings)

    return _make


def a_sample(food1=190.0, set1=SET, status1=0, cook=PIT, food2=None, food3=None) -> dict:
    """A parsed sample, as the poller hands one to the database."""
    probes = {"cook": {"temp": cook, "set": PIT, "status": 0, "name": "Cook"}}
    for name, temp, set_f in (("food1", food1, set1), ("food2", food2, SET), ("food3", food3, SET)):
        status = status1 if name == "food1" else (4 if temp is None else 0)
        probes[name] = {"temp": temp, "set": set_f, "status": status, "name": name}
    return {"ts": 0, "probes": probes, "output_pct": 50, "timer_curr": "00:00:00", "timer_status": 0}


def seed(conn, samples: list[dict], *, end: int | None = None) -> int:
    """Store the samples a minute apart, the last one at ``end`` (now, unless
    given: the endpoint judges a history by its age). Returns that ts."""
    end = int(time.time()) if end is None else end
    for offset, sample in enumerate(samples):
        appdb.insert_sample(conn, end - MINUTE * (len(samples) - 1 - offset), sample)
    return end


def rising(count: int = 46, *, end: float = 190.0, tau_h: float = 3.0, pit: float = PIT) -> list[float]:
    """Food temperatures a minute apart, the last ``end``: a lump with nothing
    evaporating relaxing towards the pit, read back in time."""
    temps = [end]
    while len(temps) < count:
        temps.append(pit - (pit - temps[-1]) * math.exp(MINUTE / (tau_h * 3600.0)))
    return temps[::-1]


# -- the endpoint ---------------------------------------------------------------------------

def test_with_nothing_stored_it_says_so_and_asks_the_device_for_nothing(make_app, fake_device, device_log):
    """Against the fake CyberQ, with its request log: the estimate adds nothing to it."""
    with TestClient(make_app({"CYBERQ_URL": fake_device})) as client:
        body = client.get("/api/eta").json()
    assert set(body["probes"]) == {"food1", "food2", "food3"}
    assert all(probe == {"state": "none", "reason": "no samples yet"} for probe in body["probes"].values())
    assert device_log() == []                     # the estimate never polls


def test_the_estimate_comes_from_the_stored_history(make_app):
    app = make_app()
    with TestClient(app) as client:
        last = seed(app.state.conn, [a_sample(food1=temp, food3=158.0) for temp in rising()])
        body = client.get("/api/eta").json()
    food1 = body["probes"]["food1"]
    assert food1["state"] == "eta"
    assert (food1["temp_f"], food1["set_f"], food1["sample_ts"]) == (190.0, SET, last)
    assert food1["minutes"] == pytest.approx(FROM_190_MIN, abs=2.0)
    assert food1["band_min"] >= app_eta.eta.MIN_BAND_MIN
    assert food1["eta_ts"] == last + food1["minutes"] * MINUTE
    assert body["probes"]["food2"] == {"state": "none", "reason": "the probe is open or in error"}
    stalled = body["probes"]["food3"]
    assert stalled["state"] == "stalled" and stalled["minutes"] > 2 * 60


def test_the_estimate_is_anchored_to_the_latest_sample_not_the_clock(make_app):
    """With the device quiet for ten minutes nothing newer is known, so the
    clock time of the finish stays where the last sample put it."""
    app = make_app()
    with TestClient(app):
        last = seed(app.state.conn, [a_sample(food1=temp) for temp in rising()])
        sooner = app_eta.eta_report(app.state.conn, last + MINUTE)["probes"]["food1"]
        later = app_eta.eta_report(app.state.conn, last + 10 * MINUTE)["probes"]["food1"]
    assert sooner["minutes"] == later["minutes"] == round(FROM_190_MIN)
    assert (later["sample_ts"], later["age_s"]) == (last, 10 * MINUTE)
    assert later["eta_ts"] == sooner["eta_ts"] == last + later["minutes"] * MINUTE


@pytest.mark.parametrize("sample, reason", [
    (a_sample(status1=4), "the probe is open or in error"),
    (a_sample(food1=None), "the probe is open or in error"),
    (a_sample(status1=3), "done"),
    (a_sample(food1=SET), "done"),
    (a_sample(set1=None), "no setpoint"),
])
def test_nothing_is_estimated_for(make_app, sample, reason):
    app = make_app()
    with TestClient(app) as client:
        seed(app.state.conn, [sample] * 46)
        food1 = client.get("/api/eta").json()["probes"]["food1"]
    assert food1 == {"state": "none", "reason": reason}


def test_under_a_quarter_hour_of_data_says_so(make_app):
    app = make_app()
    with TestClient(app) as client:
        seed(app.state.conn, [a_sample(food1=temp) for temp in rising(count=10)])
        food1 = client.get("/api/eta").json()["probes"]["food1"]
    assert food1 == {"state": "none", "reason": "under 15 min of data"}


def test_a_history_nobody_is_adding_to_says_so(make_app):
    """The device unreachable for half an hour: the last estimate is not left standing as if live."""
    app = make_app()
    with TestClient(app) as client:
        seed(app.state.conn, [a_sample(food1=temp) for temp in rising()], end=int(time.time()) - 30 * MINUTE)
        food1 = client.get("/api/eta").json()["probes"]["food1"]
    assert food1 == {"state": "none", "reason": "the last sample is 30 min old"}


def test_the_report_is_cached_for_the_ttl(make_app, monkeypatch):
    calls: list[int] = []
    real = app_eta.eta_report
    monkeypatch.setattr(app_eta, "eta_report", lambda conn, now: (calls.append(now), real(conn, now))[1])

    with TestClient(make_app({"ETA_TTL": "30"})) as client:
        seed(client.app.state.conn, [a_sample(food1=temp) for temp in rising()])
        first = client.get("/api/eta").json()
        assert client.get("/api/eta").json() == first
        assert len(calls) == 1                       # computed once, then served from the cache
    with TestClient(make_app({"ETA_TTL": "0"})) as client:
        client.get("/api/eta"), client.get("/api/eta")
        assert len(calls) == 3


# -- what the image needs ----------------------------------------------------------------------

@pytest.mark.slow
def test_the_image_gets_the_simulator_files_the_app_imports(tmp_path):
    """The Dockerfile copies three files from sim/, by name. With only those
    present the app must still import: it must not reach for the rest."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    match = re.search(r"^COPY ((?:sim/\S+(?: |$))+)\./sim/$", dockerfile, re.M)
    assert match, "the Dockerfile no longer copies anything from sim/"
    tree = tmp_path / "srv"
    shutil.copytree(ROOT / "app", tree / "app", ignore=shutil.ignore_patterns("__pycache__"))
    for name in match.group(1).split():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, tree / name)
    run = subprocess.run(
        [sys.executable, "-c", "import app.main, app.eta, sim.eta; print(sim.eta.__name__)"],
        cwd=tmp_path, env={"PYTHONPATH": str(tree)}, capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "sim.eta"


def test_with_the_pit_probe_out_it_says_so_instead_of_failing(make_app):
    """Real case: the pit probe unplugged (cook reads OPEN) while food1 reads."""
    app = make_app()
    with TestClient(app) as client:
        seed(app.state.conn, [a_sample(food1=temp, cook=None) for temp in rising()])
        response = client.get("/api/eta")
    assert response.status_code == 200
    assert response.json()["probes"]["food1"] == {"state": "none", "reason": "no pit reading"}
    assert response.json()["probes"]["food2"]["state"] == "none"
