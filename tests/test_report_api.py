"""``GET /api/report``: a cook as it went, from the stored history.

The quick tests seed a database by hand. The slow ones, which matter most,
use a recording shaped like a real one, made by the simulator so that the
truth is known: the pit probe out for a stretch with the fan off, food probes
that were never plugged in, a hole where the device was away, a lid event.

The scorecard and the law are the simulator's own (sim/summary.py,
sim/identify.py) and are tested there. Here it is the reading of a recording.
"""

from __future__ import annotations

import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from app import db as appdb
from app import report
from app.main import Settings, create_app
from sim.engine import Event

MINUTE = 60
HOUR = 3600
START = 1_790_000_000          # where the real-shaped recording starts
PIT = 225.0                    # its setpoint


@pytest.fixture()
def make_app(tmp_path):
    """Factory: an app on a temp database, its poller off and its device a
    dead address. The report must never reach the device."""

    def _make(env: dict[str, str] | None = None):
        settings = Settings(env={
            "CYBERQ_URL": "http://127.0.0.1:9", "DB_PATH": str(tmp_path / "bbq.db"), "POLL_INTERVAL": "0", **(env or {}),
        })
        return create_app(settings)

    return _make


def a_sample(cook=PIT, cook_set=PIT, cook_status=0, food1=190.0, food1_status=0, output=40, timer_status=0) -> dict:
    """A sample as the poller hands one to the database."""
    probes = {
        "cook": {"temp": cook, "set": cook_set, "status": cook_status},
        "food1": {"temp": food1, "set": 203.0, "status": food1_status},
        "food2": {"temp": None, "set": 203.0, "status": 4},
        "food3": {"temp": None, "set": 203.0, "status": 4},
    }
    return {"ts": 0, "probes": probes, "output_pct": output, "timer_curr": "00:00:00", "timer_status": timer_status}


def store(conn, rows: list[dict], *, start: int | None = None, gap: tuple[int, int] | None = None) -> int:
    """Write the samples a minute apart, the last one at ``start`` if given,
    else now (the endpoint judges a history by its age). ``gap`` (seconds back
    from the last sample) is left out: the device was away. Returns the first ts."""
    span = (len(rows) - 1) * MINUTE
    start = int(time.time()) if start is None else start
    for offset, row in enumerate(rows):
        back = span - offset * MINUTE
        if gap and gap[0] <= back < gap[1]:
            continue
        appdb.insert_sample(conn, start - back, row)
    return start - span


# -- the endpoint, and what it refuses ------------------------------------------------------

def test_with_nothing_stored_it_says_so(make_app):
    with TestClient(make_app()) as client:
        body = client.get("/api/report").json()
    assert (body["range"]["basis"], body["range"]["hours"]) == ("default", 12.0)
    assert body["history"] == {"samples": 0, "stretch": None, "skipped": []}
    assert body["scorecard"] is None and body["verdict"] is None


def test_the_span_is_the_cook_the_hours_or_the_latest_cook(make_app):
    app = make_app()
    with TestClient(app) as client:
        conn, now = app.state.conn, int(time.time())
        store(conn, [a_sample()] * 30, start=now - 3 * HOUR)
        first = appdb.create_cook(conn, "brisket", "the long one", started_ts=now - 3 * HOUR)
        appdb.update_cook(conn, first, ended_ts=now - HOUR)
        second = appdb.create_cook(conn, "ribs", None, started_ts=now - 50 * MINUTE)

        by_cook = client.get(f"/api/report?cook={first}").json()["range"]
        assert (by_cook["basis"], by_cook["from"], by_cook["to"]) == ("cook", now - 3 * HOUR, now - HOUR)
        assert (by_cook["cook"]["id"], by_cook["cook"]["name"]) == (first, "brisket")
        last = client.get("/api/report?hours=1.5").json()["range"]
        assert (last["basis"], last["hours"]) == ("hours", 1.5)
        assert last["from"] == pytest.approx(now - 90 * MINUTE, abs=5)
        latest = client.get("/api/report").json()["range"]
        assert (latest["basis"], latest["cook"]["id"], latest["to"]) == ("latest cook", second, now)


def test_an_unknown_cook_or_an_absurd_span_is_refused(make_app):
    with TestClient(make_app()) as client:
        missing = client.get("/api/report?cook=99")
        assert (missing.status_code, missing.json()["detail"]) == (404, "no cook 99")
        for query in ("hours=0", "hours=-3", "hours=169", "cook=0", "cook=abc"):
            assert client.get(f"/api/report?{query}").status_code == 422, query


