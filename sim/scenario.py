"""Scenario loader (docs/SIMULATOR.md): a YAML cook, turned into a Simulator.

A scenario says what is in the cooker and what happens to it::

    name: brisket-12h
    description: A 12 lb brisket at 225 °F from a cold start.
    duration: 14h
    seed: 0
    controller: proportional        # proportional | bias | pi | firmware
    ambient: 70                     # °F, or a schedule: [[0, 70], [12h, 55]]
    wind_mph: 0                     # or a schedule
    cooker:
      fuel_lb: 15                   # lump charcoal (fuel_btu also accepted)
      c_pit: 80                     # any PlantParams field; all optional
    start: {pit_f: 225}             # begin mid-cook; default is cold
    device:                         # starting settings; anything left out is the default
      cook_set: 225
      food_set: {food1: 203}
      cookhold: 180
      propband: 30
      timer: 12h
    names: {cook: Pit, food1: Brisket}
    food:
      food1: {cut: brisket, mass_lb: 12, temp_f: 40}   # a FoodParams preset + overrides
    lid_open: [[5h, 5h03m]]         # when the lid comes off, and goes back on
    events:
      - {at: 1h, control: {propband: 20}}
      - {at: 2h, timer: 06:00:00}
      - {at: 3h, unplug: food2}
      - {at: 3h10m, plug: food2}

Temperatures are °F. Times carry a unit — ``90s``, ``5m``, ``1h30m`` — or
are ``HH:MM:SS``; a bare number is refused, as it is too easy to write 12
meaning hours and mean minutes. A food probe with no entry under ``food`` is
unplugged; ``{cut: air}`` is a probe plugged in with nothing on it.

Unknown fields, in any section, are an error with the nearest known name
suggested, so a typo cannot quietly become a default.
"""

from __future__ import annotations

import dataclasses
import difflib
import pathlib
import re
from typing import Any, Sequence

import yaml

from sim.controllers import CONTROLLERS, get_controller
from sim.device import CyberQDevice, DeviceSettings, TimeoutAction, parse_hhmmss
from sim.engine import Environment, Event, Simulator
from sim.food import PRESETS, Food, FoodParams, preset
from sim.plant import BTU_PER_LB_LUMP, Cooker, Plant, PlantParams
from sim.probes import Probe

PROBES = ("cook", "food1", "food2", "food3")
FOOD_PROBES = PROBES[1:]

#: The scenarios that ship with the simulator.
SCENARIOS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scenarios"

DEFAULT_DURATION_S = 12 * 3600.0

_TIMEOUT_ACTIONS = {
    "none": TimeoutAction.NONE, "hold": TimeoutAction.HOLD,
    "alarm": TimeoutAction.ALARM, "shutdown": TimeoutAction.SHUTDOWN,
}
_PLANT_FIELDS = tuple(f.name for f in dataclasses.fields(PlantParams))
#: FoodParams fields a scenario may override on a cut.
_FOOD_FIELDS = tuple(f.name for f in dataclasses.fields(FoodParams))


class ScenarioError(ValueError):
    """A scenario file that does not parse, or does not make sense."""


# -- validation helpers --------------------------------------------------------

_TIME_PART = re.compile(r"(\d+(?:\.\d+)?)(h|m|s)")


