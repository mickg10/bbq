"""Recorded history: reading the dashboard's database, replaying a cook
through the model, and scoring it.

The ``recording`` fixture (conftest) is three hours of the simulator playing
a cooker whose parameters are known and are *not* the defaults — so a replay
with the right parameters has to come out at the noise floor, and one with
the defaults has to be visibly wrong.
"""

from __future__ import annotations

import contextlib
import csv
import sqlite3
from pathlib import Path

import pytest

from app import db as appdb
from sim.device import CyberQDevice, DeviceSettings
from sim.engine import Environment, Simulator
from sim.food import preset
from sim.history import (
    AFTER_PLUG_S,
    HistoryError,
    Track,
    format_score,
    load_track,
    mse,
    open_history,
    replay,
    score,
    write_comparison_csv,
)
from sim.plant import Cooker, Plant, PlantParams
from sim.probes import Probe
from tests.sim.conftest import AIR_F, TRUTH, record

DT = 5.0  # the replay's step: fast, and (see below) accurate to the noise floor


def _read(path, t_from: int | None = None, t_to: int | None = None) -> Track:
    with contextlib.closing(open_history(path)) as conn:
        return load_track(conn, t_from, t_to)


def _rmse(track: Track, played, probe: str = "cook", **score_kwargs) -> float:
    return score(track, played.simulated, **score_kwargs)["probes"][probe]["rmse"]


# -- reading the database -------------------------------------------------------

def test_a_recording_has_the_shape_the_dashboard_writes(recording):
    track = _read(recording.db)
    assert len(track.times) == 2160 and (len(track.times), track.times[0], track.times[-1]) == (2160, 0.0, 3 * 3600.0 - 5)
    assert track.start_ts == 1_790_000_005 and track.skipped == []  # the first row is at t = 5 s
    assert set(track.readings) == set(track.sets) == {"cook", "food1", "food2", "food3"}
    assert track.readings["food2"][:719] == [None] * 719 and track.readings["food2"][720] is not None
    assert track.readings["food3"] == [None] * 2160
    assert track.sets["cook"][0] == 250.0 and track.sets["cook"][-1] == 225.0
    assert track.output[0] == 100 and 0 <= min(track.output) and max(track.output) == 100


def test_the_longest_contiguous_piece_is_kept_and_the_rest_reported(tmp_path):
    path = tmp_path / "bbq.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(appdb.SCHEMA)
        conn.executemany(
            "INSERT INTO samples (ts, cook, cook_set, output_pct) VALUES (?, 100.0, 225.0, 50)",
            [(t,) for t in (0, 5, 10, 5000, 5005, 5010, 5015, 5020, 9000, 9005)],
        )
    track = _read(path)
    assert [len(track.times), track.duration] == [5, 20]
    assert track.skipped == [(0, 10, 3), (9000, 9005, 2)]
    window = _read(path, 0, 10)
    assert (len(window.times), window.skipped) == (3, [])
    with pytest.raises(HistoryError, match="fewer than two samples"):
        _read(path, 6000, 7000)  # nothing in that range
    with pytest.raises(HistoryError, match="fewer than two samples"):
        _read(path, 9002, 9500)  # one sample in that range