def test_the_report_is_cached_per_query(make_app, monkeypatch):
    calls: list[tuple[int | None, float | None]] = []
    real = report.cook_report
    monkeypatch.setattr(report, "cook_report",
                        lambda conn, **kw: (calls.append((kw.get("cook"), kw.get("hours"))), real(conn, **kw))[1])
    with TestClient(make_app({"REPORT_TTL": "30"})) as client:
        store(client.app.state.conn, [a_sample()] * 20)
        first = client.get("/api/report?hours=2").json()
        assert client.get("/api/report?hours=2").json() == first
        assert calls == [(None, 2.0)]                      # computed once, then served from the cache
        client.get("/api/report").json()
        assert calls == [(None, 2.0), (None, None)]        # another query: computed once more


# -- a recording with the pit probe out -------------------------------------------------------

def test_with_the_pit_probe_out_the_whole_time_it_still_reports(make_app):
    """The live case: the pit probe unplugged, the fan at 0, food1 reading."""
    app = make_app()
    with TestClient(app) as client:
        store(app.state.conn, [a_sample(cook=None, output=0, cook_status=4)] * 90)
        response = client.get("/api/report?hours=2")
        assert response.status_code == 200
        body = response.json()
    score = body["scorecard"]
    assert score["setpoint_f"] == PIT
    assert (score["to_setpoint_s"], score["settled_at_s"], score["steady_error_f"]) == (None, None, None)
    assert score["off_target"] is False                     # nothing held, so nothing to hold against
    assert score["fan_mean_pct"] == 0.0
    assert score["food"]["food2"] == {"plugged": False}
    assert body["verdict"]["state"] == "refused"
    assert "pit probe reads OPEN" in body["verdict"]["message"]


def test_a_stretch_far_from_the_setpoint_is_not_called_settled(make_app):
    """The pit at 76 °F under a 190 °F setpoint with the fan flat out, as an
    idle cooker is: reported for what it is, with its fan."""
    app = make_app()
    with TestClient(app) as client:
        store(app.state.conn, [a_sample(cook=76.0, cook_set=190.0, output=100, cook_status=2)] * 120)
        score = client.get("/api/report?hours=3").json()["scorecard"]
    assert score["held_s"] == 120 * MINUTE
    assert score["steady_error_f"] == pytest.approx(-114.0)
    assert score["off_target"] is True
    assert score["fan_mean_pct"] == 100.0


def test_an_open_pit_probe_is_a_missing_reading_never_the_last_one_repeated(make_app):
    """Held at 225 °F, then the probe out for half an hour while the cooker
    cooled (the fan off), then back reading 150 °F and climbing. Nothing may
    be scored over the outage, and the probe coming back low is not a fall."""
    rows = [a_sample(output=35)] * 50 + [a_sample(cook=None, output=0, cook_status=4)] * 30
    rows += [a_sample(cook=150.0 + 5 * k, output=100, cook_status=2 if k < 14 else 0) for k in range(16)]
    app = make_app()
    with TestClient(app) as client:
        store(app.state.conn, rows)
        body = client.get("/api/report?hours=3").json()
    score = body["scorecard"]
    assert score["cook_status_s"] == {"OK": 50 * MINUTE + 2 * MINUTE, "ERROR": 30 * MINUTE, "LOW": 14 * MINUTE}
    assert score["lid"] == []                             # not carried through the outage, so no cliff coming back
    assert score["overshoot_f"] == 0.0
    assert score["held_s"] == pytest.approx(50 * MINUTE, abs=2 * MINUTE)   # the stretch before the probe came out
    assert score["off_target"] is False
    # and too little was left with the fan off its limits to read a law from
    assert body["verdict"]["state"] == "refused" and "pinned at 0 or 100 %" in body["verdict"]["message"]


@pytest.mark.parametrize("level, falls_to, comes_back, expected", [
    (PIT, 190.0, True, 1),        # the lid off a 225 °F cooker, and back on
    (PIT, 190.0, False, 0),       # the same fall, never recovered: the cooker going cold
    (80.0, 60.0, True, 0),        # a cold cooker dropping: the fire out, or the probe out
    (PIT, PIT - 3.0, True, 0),    # 1 °F a minute is a sag, not a lid
])
def test_only_a_fall_from_a_hot_cooker_that_recovers_is_a_lid(level, falls_to, comes_back, expected):
    """The lid is inferred from the pit reading alone: it fell faster than
    5 °F in a minute, from a cooking temperature, and it came back."""
    cook = [level] * 12 + [level - (level - falls_to) * k / 3 for k in (1, 2, 3)] + [falls_to] * 3
    cook += ([falls_to + (level - falls_to) * k / 4 for k in (1, 2, 3, 4)] + [level] * 5) if comes_back \
        else [falls_to - k for k in range(8)]
    times = [float(k) * MINUTE for k in range(len(cook))]
    assert len(report.lid_events(times, cook)) == expected

    app = create_app(Settings(env={"CYBERQ_URL": "http://127.0.0.1:9", "DB_PATH": ":memory:", "POLL_INTERVAL": "0"}))
    with TestClient(app) as client:
        first = store(app.state.conn, [a_sample(cook=value) for value in cook])
        events = client.get("/api/report?hours=2").json()["scorecard"]["lid"]
    assert len(events) == expected
    if expected:
        # the report reads the recording on a minute grid
        grid = first // MINUTE * MINUTE
        assert events[0]["at_ts"] == grid + 11 * MINUTE            # the last reading before the fall
        assert events[0]["low_f"] == pytest.approx(falls_to)
        assert events[0]["low_ts"] == grid + 14 * MINUTE            # the first reading at the bottom
        assert events[0]["back_ts"] == grid + 21 * MINUTE           # within 5 °F of where it had been


