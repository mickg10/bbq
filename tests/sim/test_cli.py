"""The command line: ``python -m sim run|serve|compare|fit``, through main()."""

from __future__ import annotations

import csv
import json

import pytest

from sim.cli import _skips, _when, build_parser, main
from sim.device import CyberQDevice, DeviceSettings
from sim.engine import Environment, Simulator
from sim.history import HistoryError
from sim.plant import Cooker, Plant
from sim.probes import Probe
from sim.run import CSV_COLUMNS
from tests.sim.conftest import record

SHIPPED = ["brisket-12h", "cold-windy", "fuel-out", "lid-open", "ribs-6h"]


def _run(capsys) -> str:
    captured = capsys.readouterr()
    assert captured.err == ""
    return captured.out


# -- run ---------------------------------------------------------------------------

def test_run_one_cook_and_write_its_series(tmp_path, capsys):
    out, summary = tmp_path / "runs" / "cook.csv", tmp_path / "summary.json"
    code = main([
        "run", "--scenario", "brisket-12h", "--controller", "pi", "--duration", "20m", "--dt", "5",
        "--set", "propband=15", "--set", "food_set.food1=195", "--out", str(out), "--json", str(summary),
    ])
    assert code == 0
    text = _run(capsys)
    assert text.startswith("brisket-12h · pi · 20m run, setpoint 225 °F\n")
    assert "reached setpoint" in text and "food1:" in text and "cook status:" in text
    assert text.count("wrote ") == 2 and f"wrote {out}" in text and f"wrote {summary}" in text
    with out.open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        assert tuple(reader.fieldnames) == CSV_COLUMNS
    assert len(rows) == 20 and (rows[0]["food1_set"], rows[0]["cook_set"]) == ("195", "225")
    saved = json.loads(summary.read_text())
    assert len(saved) == 1 and (saved[0]["scenario"], saved[0]["controller"]) == ("brisket-12h", "pi")


def test_run_scorecard_for_every_law_on_a_scenario(capsys):
    code = main(["run", "--scenario", "ribs-6h", "--controller", "proportional,bias,pi",
                 "--duration", "30m", "--dt", "5"])
    assert code == 0
    lines = _run(capsys).splitlines()
    assert lines[0].split()[:3] == ["scenario", "controller", "reached"]
    assert [line.split()[1] for line in lines[2:5]] == ["proportional", "bias", "pi"]
    assert all(line.startswith("ribs-6h ") for line in lines[2:5])
    assert lines[5].startswith("reached set: when the pit reading first reached the setpoint")  # the legend
    assert main(["run", "ribs-6h", "--controller", "proportional,pi", "--duration", "30m", "--dt", "5",
                 "--no-legend"]) == 0
    assert len(_run(capsys).splitlines()) == 4