def test_history_is_only_ever_opened_for_reading(recording):
    conn = open_history(recording.db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM samples")
    conn.close()


def test_opening_a_history_leaves_nothing_beside_it(recording):
    """The dashboard's database is in WAL mode, and an ordinary read-only
    open of one creates -shm and -wal files next to it. Reading a history
    must not: not a scratch file in the tree, not a write."""
    beside = sorted(p.name for p in recording.db.parent.iterdir() if p.name.startswith(recording.db.name))
    assert beside == [recording.db.name]
    with open_history(recording.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2160
    assert sorted(p.name for p in recording.db.parent.iterdir() if p.name.startswith(recording.db.name)) == [
        recording.db.name
    ]
    assert recording.db.stat().st_mtime == recording.mtime


def test_a_history_someone_is_writing_is_read_the_ordinary_read_only_way(tmp_path):
    """With a WAL beside it — a dashboard mid-poll — the database is not
    immutable, and reading it sees what has been committed."""
    path = tmp_path / "live.db"
    writer = appdb.connect(str(path))
    writer.execute(
        "INSERT INTO samples (ts, cook, cook_set, output_pct) VALUES (0, 100.0, 225.0, 50), (5, 101.0, 225.0, 50)"
    )
    writer.commit()  # the connection stays open: so does its -wal
    assert Path(f"{path}-wal").exists()
    try:
        with open_history(str(path)) as conn:
            assert len(load_track(conn).times) == 2
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM samples")
    finally:
        writer.close()


def test_a_track_holds_each_value_until_the_next_sample():
    track = Track(0, [0.0, 10.0, 20.0], {}, {}, [], [])
    assert [track.at(["a", "b", "c"], t) for t in (-5.0, 0.0, 9.9, 10.0, 19.9, 20.0, 99.0)] == [
        "a", "a", "a", "b", "b", "c", "c",
    ]


# -- replaying ----------------------------------------------------------------------

#: what is in the cooker during the recording
BRISKET = {"food1": preset("brisket")}


def test_open_loop_with_the_right_parameters_is_at_the_noise_floor(recording):
    track = _read(recording.db)
    played = replay(track, ambient_f=AIR_F, params=recording.truth, foods=BRISKET, open_loop=True, dt=DT)
    assert played.open_loop and len(played.simulated["cook"]) == len(track.times)
    scored = score(track, played.simulated, exclude=[(0.0, 3600.0)])  # the cold start's fire is a guess
    pit = scored["probes"]["cook"]
    assert pit["rmse"] < 0.5 and abs(pit["bias"]) < 0.25 and pit["max_abs"] < 3.0
    assert pit["n"] == len(track.times) - 720
    assert scored["excluded"] == 720
    assert scored["transients"] == 6  # food2 warming from room temperature as it was plugged in


def test_the_step_size_does_not_decide_the_answer(recording):
    """A 5 s step and a 20 s step tell the same story: the coals' lag is what
    the step has to resolve, and it is minutes long."""
    track = _read(recording.db)
    rmse = {
        dt: _rmse(track, replay(track, ambient_f=AIR_F, params=TRUTH, foods=BRISKET, open_loop=True, dt=dt),
                  exclude=[(0.0, 3600.0)])
        for dt in (2.5, 5.0, 20.0)
    }
    assert max(rmse.values()) < 0.6
    assert max(rmse.values()) - min(rmse.values()) < 0.25


def test_the_default_parameters_are_visibly_wrong_for_this_cooker(recording):
    track = _read(recording.db)
    played = replay(track, ambient_f=AIR_F, foods=BRISKET, open_loop=True, dt=DT)
    assert played.sim.cooker.plant.params == PlantParams()
    pit = score(track, played.simulated)["probes"]["cook"]
    assert pit["rmse"] > 5.0 and pit["max_abs"] > 20.0


def test_food_the_model_was_not_told_about_shows_up_in_the_pit(recording):
    """Right parameters, but no brisket in the cooker: 12 lb of cold meat is
    a 5 °F error in the pit, all through the hold."""
    track = _read(recording.db)
    played = replay(track, ambient_f=AIR_F, params=TRUTH, open_loop=True, dt=DT)
    pit = score(track, played.simulated, exclude=[(0.0, 3600.0)])["probes"]["cook"]
    assert 4.0 < pit["bias"] < 6.0 and pit["rmse"] < 7.0


def test_closed_loop_the_simulated_controller_follows_the_recorded_setpoints(recording):
    track = _read(recording.db)
    played = replay(track, ambient_f=AIR_F, params=recording.truth, foods=BRISKET, dt=DT)
    assert not played.open_loop and played.sim.device.controller.name == "proportional"
    assert played.sim.device.settings.cook_set_f == 225.0  # the change at 2 h was replayed…
    assert track.sets["cook"][1439] == 250.0  # …from the sample it happened at
    scored = score(track, played.simulated, played.fan, exclude=[(0.0, 3600.0)])
    # the demand is whole percent: an exact match is luck, a point either way is parity
    assert scored["fan_exact"] > 0.4 and scored["fan_rmse"] < 2.5
    assert scored["probes"]["cook"]["rmse"] < 0.6


def test_no_food_goes_in_the_cooker_unless_it_is_named(recording):
    track = _read(recording.db)
    told = replay(track, ambient_f=AIR_F, params=TRUTH, foods=BRISKET, open_loop=True, dt=DT)
    assert told.sim.cooker.foods[0].params.mass_lb == 12.0
    assert _rmse(track, told, "food1") < 1.5           # modelled as the brisket it was
    untold = replay(track, ambient_f=AIR_F, params=TRUTH, open_loop=True, dt=DT)
    assert untold.sim.cooker.foods == (None, None, None)
    assert _rmse(track, untold, "food1") > 50.0        # reading the pit instead of a cold brisket
    # and it makes a difference to the pit too: 12 lb of cold meat draws heat
    assert _rmse(track, untold) > _rmse(track, told)


def test_a_probe_plugged_in_part_way_through_the_recording(recording):
    track = _read(recording.db)
    played = replay(track, ambient_f=AIR_F, params=TRUTH, foods=BRISKET, open_loop=True, dt=DT)
    food2 = played.simulated["food2"]
    assert food2[:720] == [None] * 720 and food2[720] is not None
    assert played.simulated["food3"] == [None] * len(track.times)  # and it was food2 that got plugged
    assert food2[720] == pytest.approx(track.readings["cook"][720], abs=3.0)  # nothing on it: the pit
    info = score(track, played.simulated)["probes"]["food2"]
    # what it showed just after being plugged in is not scored
    assert info["n"] == len(track.times) - 720 - int(AFTER_PLUG_S / 5)
    assert info["rmse"] < 1.0
    assert score(track, played.simulated)["probes"]["food3"] == {"n": 0}


def test_a_recording_that_starts_with_the_pit_probe_open_is_refused(recording):
    track = _read(recording.db)
    broken = Track(track.start_ts, track.times, {**track.readings, "cook": [None] + track.readings["cook"][1:]},
                   track.sets, track.output)
    with pytest.raises(HistoryError, match="starts with the pit probe open"):
        replay(broken, ambient_f=AIR_F)


# -- scoring ------------------------------------------------------------------------

def test_score_by_hand():
    times = [0.0, 10.0, 20.0, 30.0, 40.0]
    track = Track(
        0, times,
        {"cook": [100.0, 102.0, None, 106.0, 108.0], "food1": [60.0] * 5, "food2": [None] * 5, "food3": [None] * 5},
        {probe: [225.0] * 5 for probe in ("cook", "food1", "food2", "food3")},
        [50, 50, 50, 50, 50],
    )
    simulated = {
        "cook": [100.0, 103.0, 104.0, 105.0, 109.0], "food1": [60.5] * 5,
        "food2": [70.0] * 5, "food3": [None] * 5,
    }
    scored = score(track, simulated, fan=[50, 52, 50, 50, 50], after_plug_s=0.0)
    # cook: measured at 0, 1, 3, 4 -> errors 0, +1, -1, +1
    assert scored["probes"]["cook"] == {
        "n": 4, "rmse": pytest.approx(0.8660254, abs=1e-6), "mae": pytest.approx(0.75),
        "bias": pytest.approx(0.25), "max_abs": 1.0,
    }
    assert scored["probes"]["food1"]["rmse"] == pytest.approx(0.5) and scored["probes"]["food1"]["bias"] == 0.5
    assert scored["probes"]["food3"] == {"n": 0}       # never plugged in: nothing to compare
    assert (scored["samples"], scored["transients"], scored["excluded"]) == (5, 0, 0)
    assert scored["fan_exact"] == pytest.approx(0.8) and scored["fan_rmse"] == pytest.approx(0.894427, abs=1e-5)
    assert mse(scored) == pytest.approx(0.75)
    assert mse(scored, ("cook", "food1")) == pytest.approx((0.75 * 4 + 0.25 * 5) / 9)
    assert mse({"probes": {"cook": {"n": 0, "rmse": 1.0}}}) == float("inf")


def test_plug_and_unplug_spikes_are_left_out_of_the_score():
    cook = [200.0 + 0.1 * (i % 3) for i in range(10)] + [200.1, 296.2, 70.5, 200.3] + [200.2] * 10
    times = [float(i) for i in range(len(cook))]
    none = [None] * len(cook)
    track = Track(0, times, {"cook": cook, "food1": none, "food2": none, "food3": none},
                  {probe: [225.0] * len(cook) for probe in ("cook", "food1", "food2", "food3")}, [100] * len(cook))
    simulated = {"cook": [200.0 + 0.05 * i for i in range(len(cook))], "food1": none, "food2": none, "food3": none}
    scored = score(track, simulated, after_plug_s=0.0)
    # flagged: every sample within two of a jump bigger than JUMP_F. The jumps
    # are 10→11, 11→12 and 12→13 — a probe pulled out and pushed back — so
    # samples 8 to 15, ramp included; what is left is the still air either side
    assert scored["transients"] == 8
    info = scored["probes"]["cook"]
    assert (info["n"], info["max_abs"]) == (16, pytest.approx(0.95))  # 24 less the 8; index 23 is furthest
    assert info["bias"] == pytest.approx((sum(0.05 * i - 0.1 * (i % 3) for i in range(8))
                                          + sum(0.05 * i - 0.2 for i in range(16, 24))) / 16)


def test_what_a_probe_shows_right_after_being_plugged_in_is_not_scored():
    times = [float(i) * 5 for i in range(60)]
    readings = [None] * 20 + [200.0] * 40
    track = Track(0, times, {"cook": readings, "food1": [None] * 60, "food2": [None] * 60, "food3": [None] * 60},
                  {probe: [225.0] * 60 for probe in ("cook", "food1", "food2", "food3")}, [100] * 60)
    simulated = {"cook": [200.0] * 60, "food1": [None] * 60, "food2": [None] * 60, "food3": [None] * 60}
    assert score(track, simulated)["probes"]["cook"]["n"] == 40 - int(AFTER_PLUG_S / 5)
    assert score(track, simulated, after_plug_s=0.0)["probes"]["cook"]["n"] == 40
    assert score(track, simulated, exclude=[(0.0, 15.0)])["excluded"] == 3


def test_score_excludes_a_window_from_every_count():
    times = [float(i) * 5 for i in range(100)]
    track = Track(0, times, {"cook": [200.0] * 100, "food1": [None] * 100, "food2": [None] * 100, "food3": [None] * 100},
                  {probe: [225.0] * 100 for probe in ("cook", "food1", "food2", "food3")}, [40] * 100)
    simulated = {"cook": [200.0] * 100, "food1": [None] * 100, "food2": [None] * 100, "food3": [None] * 100}
    for i in range(20, 40):
        simulated["cook"][i] = 240.0
    whole = score(track, simulated, fan=[40] * 100, after_plug_s=0.0)
    part = score(track, simulated, fan=[40] * 100, after_plug_s=0.0, exclude=[(100.0, 200.0)])
    assert whole["probes"]["cook"]["max_abs"] == 40.0 and part["probes"]["cook"]["max_abs"] == 0.0
    assert (part["probes"]["cook"]["n"], part["excluded"]) == (80, 20)
    assert part["fan_exact"] == 1.0


def test_a_lid_that_came_off_shows_as_error_until_the_pit_settles_again(tmp_path):
    """The recording cannot say when the lid came off, and the model cannot
    know: the replay is wrong there, and for as long as the pit takes to
    find its level again — and that is what `--skip` is for."""
    sim = Simulator(
        Cooker(Plant(TRUTH, t_amb=AIR_F), ()),
        [Probe(), Probe(plugged=False), Probe(plugged=False), Probe(plugged=False)],
        CyberQDevice(DeviceSettings(cook_set_f=250.0)),
        env=Environment(ambient_f=AIR_F, lid_events=[(2 * 3600.0, 2 * 3600.0 + 180.0)]),
        sample_every=5, seed=0,
    )
    sim.run(3 * 3600.0)
    path = tmp_path / "lid.db"
    record(path, sim)
    track = _read(path)
    played = replay(track, ambient_f=AIR_F, params=TRUTH, open_loop=True, dt=DT)
    whole = _rmse(track, played)
    before = _rmse(track, played, exclude=[(1.9 * 3600.0, 3 * 3600.0)])
    skipped = _rmse(track, played, exclude=[(1.9 * 3600.0, 2.75 * 3600.0)])
    assert whole > 3.0 and before < 0.5 and 0.4 < skipped < whole


# -- as text and CSV ------------------------------------------------------------

def test_format_score_and_the_side_by_side_csv(recording, tmp_path):
    track = _read(recording.db)
    played = replay(track, ambient_f=AIR_F, params=TRUTH, foods=BRISKET, dt=DT)
    scored = score(track, played.simulated, played.fan, exclude=[(0.0, 60.0)])
    text = format_score(track, scored, model="the model")
    lines = text.splitlines()
    assert lines[0].startswith("recorded from 2026-09-21 ") and "2h59m, 2160 samples" in lines[0]
    assert lines[1] == "model: the model"
    assert [line.split()[0] for line in lines[2:6]] == ["probe", "cook", "food1", "food2"]
    assert "RMSE" in lines[2] and lines[3].split()[1] == "2136"  # less the first two minutes and the window
    assert lines[-1].startswith("  fan demand:") and "12 samples left out, as asked" in lines[-2]

    out = write_comparison_csv(tmp_path / "out" / "cook.csv", played)
    with out.open(newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        rows = list(reader)
    assert header == ["ts", "t"] + [f"cook{suffix}" for suffix in ("", "_model")] + [
        f"{probe}{suffix}" for probe in ("food1", "food2", "food3") for suffix in ("", "_model")
    ] + ["output_pct", "output_model"]
    assert len(rows) == 2160
    assert rows[0]["ts"] == str(track.start_ts) and rows[1]["t"] == "5.0"
    assert rows[0]["food2"] == "" and rows[0]["food2_model"] == "" and rows[720]["food2"] != ""
    assert float(rows[5]["cook_model"]) == pytest.approx(played.simulated["cook"][5])
    assert rows[0]["output_pct"] == "100" and rows[0]["output_model"] == ""  # not known before a step
    assert rows[1]["output_model"] == "100"


def test_a_track_that_is_only_spikes_scores_nothing():
    times = [0.0, 5.0, 10.0]
    track = Track(0, times, {"cook": [70.0, 300.0, 71.0], "food1": [None] * 3, "food2": [None] * 3, "food3": [None] * 3},
                  {probe: [225.0] * 3 for probe in ("cook", "food1", "food2", "food3")}, [100] * 3)
    simulated = {"cook": [70.0] * 3, "food1": [None] * 3, "food2": [None] * 3, "food3": [None] * 3}
    scored = score(track, simulated, fan=[100] * 3)
    assert scored["probes"]["cook"] == {"n": 0} and scored["transients"] == 3
    assert scored["fan_exact"] == 1.0 and "fan_rmse" in scored