def test_the_recording_is_scored_on_its_longest_unbroken_stretch(make_app):
    """Away for 40 minutes, an hour and forty minutes before the end: the
    shorter piece, the first hour, is listed and not scored."""
    app = make_app()
    with TestClient(app) as client:
        first = store(app.state.conn, [a_sample()] * 200, gap=(100 * MINUTE, 140 * MINUTE))
        body = client.get("/api/report?hours=6").json()
    history = body["history"]                                   # a stretch runs from its first sample to its last
    assert history["samples"] == 160
    assert (history["stretch"]["from"], history["stretch"]["to"], history["stretch"]["samples"]) \
        == (first + 100 * MINUTE, first + 199 * MINUTE, 100)
    assert [(p["from"], p["to"], p["samples"]) for p in history["skipped"]] == [(first, first + 59 * MINUTE, 60)]


def test_a_missing_minute_ends_the_scored_stretch_but_is_not_a_hole(make_app):
    """One minute without a sample is not the device going away (the history
    still holds all of it), but time is counted as samples times their
    spacing, so the score stops there and takes the longer side."""
    rows = [a_sample()] * 90
    app = make_app()
    with TestClient(app) as client:
        first = store(app.state.conn, rows, gap=(59 * MINUTE, 60 * MINUTE))     # the 31st minute is missing
        body = client.get("/api/report?hours=3").json()
    assert body["history"]["samples"] == 89 and body["history"]["skipped"] == []
    assert body["history"]["stretch"]["from"] == first              # one piece, as far as the history goes
    assert body["scorecard"]["duration_s"] == 58 * MINUTE          # scored on the 59 minutes after it
    assert body["verdict"]["state"] == "refused"                   # too little to read a law from


def test_food_done_times_come_from_the_recorded_statuses(make_app):
    app = make_app()
    with TestClient(app) as client:
        rows = [a_sample(food1=170.0 + k / 5) for k in range(100)]
        for row in rows[80:]:
            row["probes"]["food1"].update(temp=203.0, status=3)
        first = store(app.state.conn, rows)
        food = client.get("/api/report?hours=3").json()["scorecard"]["food"]
    assert food["food1"]["done_at_s"] == 80 * MINUTE
    assert food["food1"]["done_ts"] == first // MINUTE * MINUTE + 80 * MINUTE
    assert food["food2"] == {"plugged": False}


# -- slow: a recording shaped like a real one, with the truth known ----------------------------

def a_real_shaped_recording(path: pathlib.Path) -> list[dict]:
    """Made by the simulator, so that the truth is known: a cold start to
    225 °F under the PI controller, ribs on food1, food2 and food3 never
    plugged; the lid off for four minutes at 4 h; the pit probe out from
    5.5 h to 6.5 h, the fan off meanwhile; and the device away from 3 h to
    3.5 h. Ten hours, a row a minute, stored as the poller stores them.
    Returns every row the simulator produced, with its timestamp."""
    from sim.controllers import get_controller
    from sim.food import Food, preset
    from tests.sim.conftest import build_sim, record

    sim = build_sim(
        cook_set_f=PIT, food=Food(preset("ribs"), temp_f=70.0), food_set_f=195.0,
        controller=get_controller("pi"), lid_events=[(4 * HOUR, 4 * HOUR + 240.0)], dt=10.0, sample_every=6,
        events=[
            Event(5.5 * HOUR, lambda s: setattr(s.probe("cook"), "plugged", False), "pit probe out"),
            Event(6.5 * HOUR, lambda s: setattr(s.probe("cook"), "plugged", True), "pit probe in"),
        ],
    )
    sim.run(10 * HOUR)
    everything = list(sim.rows)
    away = lambda row: 3 * HOUR < row["t"] < 3.5 * HOUR  # noqa: E731
    sim.rows = [row for row in everything if not away(row)]
    record(path, sim, start_ts=START)
    sim.rows = everything
    for row in everything:
        row["ts"] = START + round(row["t"])
    return everything


