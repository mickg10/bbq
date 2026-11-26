"""The scenario loader: times, validation, and the scenarios that ship."""

from __future__ import annotations

import pathlib

import pytest
import yaml

from sim.device import TimeoutAction
from sim.engine import Event
from sim.plant import BTU_PER_LB_LUMP
from sim.scenario import (
    SCENARIOS_DIR,
    ScenarioError,
    apply_device_fields,
    format_time,
    list_scenarios,
    load_scenario,
    parse_device_fields,
    parse_overrides,
    parse_scenario,
    parse_time,
)

SHIPPED = ("brisket-12h", "cold-windy", "fuel-out", "lid-open", "ribs-6h")

#: a scenario that uses every section there is
FULL = """
name: full
description: Everything a scenario can say.
duration: 14h
seed: 3
controller: pi
controller_options: {ki: 0.2}
ambient: [[0, 70], [12h, 55]]
wind_mph: [[0, 0], [6h, 12]]
cooker:
  fuel_lb: 15
  tau_fire: 300
  fuel_tail: 0.3
start: {pit_f: 200}
device:
  cook_set: 225
  food_set: {food1: 203, food2: 180}
  cookhold: 180
  alarmdev: 25
  propband: 20
  cyctime: 8
  ramp: 1
  opendetect: false
  timeout_action: hold
  timer: 12h
names: {cook: Pit, food1: Brisket}
food:
  food1: {cut: brisket, mass_lb: 14, temp_f: 38}
  food2: {cut: air}
lid_open: [[5h, 5h03m]]
events:
  - {at: 1h, control: {propband: 15, opendetect: true}}
  - {at: 2h, timer: 06:00:00}
  - {at: 3h, cook_set: 250}
  - {at: 3h05m, food_set: {food1: 205}}
  - {at: 4h, unplug: food2}
  - {at: 4h10m, plug: food2}
"""


def _full(**changes) -> dict:
    doc = yaml.safe_load(FULL)
    for section, values in changes.items():
        if values is None:
            doc.pop(section, None)
        elif isinstance(values, dict):
            doc.setdefault(section, {}).update(values)
        else:
            doc[section] = values
    return doc


def _message(document, fragment: str) -> str:
    with pytest.raises(ScenarioError, match=fragment):
        parse_scenario(document)
    return ""


# -- times ---------------------------------------------------------------------

def test_times_carry_a_unit():
    assert parse_time("90s") == 90
    assert parse_time("5m") == 300
    assert parse_time("1h") == 3600
    assert parse_time("1h30m") == 5400
    assert parse_time("1.5h") == 5400
    assert parse_time("1 h 30 m") == 5400
    assert parse_time("02:00:00") == 7200
    assert parse_time(0) == 0.0  # the start of a schedule needs no unit


def test_a_bare_number_is_refused_as_a_time():
    """It is too easy to write 12 meaning hours and mean minutes."""
    for bad in (12, 12.5, "12", "five", "1x", "h", None, True):
        with pytest.raises(ScenarioError) as caught:
            parse_time(bad, "duration")
        assert "duration" in str(caught.value)


def test_format_time():
    assert format_time(None) == "–"
    assert format_time(0) == "0s"
    assert format_time(12.5) == "12.5s"
    assert format_time(90) == "1m30s"
    assert format_time(2700) == "45m"
    assert format_time(2730) == "45m30s"
    assert format_time(3660) == "1h01m"
    assert format_time(14 * 3600) == "14h"


# -- a whole scenario ----------------------------------------------------------

def test_a_scenario_can_say_everything():
    s = parse_scenario(yaml.safe_load(FULL), source=pathlib.Path("full.yaml"))
    assert (s.name, s.description) == ("full", "Everything a scenario can say.")
    assert (s.duration, s.seed, s.controller, s.controller_options) == (50400.0, 3, "pi", {"ki": 0.2})
    assert (s.plant.tau_fire, s.plant.fuel_tail, s.plant.c_pit) == (300.0, 0.3, 80.0)
    assert s.fuel_btu == pytest.approx(15 * BTU_PER_LB_LUMP)
    assert s.ambient == [(0.0, 70.0), (43200.0, 55.0)]
    assert s.wind_mph == [(0.0, 0.0), (21600.0, 12.0)]
    assert s.lid_open == ((18000.0, 18180.0),)
    assert s.start_pit_f == 200.0
    assert s.device == {
        "cook_set_f": 225.0, "food_set_f": {"food1": 203.0, "food2": 180.0}, "cookhold_f": 180.0,
        "alarmdev_f": 25.0, "propband_f": 20.0, "cyctime_s": 8, "ramp": 1, "opendetect": False,
        "timeout_action": TimeoutAction.HOLD, "timer": 43200.0,
    }
    assert s.names == {"cook": "Pit", "food1": "Brisket"}
    assert (s.foods["food1"].cut, s.foods["food1"].params.mass_lb, s.foods["food1"].temp_f) == ("brisket", 14.0, 38.0)
    assert s.foods["food2"].params.mass_lb == 0.0
    assert "food3" not in s.foods
    assert [event.t for event in s.events] == sorted(event.t for event in s.events)
    assert len(s.events) == 6


