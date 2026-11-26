"""Shared fixtures for the simulator tests: seeded and headless.

The factory builds a whole :class:`sim.engine.Simulator` around the default
kamado in 70 °F still air. Tests reach for the parts directly (``sim.probes``,
``sim.device``, ``sim.cooker.plant``) when a unit-level assertion is clearer.
The one exception to "headless" is ``serve_sim``, which serves a simulator on
a real port at high speed for the HTTP facade tests.
"""

from __future__ import annotations

import threading
import time
from typing import Sequence

import pytest
import uvicorn

from sim import server as sim_server
from sim.device import CyberQDevice, DeviceSettings
from sim.engine import Environment, Event, Simulator
from sim.food import Food
from sim.plant import Cooker, Plant, PlantParams
from sim.probes import Probe

HOUR = 3600.0


def build_sim(
    *,
    cook_set_f: float = 225.0,
    food: Food | None = None,
    food_set_f: float = 203.0,
    fuel_btu: float = 200_000.0,
    plant_params: PlantParams | None = None,
    lid_events: Sequence[tuple[float, float]] = (),
    wind_mph: float = 0.0,
    ambient_f: float = 70.0,
    seed: int = 0,
    events: Sequence[Event] = (),
    preheat: float | None = None,
    sample_every: int = 1,
    dt: float = 1.0,
    controller=None,
    max_rows: int | None = None,
    **settings,
) -> Simulator:
    """A simulator with one food probe plugged iff there is food on it."""
    env = Environment(ambient_f=ambient_f, wind_mph=wind_mph, lid_events=lid_events)
    cooker = Cooker(
        Plant(plant_params, t_amb=env.ambient_at(0.0), fuel_btu=fuel_btu),
        [food] if food is not None else [],
    )
    probes = [
        Probe(), Probe(plugged=food is not None),
        Probe(plugged=False), Probe(plugged=False),
    ]
    device = CyberQDevice(
        DeviceSettings(
            cook_set_f=cook_set_f,
            food_set_f=[food_set_f, 180.0, 180.0],
            **settings,
        ),
        controller,
    )
    sim = Simulator(
        cooker, probes, device, env=env,
        dt=dt, sample_every=sample_every, events=events, seed=seed, max_rows=max_rows,
    )
    if preheat is not None:
        sim.preheat(preheat)
    return sim


@pytest.fixture()
def make_sim():
    return build_sim


@pytest.fixture()
def serve_sim():
    """Factory: serve a simulator on an ephemeral port at ``speed``× real time
    (600× by default, so a cook's first hour takes six seconds). Returns
    ``(base_url, sim)``, as the root conftest does for the fake CyberQ."""
    started: list[tuple[uvicorn.Server, threading.Thread]] = []

    def _start(speed: float = 600.0, **kwargs) -> tuple[str, Simulator]:
        sim = build_sim(max_rows=3600, **kwargs)
        config = uvicorn.Config(
            sim_server.create_app(sim, speed=speed), host="127.0.0.1", port=0,
            log_level="warning", access_log=False,
        )
        instance = uvicorn.Server(config)
        thread = threading.Thread(target=instance.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while not instance.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert instance.started, "the simulated CyberQ did not start"
        started.append((instance, thread))
        return f"http://127.0.0.1:{instance.servers[0].sockets[0].getsockname()[1]}", sim

    yield _start
    for instance, thread in started:
        instance.should_exit = True
        thread.join(timeout=5.0)