def parse_time(value: Any, where: str = "time") -> float:
    """``"90s"``, ``"5m"``, ``"1h30m"``, ``"02:00:00"`` -> seconds.

    A bare number is refused, as it is too easy to write 12 meaning hours and
    mean minutes — except ``0``, which can only mean the start.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ScenarioError(f"{where}: not a time: {value!r}")
    if isinstance(value, (int, float)):
        if value == 0:
            return 0.0
        raise ScenarioError(f"{where}: {value!r} has no unit — write 90s, 5m or 1.5h")
    text = re.sub(r"\s+", "", value).lower()
    if text == "0":
        return 0.0  # the start of a window, as on the command line
    if ":" in text:
        try:
            return float(parse_hhmmss(text))
        except ValueError as exc:
            raise ScenarioError(
                f"{where}: {value!r} is neither <n><h|m|s> nor HH:MM:SS"
            ) from exc
    parts = _TIME_PART.findall(text)
    if not parts or "".join(number + unit for number, unit in parts) != text:
        raise ScenarioError(f"{where}: {value!r} is neither <n><h|m|s> nor HH:MM:SS")
    return sum(float(number) * {"h": 3600.0, "m": 60.0, "s": 1.0}[unit] for number, unit in parts)


def format_time(seconds: float | None) -> str:
    """Seconds -> ``"14h"``/``"1h30m"``/``"45m"``/``"90s"``/``"12.5s"``; None -> ``"–"``."""
    if seconds is None:
        return "–"
    if seconds <= 0:
        return "0s"
    hours, rest = divmod(seconds, 3600.0)
    minutes, secs = divmod(rest, 60.0)
    if hours:
        return f"{int(hours)}h" + (f"{int(minutes):02d}m" if minutes else "")
    if minutes:
        return f"{int(minutes)}m" + (f"{int(secs):02d}s" if secs else "")
    return f"{secs:g}s"


def _kind(value: Any) -> str:
    return "nothing" if value is None else type(value).__name__


def _mapping(value: Any, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScenarioError(f"{where}: expected a mapping, got {_kind(value)}")
    return value


def _only(mapping: dict, allowed: Sequence[str], where: str) -> None:
    for key in mapping:
        if key not in allowed:
            near = difflib.get_close_matches(str(key), list(allowed), n=1)
            hint = f" (did you mean {near[0]!r}?)" if near else ""
            raise ScenarioError(
                f"{where}: unknown field {key!r}{hint}; known fields: {', '.join(sorted(allowed))}"
            )


def _number(value: Any, where: str, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioError(f"{where}: expected a number, got {_kind(value)}")
    number = float(value)
    if (low is not None and number < low) or (high is not None and number > high):
        raise ScenarioError(f"{where}: {number:g} is outside {low}…{high}")
    return number


def _choice(value: Any, where: str, allowed: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ScenarioError(f"{where}: expected one of {', '.join(allowed)}, got {value!r}")
    return value


def _schedule(value: Any, where: str) -> float | list[tuple[float, float]]:
    """A constant, or ``[[time, value], ...]`` as (seconds, value) pairs."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, list) or not value:
        raise ScenarioError(f"{where}: expected a number or a list of [time, value] pairs")
    points = []
    for i, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ScenarioError(f"{where}[{i}]: expected [time, value]")
        points.append((
            parse_time(point[0], f"{where}[{i}] time"),
            _number(point[1], f"{where}[{i}] value"),
        ))
    times = [t for t, _ in points]
    if times != sorted(times) or len(set(times)) != len(times):
        raise ScenarioError(f"{where}: times must be strictly increasing")
    return points


# -- device settings, as a scenario names them ---------------------------------

#: scalar settings: scenario name -> (what the device is told, how it is written)
_DEVICE_SCALARS: dict[str, tuple[str, str]] = {
    "cook_set": ("cook_set_f", "temp"),
    "cookhold": ("cookhold_f", "temp"),
    "alarmdev": ("alarmdev_f", "temp"),
    "propband": ("propband_f", "temp"),
    "cyctime": ("cyctime_s", "int"),
    "ramp": ("ramp", "int"),
    "opendetect": ("opendetect", "bool"),
    "timeout_action": ("timeout_action", "action"),
}
#: what the ``device`` section, an event and ``--set`` may set
DEVICE_FIELDS = ("cook_set", "food_set", "timer", *_DEVICE_SCALARS)
#: the control-loop settings only (control.htm's)
CONTROL_FIELDS = tuple(k for k in _DEVICE_SCALARS if k != "cook_set")