def test_a_scenario_is_mostly_optional():
    s = parse_scenario({"device": {"cook_set": 225}})
    assert (s.name, s.duration, s.seed, s.controller) == ("untitled", 43200.0, 0, "proportional")
    assert s.plant == parse_scenario({}).plant and s.fuel_btu == 200_000.0
    assert (s.ambient, s.wind_mph, s.lid_open, s.start_pit_f) == (70.0, 0.0, (), None)
    assert s.device == {"cook_set_f": 225.0} and s.foods == {} and s.events == ()
    _message({"name": "  "}, "scenario.name: expected a name")  # blank is a mistake, not absent


def test_an_unknown_field_names_the_nearest_known_one():
    _message(_full(cooker={"c_pt": 70}), r"c_pt' \(did you mean 'c_pit'\?\)")
    _message({"duraton": "1h"}, r"duraton' \(did you mean 'duration'\?\)")
    _message({"device": {"prop_band": 5}}, "unknown field 'prop_band'")
    _message({"food": {"food4": {}}}, "unknown field 'food4'")
    _message({"events": [{"at": "1h", "cook_st": 250}]}, "unknown field 'cook_st'")
    _message({"nonsense": 1}, "unknown field 'nonsense'")


def test_fuel_is_pounds_or_btu():
    assert parse_scenario({"cooker": {"fuel_btu": 100000}}).fuel_btu == 100000.0
    assert parse_scenario({"cooker": {"fuel_lb": 0}}).fuel_btu == 0.0
    _message(_full(cooker={"fuel_btu": 1}), "fuel_lb or fuel_btu, not both")
    _message({"cooker": {"c_pit": -1}}, "cooker")


def test_a_controller_and_its_options_are_checked():
    _message({"controller": "pid"}, "expected one of")
    _message({"controller": "pi", "controller_options": {"nonsense": 1}}, "controller 'pi'")


def test_food_cuts_and_what_is_on_them():
    s = parse_scenario({"food": {"food1": {"mass_lb": 9, "temp_f": 33.5}, "food2": None}})
    assert set(s.foods) == {"food1"}
    assert (s.foods["food1"].cut, s.foods["food1"].params.mass_lb, s.foods["food1"].temp_f) == ("no-stall", 9.0, 33.5)
    _message({"food": {"food1": {"cut": "turkey"}}}, "expected one of")
    _message({"food": {"food1": {"weigh": 9}}}, "unknown field 'weigh'")
    _message({"food": {"food1": "brisket"}}, "expected a mapping, got str")


def test_events_need_a_time_and_exactly_one_action():
    _message({"events": [{"cook_set": 250}]}, "needs 'at' and exactly one")
    _message({"events": [{"at": "1h"}]}, "needs 'at' and exactly one")
    _message({"events": [{"at": "1h", "plug": "food1", "timer": "01:00:00"}]}, "exactly one")
    _message({"events": [{"at": "1h", "plug": "food9"}]}, "expected one of")
    _message({"events": [{"at": "1h", "control": {"cook_set": 250}}]}, "unknown field 'cook_set'")


def test_a_timeout_action_by_name_or_by_control_htm_s_number():
    fields = parse_device_fields({"timeout_action": "shutdown"})
    assert fields == {"timeout_action": TimeoutAction.SHUTDOWN}
    assert parse_device_fields({"timeout_action": 3}) == fields
    _message({"device": {"timeout_action": "off"}}, "expected one of none, hold, alarm, shutdown")
    _message({"device": {"timeout_action": 4}}, "outside 0…3")


def test_lid_and_names_are_checked():
    _message({"lid_open": [[300]]}, "expected \\[open_at, close_at\\]")
    _message({"lid_open": [["1h", "30m"]]}, "closes before it opens")
    _message({"names": {"food1": "a name that is far too long for the box"}}, "1–16 characters")
    _message({"names": {"pit": "Pit"}}, "unknown field 'pit'")