@pytest.fixture(scope="module")
def real_shaped(tmp_path_factory) -> tuple[pathlib.Path, list[dict]]:
    path = tmp_path_factory.mktemp("report") / "history.db"
    return path, a_real_shaped_recording(path)


@pytest.mark.slow
def test_a_real_shaped_recording_is_read_right(real_shaped):
    path, rows = real_shaped
    kept = [row for row in rows if not 3 * HOUR < row["t"] < 3.5 * HOUR]      # what was stored
    stretch = [row for row in kept if row["t"] >= 3.5 * HOUR]                 # the longest unbroken piece
    conn = appdb.connect(str(path))
    try:
        body = report.cook_report(conn, hours=12, now=START + 10 * HOUR + MINUTE)
    finally:
        conn.close()

    history = body["history"]
    assert history["samples"] == len(kept)
    assert (history["stretch"]["from"], history["stretch"]["to"]) == (stretch[0]["ts"], stretch[-1]["ts"])
    away_from, away_to = kept[0]["ts"], max(row["ts"] for row in kept if row["t"] <= 3 * HOUR)
    assert [(p["from"], p["to"]) for p in history["skipped"]] == [(away_from, away_to)]

    score = body["scorecard"]
    reached = next(row for row in stretch if row["cook"] >= PIT)
    assert score["to_setpoint_ts"] == pytest.approx(reached["ts"], abs=2 * MINUTE)
    readings = [row["cook"] for row in stretch if row["cook"] is not None]
    assert score["overshoot_f"] == pytest.approx(max(readings) - PIT, abs=0.3)
    assert (score["setpoint_f"], score["off_target"]) == (PIT, False)
    assert score["held_s"] >= 30 * MINUTE and abs(score["steady_error_f"]) < 5.0
    assert score["fan_mean_pct"] == pytest.approx(
        sum(row["output_pct"] for row in stretch) / len(stretch), abs=1.0)
    assert score["cook_status_s"].get("ERROR") == pytest.approx(3600.0, abs=2 * MINUTE)   # the hour the probe was out
    # the lid: off at 4 h, down, and back within 5 °F of where it had been. Just
    # the one: the probe coming back low after its hour out is not a lid.
    (lid,) = score["lid"]
    assert score["lid_holds"] == 1
    assert lid["at_ts"] == pytest.approx(START + 4 * HOUR, abs=3 * MINUTE)
    around = [row for row in stretch if abs(row["t"] - 4 * HOUR) < HOUR]
    low = min(around, key=lambda row: row["cook"])
    assert lid["low_f"] == pytest.approx(low["cook"], abs=0.5)
    assert lid["low_ts"] == pytest.approx(low["ts"], abs=2 * MINUTE)      # the first reading at the bottom
    assert lid["back_ts"] is not None and lid["back_ts"] > lid["low_ts"]
    # the food: done when the device said so; nothing else was ever plugged in
    done = next(row for row in stretch if row["food1_status"] == 3)
    assert score["food"]["food1"]["done_ts"] == pytest.approx(done["ts"], abs=2 * MINUTE)
    assert score["food"]["food2"] == {"plugged": False} and score["food"]["food3"] == {"plugged": False}
    # the law: the PI controller that drove it, read through the hole, the outage
    # and the lid, with the lid's window left out of the fit
    verdict = body["verdict"]
    assert verdict["state"] == "identified"
    assert verdict["law"] == "PI"
    assert verdict["params"]["band"] == pytest.approx(30.0, abs=3.0)
    assert verdict["params"]["ki"] == pytest.approx(0.1, abs=0.03)
    assert verdict["rmse_pp"] < 1.5
    assert verdict["lid_windows_left_out"] == 1
    assert "selected PI" in verdict["text"]


@pytest.mark.slow
def test_the_real_shaped_report_through_the_endpoint(real_shaped):
    path, rows = real_shaped
    settings = Settings(env={"CYBERQ_URL": "http://127.0.0.1:9", "DB_PATH": str(path), "POLL_INTERVAL": "0"})
    with TestClient(create_app(settings)) as client:
        conn = client.app.state.conn
        cook = appdb.create_cook(conn, "ribs", None, started_ts=START)
        appdb.update_cook(conn, cook, ended_ts=rows[-1]["ts"])
        response = client.get(f"/api/report?cook={cook}")
        assert response.status_code == 200
        body = response.json()
    assert body["range"]["basis"] == "cook" and body["range"]["hours"] is None
    assert body["scorecard"]["lid"] and body["verdict"]["state"] == "identified"
    assert body["history"]["samples"] == len([row for row in rows if not 3 * HOUR < row["t"] < 3.5 * HOUR])