def parse_device_fields(mapping: Any, where: str = "device", *, allowed: Sequence[str] = DEVICE_FIELDS) -> dict:
    """Scenario-style settings -> fields for :func:`apply_device_fields`.

    ``{"cook_set": 225, "food_set": {"food1": 203}, "propband": 20}`` becomes
    ``{"cook_set_f": 225.0, "food_set_f": {"food1": 203.0}, "propband_f": 20.0}``
    and a timer becomes ``{"timer": 43200.0}`` (seconds). Only the fields
    given come back; the device clamps whatever it is handed.
    """
    mapping = _mapping(mapping, where)
    _only(mapping, allowed, where)
    fields: dict = {}
    for key, value in mapping.items():
        at = f"{where}.{key}"
        if key == "food_set":
            sets = _mapping(value, at)
            _only(sets, FOOD_PROBES, at)
            fields["food_set_f"] = {probe: _number(sets[probe], f"{at}.{probe}") for probe in sets}
        elif key == "timer":
            fields["timer"] = parse_time(value, at)
        elif key == "cook_set":
            fields["cook_set_f"] = _number(value, at)
        else:
            attribute, kind = _DEVICE_SCALARS[key]
            if kind == "temp":
                fields[attribute] = _number(value, at)
            elif kind == "int":
                fields[attribute] = int(_number(value, at))
            elif kind == "bool":
                if not isinstance(value, bool):
                    raise ScenarioError(f"{at}: expected true or false, got {value!r}")
                fields[attribute] = value
            else:  # a timeout action: by name, or control.htm's number
                named = value.lower() in _TIMEOUT_ACTIONS if isinstance(value, str) else False
                if not named and not isinstance(value, (int, float)):
                    raise ScenarioError(
                        f"{at}: expected one of {', '.join(_TIMEOUT_ACTIONS)}, or a number 0–3, got {value!r}"
                    )
                fields[attribute] = (
                    _TIMEOUT_ACTIONS[value.lower()] if named
                    else TimeoutAction(int(_number(value, at, 0, 3)))
                )
    return fields


def apply_device_fields(sim: Simulator, fields: dict) -> None:
    """Hand :func:`parse_device_fields` output to a running simulator's device."""
    device = sim.device
    if "cook_set_f" in fields:
        device.set_cook_set_tenths(round(fields["cook_set_f"] * 10))
    for probe, value in fields.get("food_set_f", {}).items():
        device.set_food_set_tenths(FOOD_PROBES.index(probe), round(value * 10))
    if "cookhold_f" in fields:
        device.set_cookhold_tenths(round(fields["cookhold_f"] * 10))
    if "alarmdev_f" in fields:
        device.set_alarmdev_tenths(round(fields["alarmdev_f"] * 10))
    if "propband_f" in fields:
        device.set_propband_tenths(round(fields["propband_f"] * 10))
    if "cyctime_s" in fields:
        device.set_cyctime_s(fields["cyctime_s"])
    if "ramp" in fields:
        device.set_ramp(fields["ramp"])
    if "opendetect" in fields:
        device.set_opendetect(fields["opendetect"])
    if "timeout_action" in fields:
        device.set_timeout_action(int(fields["timeout_action"]))
    if "timer" in fields:
        device.set_timer_s(fields["timer"])


_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


def _from_text(text: str, key: str) -> Any:
    """One ``--set`` value, typed the way its field is written in a scenario."""
    if key == "timer":
        return text
    if key == "opendetect":
        if text.lower() not in _TRUE + _FALSE:
            raise ScenarioError(f"--set {key}: expected true or false, got {text!r}")
        return text.lower() in _TRUE
    if key == "timeout_action" and text.lower() in _TIMEOUT_ACTIONS:
        return text.lower()
    try:
        number = float(text)
    except ValueError as exc:
        raise ScenarioError(f"--set {key}: {text!r} is not a number") from exc
    return int(number) if number.is_integer() else number