def test_schedules_need_increasing_times():
    assert parse_scenario({"ambient": [[0, 70], ["1m", 80]]}).ambient == [(0.0, 70.0), (60.0, 80.0)]
    _message({"ambient": [[0, 70], [0, 71]]}, "strictly increasing")
    _message({"ambient": [["2m", 70], ["1m", 80]]}, "strictly increasing")
    _message({"ambient": [[0, 70], [60, 80]]}, "60 has no unit")
    _message({"wind_mph": [[0]]}, "expected \\[time, value\\]")


# -- device settings as a scenario writes them ----------------------------------

def test_parse_device_fields():
    fields = parse_device_fields({
        "cook_set": 250, "food_set": {"food1": 203.5}, "cookhold": 180, "alarmdev": 25,
        "propband": 20, "cyctime": 8.0, "ramp": 2, "opendetect": False, "timeout_action": 1,
        "timer": "01:30:00",
    })
    assert fields == {
        "cook_set_f": 250.0, "food_set_f": {"food1": 203.5}, "cookhold_f": 180.0, "alarmdev_f": 25.0,
        "propband_f": 20.0, "cyctime_s": 8, "ramp": 2, "opendetect": False,
        "timeout_action": TimeoutAction.HOLD, "timer": 5400.0,
    }
    with pytest.raises(ScenarioError, match="expected true or false"):
        parse_device_fields({"opendetect": "yes please"})
    with pytest.raises(ScenarioError, match="unknown field 'cook_set'"):
        parse_device_fields({"cook_set": 225}, "events[0].control", allowed=("propband",))


def test_overrides_from_the_command_line_are_typed():
    assert parse_overrides(["cook_set=250", "propband=12.5", "cyctime=8", "opendetect=false",
                            "food_set.food1=190", "timeout_action=alarm", "ramp=0"]) == {
        "cook_set_f": 250, "propband_f": 12.5, "cyctime_s": 8, "opendetect": False,
        "food_set_f": {"food1": 190}, "timeout_action": TimeoutAction.ALARM, "ramp": 0,
    }
    for bad in ("cook_set", "=250", "cook_set=wide", "opendetect=maybe", "timeout_action=9"):
        with pytest.raises(ScenarioError):
            parse_overrides([bad])


def test_device_fields_reach_a_running_simulator(make_sim):
    sim = make_sim(preheat=215.0)
    apply_device_fields(sim, {
        "cook_set_f": 250.0, "food_set_f": {"food2": 190.0}, "cookhold_f": 175.0, "alarmdev_f": 20.0,
        "propband_f": 10.0, "cyctime_s": 8, "ramp": 3, "opendetect": False,
        "timeout_action": TimeoutAction.SHUTDOWN, "timer": 600.0,
    })
    s = sim.device.settings
    assert (s.cook_set_f, s.food_set_f, s.cookhold_f, s.alarmdev_f) == (250.0, [203.0, 190.0, 180.0], 175.0, 20.0)
    assert (s.propband_f, s.cyctime_s, s.ramp, s.opendetect) == (10.0, 8, 3, False)
    assert s.timeout_action is TimeoutAction.SHUTDOWN
    assert (sim.device.timer.text, sim.device.timer.status) == ("00:10:00", 1)


# -- files ------------------------------------------------------------------------

def test_the_shipped_scenarios():
    assert [path.stem for path in list_scenarios()] == list(SHIPPED)
    assert list_scenarios()[0].parent == SCENARIOS_DIR
    for path in list_scenarios():
        s = load_scenario(path)
        assert s.name == path.stem and s.description, path
        assert s.duration > 0
        sim = s.build(max_rows=10)  # every one of them builds
        assert sim.device.controller.name == s.controller


def test_loading_by_path_names_a_scenario_from_its_file(tmp_path):
    by_path = load_scenario(SCENARIOS_DIR / "ribs-6h.yaml")
    assert by_path.source == SCENARIOS_DIR / "ribs-6h.yaml"
    assert by_path.name == "ribs-6h"                       # the name in the file…
    copied = tmp_path / "copied.yaml"
    copied.write_text("duration: 2h\n")                    # …or the file's, when there is none
    assert load_scenario(copied).name == "copied"


