"""Command line for the simulator: ``python -m sim run|serve|fit|compare``.

    python -m sim run                                  # scorecard: every scenario, its own controller
    python -m sim run --controller proportional,bias,pi
                                                       # ...every scenario under each law
    python -m sim run --scenario brisket-12h --controller pi --out run.csv
    python -m sim serve --speed 60 --port 8099         # a live CyberQ for the dashboard
    python -m sim compare --db bbq.db                  # a recorded cook through the model
    python -m sim fit --db bbq.db --out cooker.yaml    # the cooker's parameters from one

Scenarios live in ``scenarios/`` (``sim/scenario.py``); a name without a
path is looked up there. See docs/SIMULATOR.md.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import pathlib
import sys
from typing import Sequence

from sim.controllers import CONTROLLERS, get_controller
from sim.engine import default_simulator
from sim.fit import (
    DEFAULT_FIT,
    DEFAULT_PRIOR,
    FitError,
    estimate_ambient,
    fit_plant,
    format_fit,
    load_params,
    write_params,
)
from sim.food import PRESETS, Food, preset
from sim.history import (
    HistoryError,
    format_score,
    load_track,
    open_history,
    replay,
    score,
    write_comparison_csv,
)
from sim.run import DEFAULT_SAMPLE_S, run_scenario, write_csv
from sim.scenario import (
    Scenario,
    ScenarioError,
    apply_device_fields,
    list_scenarios,
    load_scenario,
    parse_overrides,
    parse_time,
    scenario_path,
)
from sim.summary import format_summary, format_table


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


# -- shared options -------------------------------------------------------------

def _scenario_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scenario", action="append", metavar="NAME", dest="scenarios",
        help="a scenario file, or the name of one in scenarios/ (repeatable; default: all of them)",
    )
    parser.add_argument(
        "--controller", action="append", metavar="LAW", dest="controllers",
        help=f"run with this control law instead of the scenario's own: {', '.join(sorted(CONTROLLERS))}"
             " (repeatable, or comma-separated)",
    )
    parser.add_argument("--duration", metavar="T", help="override the scenario's duration (e.g. 14h)")
    parser.add_argument("--seed", type=int, help="override the scenario's probe-noise seed")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE_S, metavar="S", help="seconds between kept rows (default 60)")
    parser.add_argument("--dt", type=float, default=1.0, help="control-loop step in seconds (default 1)")
    parser.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE", dest="overrides",
        help="change a device setting: cook_set=250, propband=20, opendetect=false, food_set.food1=190 (repeatable)",
    )


def _scenario_paths(names: Sequence[str] | None) -> list[pathlib.Path]:
    return [scenario_path(name) for name in (names or [path.stem for path in list_scenarios()])]


def _laws(names: Sequence[str] | None) -> list[str] | None:
    """--controller entries, comma-separated ones split; None for "the scenario's own"."""
    if not names:
        return None
    laws = [law for name in names for law in name.split(",") if law]
    unknown = [law for law in laws if law not in CONTROLLERS]
    if unknown:
        raise ScenarioError(
            f"unknown controller {', '.join(repr(law) for law in unknown)}; choose from {', '.join(sorted(CONTROLLERS))}"
        )
    return laws or None


def _loaded(args) -> list[Scenario]:
    """The scenarios asked for — by --scenario, as bare names, or all of them —
    each under the laws asked for, or its own."""
    overrides = {}
    if args.overrides:
        overrides["device"] = parse_overrides(args.overrides)
    if args.duration is not None:
        overrides["duration"] = parse_time(args.duration, "--duration")
    if args.seed is not None:
        overrides["seed"] = args.seed
    laws = _laws(args.controllers)
    return [
        load_scenario(path, **({**overrides, "controller": law} if law else overrides))
        for path in _scenario_paths((args.scenarios or []) + list(args.names))
        for law in (laws or [None])
    ]


# -- run ------------------------------------------------------------------------

def _run(args: argparse.Namespace) -> int:
    scenarios = _loaded(args)
    many = len(scenarios) > 1
    if many and args.out:
        return _fail("--out takes one run; give a single --scenario/--controller, or use --out-dir")
    results = [run_scenario(s, sample_every=max(1, round(args.sample / args.dt)), dt=args.dt) for s in scenarios]
    if not args.quiet:
        if many:
            print(format_table([result.summary for result in results], legend=not args.no_legend))
        else:
            print(format_summary(results[0].summary))
    for result in results:
        fuel0 = result.sim.cooker.plant.fuel0_btu
        if args.out:
            print(f"wrote {write_csv(args.out, result.rows, fuel0_btu=fuel0)}")
        if args.out_dir:
            written = write_csv(pathlib.Path(args.out_dir) / f"{result.scenario.name}-{result.controller}.csv",
                                result.rows, fuel0_btu=fuel0)
            if not args.quiet:
                print(f"wrote {written}")
    if args.json:
        path = pathlib.Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([result.summary for result in results], indent=2) + "\n")
        if not args.quiet:
            print(f"wrote {path}")
    return 0


# -- serve ----------------------------------------------------------------------

def _serve_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--speed", type=float, default=1.0, metavar="N",
        help="simulated seconds per real second (default 1; 60 runs a cook in a minute)",
    )
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--scenario", metavar="NAME", help="what is in the cooker (default: a cold, empty one)")
    parser.add_argument("--seed", type=int, help="probe-noise seed")
    parser.add_argument("--controller", metavar="LAW", help="control law (default: the scenario's, or the documented one)")
    parser.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE", dest="overrides",
        help="change a device setting (as for `sim run`; repeatable)",
    )
    parser.add_argument(
        "--ambient", type=float, default=70.0, metavar="F", help="air temperature, with no scenario",
    )
    parser.add_argument(
        "--food", default=None, metavar="CUT", choices=sorted(PRESETS), help="with no scenario: a cut on the food1 probe",
    )
    parser.add_argument(
        "--preheat", type=float, default=None, metavar="F",
        help="start with the pit already at this temperature (default: cold)",
    )


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from sim.server import create_app

    overrides = parse_overrides(args.overrides) if args.overrides else {}
    if args.scenario:
        scenario = load_scenario(
            _scenario_paths([args.scenario])[0],
            **({"controller": args.controller} if args.controller else {}),
            **({"seed": args.seed} if args.seed is not None else {}),
            **({"device": overrides} if overrides else {}),
        )
        sim = scenario.build(max_rows=3600)
        names = scenario.names
    else:
        if args.controller and args.controller not in CONTROLLERS:
            return _fail(f"unknown controller {args.controller!r}; choose from {', '.join(sorted(CONTROLLERS))}")
        sim = default_simulator(
            food=Food(preset(args.food), temp_f=40.0) if args.food else None,
            ambient_f=args.ambient,
            seed=0 if args.seed is None else args.seed,
            controller=get_controller(args.controller or "proportional"),
            max_rows=3600,
        )
        apply_device_fields(sim, overrides)
        names = {}
    if args.preheat is not None:
        sim.preheat(args.preheat)
    url = f"http://{args.host}:{args.port}"
    what = f"the {args.scenario} scenario" if args.scenario else "an empty, cold cooker"
    print(
        f"Serving a simulated CyberQ on {url}\n"
        f"  {args.speed:g}x speed, {sim.device.controller.name} controller, {what}\n"
        f"  point the dashboard at it: CYBERQ_URL={url}"
    )
    uvicorn.run(app=create_app(sim, speed=args.speed, names=names), host=args.host, port=args.port,
                log_level="warning")
    return 0


# -- recorded history -------------------------------------------------------------

def _when(text: str) -> int:
    """ISO date-time or unix seconds."""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoryError(f"not a time: {text!r} (ISO date-time or unix seconds)") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp())


def _history_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="the dashboard's SQLite history (opened read-only)")
    parser.add_argument("--from", dest="t_from", default=None, metavar="WHEN", help="ISO date-time or unix seconds")
    parser.add_argument("--to", dest="t_to", default=None, metavar="WHEN")
    parser.add_argument("--ambient", type=float, default=None, metavar="F", help="air temperature (default: estimated, assuming a cold start)")
    parser.add_argument("--fuel-btu", type=float, default=200_000.0, metavar="BTU", help="the load on the fire (0 for an unlit cooker)")
    parser.add_argument("--params", metavar="FILE", help="cooker parameters YAML (from `sim fit`)")
    parser.add_argument(
        "--food", action="append", default=[], metavar="PROBE=CUT[:LB]",
        help=f"what is on a food probe, e.g. food1=brisket:12 (cuts: {', '.join(sorted(PRESETS))}); default a piece that does not stall",
    )
    parser.add_argument("--dt", type=float, default=5.0, help="step in seconds")
    parser.add_argument(
        "--skip", action="append", default=[], metavar="FROM:TO",
        help="leave a window out of the score and the fit, for a lid that came off: e.g. 4h55m:5h40m "
             "(times from the recording's first sample; repeatable)",
    )


def _skips(items: Sequence[str]) -> list[tuple[float, float]]:
    """``FROM:TO`` windows (two <n><h|m|s> times around a colon) in seconds."""
    windows = []
    for item in items:
        start, sep, end = item.rpartition(":")
        if not sep:
            raise HistoryError(f"--skip {item!r}: expected FROM:TO, like 4h55m:5h40m")
        try:
            start_s, end_s = parse_time(start, "start"), parse_time(end, "end")
        except ScenarioError as exc:
            raise HistoryError(f"--skip {item!r}: {exc}") from None
        if end_s <= start_s:
            raise HistoryError(f"--skip {item!r}: ends before it starts")
        windows.append((start_s, end_s))
    return windows


def _foods(items: Sequence[str]) -> dict:
    foods = {}
    for item in items:
        probe, _, rest = item.partition("=")
        cut, _, pounds = rest.partition(":")
        if probe not in ("food1", "food2", "food3") or not rest:
            raise HistoryError(f"--food {item!r}: expected PROBE=CUT or PROBE=CUT:LB")
        try:
            foods[probe] = preset(cut, **({"mass_lb": float(pounds)} if pounds else {}))
        except (ValueError, TypeError) as exc:
            raise HistoryError(f"--food {item!r}: {exc}") from exc
    return foods


#: the parameters that say what a cooker is, for a one-line description
_PLANT_HEADLINE = ("c_pit", "ua_pit", "q_max", "tau_fire", "draft_min")


def _ambient_for(args, track, from_file: float | None) -> float:
    if args.ambient is not None:
        return args.ambient
    return from_file if from_file is not None else estimate_ambient(track)


def _model_for(args, track):
    params, from_file = load_params(args.params) if args.params else (None, None)
    return replay(
        track, ambient_f=_ambient_for(args, track, from_file), params=params,
        fuel_btu=args.fuel_btu, foods=_foods(args.food), controller=args.controller or "proportional",
        controller_options=_controller_options(args.controller_option), open_loop=args.open_loop, dt=args.dt,
    )


def _controller_options(items: Sequence[str] | None) -> dict:
    options = {}
    for item in items or []:
        key, sep, text = item.partition("=")
        if not sep:
            raise ScenarioError(f"--controller-option {item!r}: expected key=value")
        try:
            number = float(text)
        except ValueError as exc:
            raise ScenarioError(f"--controller-option {item!r}: {text!r} is not a number") from exc
        options[key] = int(number) if number.is_integer() else number
    return options


def _compare(args: argparse.Namespace) -> int:
    with contextlib.closing(open_history(args.db)) as conn:
        track = load_track(conn, _when(args.t_from) if args.t_from else None, _when(args.t_to) if args.t_to else None)
    played = _model_for(args, track)
    scored = score(track, played.simulated, None if played.open_loop else played.fan, exclude=_skips(args.skip))
    loop = (
        "open loop, the recorded fan output driving the plant" if played.open_loop
        else f"{args.controller or 'proportional'} controller on the recorded setpoints"
    )
    plant = played.sim.cooker.plant
    cooker = ", ".join(f"{name} {getattr(plant.params, name):g}" for name in _PLANT_HEADLINE)
    ambient = played.sim.env.ambient_at(0.0)
    print(format_score(
        track, scored,
        model=f"{loop}; air {ambient:.1f} °F; fuel {plant.fuel0_btu / 1000:.0f}k BTU; {cooker}",
    ))
    if args.csv:
        print(f"wrote {write_comparison_csv(args.csv, played)}")
    if args.json:
        path = pathlib.Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"scored": scored, "open_loop": played.open_loop}, indent=2) + "\n")
        print(f"wrote {path}")
    return 0


def _fit(args: argparse.Namespace) -> int:
    with contextlib.closing(open_history(args.db)) as conn:
        track = load_track(conn, _when(args.t_from) if args.t_from else None, _when(args.t_to) if args.t_to else None)
    params, from_file = load_params(args.params) if args.params else (None, None)
    result = fit_plant(
        track, ambient_f=_ambient_for(args, track, from_file), params=params,
        fitted=[name for name in args.fit.split(",") if name], fuel_btu=args.fuel_btu,
        foods=_foods(args.food), dt=args.dt,
        max_evaluations=args.evaluations, prior=args.prior, exclude=_skips(args.skip),
        progress=lambda n, rmse: print(f"  evaluation {n}: best pit RMSE so far {rmse:.2f} °F", file=sys.stderr),
    )
    print(format_fit(result))
    out = pathlib.Path(args.out)
    print(f"wrote {write_params(out, result, samples=len(track.times))}")
    return 0


# -- entry point -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sim", description="CyberQ simulator (docs/SIMULATOR.md)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="play a scenario headless and sum it up")
    _scenario_option(run)
    run.add_argument("names", nargs="*", metavar="SCENARIO", help="scenarios to run, as for --scenario")
    run.add_argument("--out", metavar="FILE.csv", help="write the time series (one run only)")
    run.add_argument("--out-dir", metavar="DIR", help="write each run's time series there")
    run.add_argument("--json", metavar="FILE", help="write the summaries as JSON")
    run.add_argument("--quiet", "-q", action="store_true", help="print nothing but the files written")
    run.add_argument(
        "--no-legend", action="store_true",
        help="leave out the lines under a table that say what each column means",
    )
    run.set_defaults(handler=_run)

    serve = sub.add_parser("serve", help="serve a live simulated CyberQ over HTTP")
    _serve_options(serve)
    serve.set_defaults(handler=_serve)

    compare = sub.add_parser("compare", help="replay a recorded cook through the model and score it")
    _history_options(compare)
    compare.add_argument("--controller", help="control law for a closed-loop replay (default proportional)")
    compare.add_argument("--controller-option", action="append", default=[], metavar="KEY=VALUE")
    compare.add_argument("--open-loop", action="store_true", help="drive the plant with the recorded fan output instead")
    compare.add_argument("--csv", metavar="FILE", help="measured and modelled, side by side")
    compare.add_argument("--json", metavar="FILE")
    compare.set_defaults(handler=_compare)

    fit = sub.add_parser("fit", help="estimate the cooker's parameters from a recorded cook")
    _history_options(fit)
    fit.add_argument("--fit", default=",".join(DEFAULT_FIT), metavar="NAMES", help="plant parameters to fit (comma-separated)")
    fit.add_argument("--evaluations", type=int, default=400, help="replays to spend (default 400)")
    fit.add_argument("--prior", type=float, default=DEFAULT_PRIOR, help=f"pull toward the starting values, in °F² per octave (default {DEFAULT_PRIOR})")
    fit.add_argument("--out", default="cooker.yaml", metavar="FILE", help="where to write the parameters (default cooker.yaml)")
    fit.set_defaults(handler=_fit, dt=10.0)  # the plant's time constants are minutes: a coarser step fits fine
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (ScenarioError, HistoryError, FitError) as exc:
        return _fail(str(exc))
    except FileNotFoundError as exc:
        return _fail(f"cannot read {exc.filename}")


__all__ = ["build_parser", "main"]