def parse_overrides(items: Sequence[str]) -> dict:
    """``--set`` entries (``key=value``, ``food_set.food1=190``) ->
    :func:`parse_device_fields` output."""
    typed: dict = {}
    for item in items:
        key, sep, text = item.partition("=")
        key, text = key.strip(), text.strip()
        if not sep or not key or not text:
            raise ScenarioError(f"--set {item!r}: expected key=value")
        if key.startswith("food_set."):
            typed.setdefault("food_set", {})[key[len("food_set."):]] = _from_text(text, key)
        else:
            typed[key] = _from_text(text, key)
    return parse_device_fields(typed, "--set")


# -- the scenario ---------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class FoodSpec:
    """A piece of food on a probe: a preset cut with overrides, at a temperature."""

    cut: str
    temp_f: float
    params: FoodParams


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    description: str = ""
    duration: float = DEFAULT_DURATION_S
    seed: int = 0
    controller: str = "proportional"
    controller_options: dict = dataclasses.field(default_factory=dict)
    plant: PlantParams = dataclasses.field(default_factory=PlantParams)
    fuel_btu: float = 200_000.0
    ambient: float | list = 70.0
    wind_mph: float | list = 0.0
    lid_open: tuple = ()
    start_pit_f: float | None = None
    device: dict = dataclasses.field(default_factory=dict)  # parse_device_fields output
    names: dict = dataclasses.field(default_factory=dict)   # probe -> name on the wire
    foods: dict = dataclasses.field(default_factory=dict)   # probe -> FoodSpec
    events: tuple = ()
    source: pathlib.Path | None = None

    @property
    def ambient_at_start(self) -> float:
        """The air temperature at t = 0 (the first point of a schedule)."""
        return self.ambient if isinstance(self.ambient, float) else self.ambient[0][1]

    def build(self, *, sample_every: int = 1, dt: float = 1.0, max_rows: int | None = None) -> Simulator:
        """A simulator set up for this scenario, before its first step."""
        controller = get_controller(self.controller, **self.controller_options)
        foods = tuple(
            Food(self.foods[probe].params, temp_f=self.foods[probe].temp_f)
            if probe in self.foods else None
            for probe in FOOD_PROBES
        )
        cooker = Cooker(
            Plant(self.plant, t_amb=self.ambient_at_start, fuel_btu=self.fuel_btu), foods,
        )
        probes = [Probe(plugged=True)] + [Probe(plugged=probe in self.foods) for probe in FOOD_PROBES]
        settings = dict(self.device)
        food_sets = settings.pop("food_set_f", {})
        settings["food_set_f"] = [food_sets.get(probe, 180.0) for probe in FOOD_PROBES]
        timer = settings.pop("timer", None)
        device = CyberQDevice(DeviceSettings(**settings), controller)
        if timer:
            device.set_timer_s(timer)
        sim = Simulator(
            cooker, probes, device,
            env=Environment(ambient_f=self.ambient, wind_mph=self.wind_mph, lid_events=self.lid_open),
            dt=dt, sample_every=sample_every, events=self.events, seed=self.seed, max_rows=max_rows,
        )
        if self.start_pit_f is not None:
            sim.preheat(self.start_pit_f)
        return sim


def list_scenarios(directory: pathlib.Path | str = SCENARIOS_DIR) -> list[pathlib.Path]:
    """Every ``*.yaml`` in the scenarios directory, sorted by name."""
    return sorted(pathlib.Path(directory).glob("*.yaml"))


def scenario_path(name: pathlib.Path | str) -> pathlib.Path:
    """A scenario's file: by path, or by the name of one that ships
    (``brisket-12h``, with or without ``.yaml``)."""
    path = pathlib.Path(name)
    if not path.suffix:
        path = path.with_suffix(".yaml")
    if not path.exists():
        looked = SCENARIOS_DIR / path.name
        if looked.exists():
            path = looked
    return path