def test_run_with_no_scenario_is_the_whole_scorecard(tmp_path, capsys):
    out_dir, summary = tmp_path / "runs", tmp_path / "s.json"
    code = main(["run", "--duration", "10m", "--dt", "10", "--quiet",
                 "--out-dir", str(out_dir), "--json", str(summary)])
    assert code == 0 and capsys.readouterr().out == ""
    assert sorted(path.name for path in out_dir.iterdir()) == [
        f"{name}-proportional.csv" for name in SHIPPED
    ]
    assert [s["scenario"] for s in json.loads(summary.read_text())] == SHIPPED
    with (out_dir / "lid-open-proportional.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 10 and rows[-1]["t"] == "600"  # ten minutes, a row a minute


def test_run_accepts_a_scenario_file_by_path(tmp_path, capsys):
    copy = tmp_path / "mine.yaml"
    copy.write_text("name: mine\nduration: 5m\ndevice: {cook_set: 200}\n")
    assert main(["run", str(copy), "--dt", "5"]) == 0
    assert _run(capsys).startswith("mine · proportional · 5m run, setpoint 200 °F")


def test_run_refuses_what_it_cannot_do(capsys, tmp_path):
    def fails(arguments: list[str], message: str) -> None:
        assert main(arguments) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and message in captured.err

    fails(["run", "--scenario", "ribs-6h", "--duration", "5m", "--controller", "pid"],
          "unknown controller 'pid'")
    fails(["run", "--duration", "5m", "--out", str(tmp_path / "x.csv")], "--out takes one run")
    fails(["run", "--scenario", "ribs-6h", "--set", "propband=wide"], "is not a number")
    fails(["run", "--scenario", "ribs-6h", "--duration", "12"], "is neither <n><h|m|s> nor HH:MM:SS")
    fails(["run", "--scenario", "ribs-6h", "--duration", "0s"], "duration must be positive")
    fails(["run", "--scenario", "no-such-scenario"], "cannot read")
    fails(["run", "--scenario", "ribs-6h", "--set", "cook_set"], "expected key=value")
    assert sorted(path.name for path in tmp_path.iterdir()) == []


# -- recorded history ----------------------------------------------------------------

def test_compare_a_recording(capsys, recording, tmp_path):
    truth = tmp_path / "cooker.yaml"
    truth.write_text(
        "ambient_f: 68.0\nplant: {c_pit: 60.0, ua_pit: 90.0, q_max: 26000.0, tau_fire: 300.0, draft_min: 0.07}\n"
    )
    out = tmp_path / "side-by-side.csv"
    code = main([
        "compare", "--db", str(recording.db), "--params", str(truth), "--food", "food1=brisket",
        "--open-loop", "--skip", "0h:30m", "--dt", "10", "--csv", str(out),
    ])
    assert code == 0
    text = _run(capsys)
    lines = text.splitlines()
    assert lines[0].startswith("recorded from 2026-09-21 ") and "2160 samples" in lines[0]
    assert lines[1].startswith(
        "model: open loop, the recorded fan output driving the plant; air 68.0 °F; fuel 200k BTU; "
        "c_pit 60, ua_pit 90, q_max 26000, tau_fire 300, draft_min 0.07"
    )
    assert "c_pit 60, ua_pit 90, q_max 26000" in lines[1]
    assert lines[3].split()[:2] == ["cook", "1800"] and float(lines[3].split()[2]) < 0.6  # 2160 less the half hour
    assert lines[4].split()[0] == "food1" and lines[5].split()[0] == "food2"
    assert "(360 samples left out, as asked)" in text and "fan demand" not in text
    assert text.rstrip().endswith(f"wrote {out}")
    with out.open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        assert reader.fieldnames[:4] == ["ts", "t", "cook", "cook_model"]
    assert len(rows) == 2160 and rows[0]["ts"] == "1790000005"

    assert main(["compare", "--db", str(recording.db), "--ambient", "68", "--controller", "pi", "--dt", "10"]) == 0
    text = _run(capsys)
    assert "pi controller on the recorded setpoints; air 68.0 °F" in text
    assert "fan demand:" in text and float(text.splitlines()[3].split()[2]) > 5.0  # default plant: wrong


def test_compare_refuses_what_it_cannot_do(capsys, recording, tmp_path):
    def fails(arguments: list[str], message: str) -> None:
        assert main(arguments) == 2
        assert message in capsys.readouterr().err

    fails(["compare", "--db", str(tmp_path / "missing.db")], "no database at")
    fails(["compare", "--db", str(recording.db), "--from", "yesterday"], "not a time")
    fails(["compare", "--db", str(recording.db), "--food", "food9=brisket"], "expected PROBE=CUT")
    fails(["compare", "--db", str(recording.db), "--food", "food1=turkey"], "unknown food preset")
    fails(["compare", "--db", str(recording.db), "--skip", "5h40m:4h55m"], "ends before it starts")
    fails(["compare", "--db", str(recording.db), "--params", str(tmp_path / "none.yaml")], "cannot read")
    fails(["compare", "--db", str(recording.db), "--controller-option", "ki"], "expected key=value")


def test_fit_from_the_command_line(capsys, recording, tmp_path):
    out = tmp_path / "fit" / "cooker.yaml"
    code = main([
        "fit", "--db", str(recording.db), "--ambient", "68", "--food", "food1=brisket", "--dt", "30",
        "--evaluations", "25", "--skip", "0h:30m", "--out", str(out),
    ])
    assert code == 0
    captured = capsys.readouterr()
    assert "evaluation 20: best pit RMSE so far" in captured.err
    assert "fitting c_pit, q_max, tau_fire, draft_min in open loop" in captured.out
    assert captured.out.rstrip().endswith(f"wrote {out}")
    assert "fitted:\n- c_pit\n" in out.read_text() and "ambient_f: 68.0" in out.read_text()


def test_fit_on_an_idle_recording_says_so(capsys, tmp_path):
    idle = Simulator(
        Cooker(Plant(t_amb=70.0, fuel_btu=0.0), ()),
        [Probe(), Probe(plugged=False), Probe(plugged=False), Probe(plugged=False)],
        CyberQDevice(DeviceSettings()), env=Environment(ambient_f=70.0), sample_every=5,
    )
    idle.run(20 * 60)
    path, out = tmp_path / "idle.db", tmp_path / "cooker.yaml"
    record(path, idle)
    code = main(["fit", "--db", str(path), "--fuel-btu", "0", "--out", str(out)])
    assert code == 0
    text = _run(capsys)
    assert "the pit moved only" in text and "no heat-up to learn from" in text
    assert "Plant parameters left as they were" in text
    written = out.read_text()
    assert "ambient_f: 70.0" in written and "fitted:" not in written and "samples: 240" in written
    assert main(["fit", "--db", str(path), "--fit", "thrust"]) == 2
    assert "not plant parameters: thrust" in capsys.readouterr().err


def test_times_and_windows_are_parsed():
    assert _when("1790000000") == 1790000000
    assert _when("2026-09-21T14:13:20Z") == 1790000000
    assert _when("2026-09-21T16:13:20+02:00") == 1790000000
    assert _when("2026-09-21 14:13:20") == 1790000000  # no zone given: taken as UTC
    with pytest.raises(HistoryError, match="not a time"):
        _when("yesterday")
    assert _skips(["4h55m:5h40m", "0:10m"]) == [(17700.0, 20400.0), (0.0, 600.0)]
    for bad in ("5h", "5h40m:4h55m", "a:b"):
        with pytest.raises(HistoryError):
            _skips([bad])


# -- the parser ----------------------------------------------------------------------

def test_serve_options_are_understood():
    args = build_parser().parse_args([
        "serve", "--scenario", "lid-open", "--set", "opendetect=false", "--speed", "60", "--port", "8123",
    ])
    assert (args.scenario, args.overrides, args.speed, args.port) == (
        "lid-open", ["opendetect=false"], 60.0, 8123,
    )
    assert args.handler.__name__ == "_serve"
    for arguments in ([], ["bake"], ["run", "--scenario"]):
        with pytest.raises(SystemExit) as caught:
            build_parser().parse_args(arguments)
        assert caught.value.code == 2
