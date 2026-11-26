"""Command line for the simulator: ``python -m sim run|serve|fit|compare``.

``serve`` is the live facade for the dashboard (``sim/server.py``). The
headless commands come with scenarios.
"""

from __future__ import annotations

import argparse
from typing import Sequence

from sim.controllers import CONTROLLERS, get_controller
from sim.engine import default_simulator
from sim.food import PRESETS, Food, preset


def _serve_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--speed", type=float, default=1.0, metavar="N",
        help="simulated seconds per real second (default 1; 60 runs a cook in a minute)",
    )
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--seed", type=int, default=0, help="probe-noise seed")
    parser.add_argument(
        "--controller", default="proportional", choices=sorted(CONTROLLERS),
        help="control law (default: the documented one)",
    )
    parser.add_argument(
        "--set", type=float, default=None, metavar="F", dest="cook_set_f",
        help="pit setpoint in °F (default 190)",
    )
    parser.add_argument(
        "--ambient", type=float, default=70.0, metavar="F", help="air temperature",
    )
    parser.add_argument(
        "--food", default=None, metavar="CUT", choices=sorted(PRESETS),
        help="put a cut on the food1 probe (default: nothing plugged in)",
    )
    parser.add_argument(
        "--preheat", type=float, default=None, metavar="F",
        help="start with the pit already at this temperature (default: cold)",
    )


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from sim.server import create_app

    sim = default_simulator(
        cook_set_f=args.cook_set_f,
        food=Food(preset(args.food), temp_f=40.0) if args.food else None,
        ambient_f=args.ambient,
        seed=args.seed,
        controller=get_controller(args.controller),
        max_rows=3600,
    )
    if args.preheat is not None:
        sim.preheat(args.preheat)
    url = f"http://{args.host}:{args.port}"
    print(
        f"Serving a simulated CyberQ ({args.controller}, {args.speed:g}x speed) on {url}\n"
        f"  point the dashboard at it: CYBERQ_URL={url}"
    )
    uvicorn.run(app=create_app(sim, speed=args.speed), host=args.host, port=args.port,
                log_level="warning")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sim", description="CyberQ simulator (docs/SIMULATOR.md)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _serve_options(sub.add_parser("serve", help="serve a live simulated CyberQ over HTTP"))
    args = parser.parse_args(argv)
    return _serve(args)