def load_scenario(path: pathlib.Path | str, **overrides) -> Scenario:
    """Read and validate a scenario file.

    ``overrides`` replace same-named attributes of the loaded scenario —
    ``duration``, ``seed``, ``controller``, ``controller_options``, or
    ``device`` (:func:`parse_device_fields` output, merged over the file's).
    """
    path = pathlib.Path(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise ScenarioError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: not valid YAML: {exc}") from exc
    try:
        scenario = parse_scenario(doc, source=path)
    except ScenarioError as exc:
        raise ScenarioError(f"{path}: {exc}") from None
    if "device" in overrides:
        overrides = {**overrides, "device": {**scenario.device, **overrides["device"]}}
    if not overrides:
        return scenario
    if "duration" in overrides and overrides["duration"] <= 0:
        raise ScenarioError(f"{path}: duration must be positive")
    return dataclasses.replace(scenario, **overrides)


_TOP_LEVEL = (
    "name", "description", "duration", "seed", "controller", "controller_options",
    "ambient", "wind_mph", "cooker", "start", "device", "names", "food", "lid_open", "events",
)
_EVENT_ACTIONS = ("cook_set", "food_set", "control", "timer", "plug", "unplug")


def parse_scenario(doc: Any, *, source: pathlib.Path | None = None) -> Scenario:
    """Validate a scenario document (as loaded from YAML)."""
    doc = _mapping(doc, "scenario")
    _only(doc, _TOP_LEVEL, "scenario")
    name = doc.get("name") or (source.stem if source is not None else "untitled")
    if not isinstance(name, str) or not name.strip():
        raise ScenarioError("scenario.name: expected a name")
    description = doc.get("description", "")
    if not isinstance(description, str):
        raise ScenarioError("scenario.description: expected text")
    where = f"scenario {name.strip()!r}"

    duration = parse_time(doc.get("duration", "12h"), f"{where}: duration")
    if duration <= 0:
        raise ScenarioError(f"{where}: duration must be positive")
    seed = int(_number(doc.get("seed", 0), f"{where}: seed", 0))
    controller = _choice(doc.get("controller", "proportional"), f"{where}: controller", sorted(CONTROLLERS))
    controller_options = dict(_mapping(doc.get("controller_options"), f"{where}: controller_options"))
    try:
        get_controller(controller, **controller_options)
    except (TypeError, ValueError) as exc:
        raise ScenarioError(f"{where}: controller {controller!r}: {exc}") from exc

    cooker = _mapping(doc.get("cooker"), f"{where}: cooker")
    _only(cooker, ("fuel_lb", "fuel_btu", *_PLANT_FIELDS), f"{where}: cooker")
    if "fuel_lb" in cooker and "fuel_btu" in cooker:
        raise ScenarioError(f"{where}: cooker: give fuel_lb or fuel_btu, not both")
    fuel_btu = (
        _number(cooker["fuel_btu"], f"{where}: cooker.fuel_btu", 0) if "fuel_btu" in cooker
        else _number(cooker.get("fuel_lb", 200_000.0 / BTU_PER_LB_LUMP), f"{where}: cooker.fuel_lb", 0)
        * BTU_PER_LB_LUMP
    )
    try:
        plant = PlantParams(**{key: cooker[key] for key in cooker if key in _PLANT_FIELDS})
    except (TypeError, ValueError) as exc:
        raise ScenarioError(f"{where}: cooker: {exc}") from exc

    lid = []
    for i, interval in enumerate(doc.get("lid_open") or []):
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            raise ScenarioError(f"{where}: lid_open[{i}]: expected [open_at, close_at]")
        opened = parse_time(interval[0], f"{where}: lid_open[{i}] open")
        closed = parse_time(interval[1], f"{where}: lid_open[{i}] close")
        if closed <= opened:
            raise ScenarioError(f"{where}: lid_open[{i}]: closes before it opens")
        lid.append((opened, closed))

    start = _mapping(doc.get("start"), f"{where}: start")
    _only(start, ("pit_f",), f"{where}: start")
    start_pit = _number(start["pit_f"], f"{where}: start.pit_f", 0, 1000) if "pit_f" in start else None

    names = _mapping(doc.get("names"), f"{where}: names")
    _only(names, PROBES, f"{where}: names")
    for probe, value in names.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 16:
            raise ScenarioError(f"{where}: names.{probe}: 1–16 characters")

    foods_doc = _mapping(doc.get("food"), f"{where}: food")
    _only(foods_doc, FOOD_PROBES, f"{where}: food")
    foods = {
        probe: _food(spec, f"{where}: food.{probe}")
        for probe, spec in foods_doc.items() if spec is not None  # None: nothing on it
    }

    events = []
    for i, item in enumerate(doc.get("events") or []):
        events.append(_event(item, f"{where}: events[{i}]"))

    return Scenario(
        name=name.strip(), description=description.strip(), duration=duration, seed=seed,
        controller=controller, controller_options=controller_options, plant=plant,
        fuel_btu=fuel_btu, ambient=_schedule(doc.get("ambient", 70.0), f"{where}: ambient"),
        wind_mph=_schedule(doc.get("wind_mph", 0.0), f"{where}: wind_mph"), lid_open=tuple(lid),
        start_pit_f=start_pit, device=parse_device_fields(doc.get("device"), f"{where}: device"),
        names=dict(names), foods=foods, events=tuple(events), source=source,
    )


def _food(spec: Any, where: str) -> FoodSpec:
    """``{cut: brisket, mass_lb: 12, temp_f: 40}``; without a ``cut``, a plain
    piece that never stalls."""
    spec = _mapping(spec, where)
    _only(spec, ("cut", "temp_f", *_FOOD_FIELDS), where)
    cut = _choice(spec.get("cut", "no-stall"), f"{where}.cut", sorted(PRESETS))
    temp = _number(spec.get("temp_f", 40.0), f"{where}.temp_f", 0, 250)
    try:
        params = preset(cut, **{key: spec[key] for key in spec if key in _FOOD_FIELDS})
    except (ValueError, TypeError) as exc:
        raise ScenarioError(f"{where}: {exc}") from exc
    return FoodSpec(cut, temp, params)


def _event(item: Any, where: str) -> Event:
    """``{at: 2h, timer: 06:00:00}``: a time and exactly one action."""
    item = _mapping(item, where)
    _only(item, ("at", *_EVENT_ACTIONS), where)
    actions = [key for key in item if key != "at"]
    if "at" not in item or len(actions) != 1:
        raise ScenarioError(f"{where}: needs 'at' and exactly one of {'/'.join(_EVENT_ACTIONS)}")
    at = parse_time(item["at"], f"{where}.at")
    key, value = actions[0], item[actions[0]]

    if key in ("plug", "unplug"):
        probe = _probe(value, f"{where}.{key}")

        def set_plugged(sim: Simulator, index: int = PROBES.index(probe), plug: bool = key == "plug") -> None:
            sim.probes[index].plugged = plug

        return Event(at, set_plugged, f"{key} {value}")

    if key == "control":
        fields = parse_device_fields(value, f"{where}.control", allowed=CONTROL_FIELDS)
        shown = value
    else:  # cook_set, food_set, timer
        fields = parse_device_fields({key: value}, where, allowed=(key,))
        shown = {key: value}
    apply = lambda sim, f=fields: apply_device_fields(sim, f)  # noqa: E731
    return Event(at, apply, ", ".join(f"{name}={val}" for name, val in shown.items()))


def _probe(value: Any, where: str) -> str:
    return _choice(value, where, PROBES)