def test_loading_applies_overrides_on_top(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("name: s\nduration: 2h\ncontroller: pi\ndevice: {cook_set: 225, propband: 30}\n")
    s = load_scenario(path, duration=3600.0, controller="bias", seed=7, device={"propband_f": 10.0})
    assert (s.duration, s.controller, s.seed) == (3600.0, "bias", 7)
    assert s.controller_options == {}
    assert s.device == {"cook_set_f": 225.0, "propband_f": 10.0}


def test_loading_reports_what_is_wrong_with_a_file(tmp_path):
    with pytest.raises(ScenarioError, match="cannot read"):
        load_scenario(tmp_path / "missing.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: [unclosed\n")
    with pytest.raises(ScenarioError, match="broken.yaml: not valid YAML"):
        load_scenario(broken)
    empty = tmp_path / "empty.yaml"
    empty.write_text("- just\n- a list\n")
    with pytest.raises(ScenarioError, match="empty.yaml: scenario: expected a mapping, got list"):
        load_scenario(empty)
    with pytest.raises(ScenarioError, match="s.yaml: scenario 's': duration"):
        load_scenario(_write(tmp_path, "name: s\nduration: 12\n"))


def _write(tmp_path, text):
    path = tmp_path / "s.yaml"
    path.write_text(text)
    return path


# -- from scenario to simulator ----------------------------------------------------

def test_build_wires_everything_together():
    s = parse_scenario(yaml.safe_load(FULL))
    sim = s.build()
    assert [probe.plugged for probe in sim.probes] == [True, True, True, False]
    assert sim.cooker.foods[0].params.mass_lb == 14.0 and sim.cooker.foods[1].params.mass_lb == 0.0
    assert sim.cooker.foods[2] is None
    plant = sim.cooker.plant
    assert plant.pit_f == 200.0 and plant.params.tau_fire == 300.0
    assert plant.fuel_btu == pytest.approx(15 * BTU_PER_LB_LUMP)
    assert sim.device.settings.ramp == 1 and sim.device.settings.opendetect is False
    assert sim.device.controller.name == "pi" and sim.device.controller.ki == 0.2
    assert (sim.device.timer.text, sim.device.timer.status) == ("12:00:00", 1)
    assert sim.env.ambient_at(6 * 3600) == 62.5 and sim.env.wind_at(0) == 0.0
    assert sim.env.lid_open_at(5 * 3600) and not sim.env.lid_open_at(6 * 3600)
    assert sim.seed == 3 and [probe.reading is not None for probe in sim.probes[:3]] == [True] * 3


def test_build_starts_where_told_and_with_the_probes_plugged():
    s = parse_scenario({"start": {"pit_f": 225.0}, "food": {"food2": {"cut": "ribs", "temp_f": 100}}})
    sim = s.build()
    assert sim.cooker.pit_f == 225.0 and sim.cooker.plant.fire == pytest.approx(0.335, abs=0.01)
    assert [probe.plugged for probe in sim.probes] == [True, False, True, False]
    assert sim.probes[2].tracked == 100.0
    assert sim.probes[0].tracked == 225.0


def test_events_fire_in_file_order_when_they_share_a_time(make_sim):
    order = []
    events = [
        Event(10.0, lambda sim: order.append("timer"), "timer"),
        Event(10.0, lambda sim: order.append("control"), "control"),
        Event(10.0, lambda sim: order.append("unplug"), "unplug"),
    ]
    make_sim(events=events).run(11)
    assert order == ["timer", "control", "unplug"]


def test_scenario_events_change_the_cook_as_they_say():
    s = parse_scenario({
        "duration": "2h", "start": {"pit_f": 215.0},
        "device": {"cook_set": 225, "food_set": {"food1": 150}, "propband": 30},
        "food": {"food1": {"cut": "air", "temp_f": 215}},
        "events": [
            {"at": "30m", "control": {"propband": 10}},
            {"at": "30m", "cook_set": 250},
            {"at": "45m", "timer": "02:00:00"},
            {"at": "50m", "unplug": "food1"},
            {"at": "1h", "plug": "food1"},
        ],
    })
    sim = s.build(sample_every=600)
    sim.run(s.duration)
    settings = sim.device.settings
    assert (settings.cook_set_f, settings.propband_f) == (250.0, 10.0)
    assert sim.device.timer.text == "00:45:00"  # two hours set at 45 min; 75 of them gone
    by_t = {row["t"]: row for row in sim.rows}
    assert by_t[3000.0]["food1"] is not None and by_t[3600.0]["food1"] is None  # out from 50 to 60 min
    assert by_t[4200.0]["food1"] is not None
