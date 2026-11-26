"""Shared fixtures for the simulator tests: seeded and headless.

The factory builds a whole :class:`sim.engine.Simulator` around the default
kamado in 70 °F still air. Tests reach for the parts directly (``sim.probes``,
``sim.device``, ``sim.cooker.plant``) when a unit-level assertion is clearer.
The one exception to "headless" is ``serve_sim``, which serves a simulator on
a real port at high speed for the HTTP facade tests.
"""

from __future__ import annotations

import dataclasses
import pathlib
import threading
import time
from typing import Sequence

import pytest
import uvicorn

from app import db as appdb
from sim import server as sim_server
from sim.device import CyberQDevice, DeviceSettings
from sim.engine import Environment, Event, Simulator
from sim.food import Food, preset
from sim.plant import Cooker, Plant, PlantParams
from sim.probes import Probe

HOUR = 3600.0

#: the cooker a recorded test cook was made with — deliberately not the defaults
TRUTH = PlantParams(c_pit=60.0, ua_pit=90.0, q_max=26_000.0, tau_fire=300.0, draft_min=0.07)
AIR_F = 68.0


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


# -- recorded history ------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Recording:
    db: pathlib.Path
    truth: PlantParams
    ambient_f: float
    samples: int
    mtime: float = 0.0  # of the database file as written: it must not change by being read


def record(path: pathlib.Path, sim: Simulator, start_ts: int = 1_790_000_000) -> int:
    """Write a run's rows to the dashboard's samples table, as its poller
    would have stored them. Returns the number of samples."""
    conn = appdb.connect(str(path))
    conn.executemany(
        "INSERT INTO samples (ts, cook, food1, food2, food3, cook_set, food1_set, food2_set,"
        " food3_set, output_pct, cook_status, food1_status, food2_status, food3_status,"
        " timer, timer_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                start_ts + round(row["t"]), row["cook"], row["food1"], row["food2"], row["food3"],
                row["cook_set"], row["food1_set"], row["food2_set"], row["food3_set"],
                row["output_pct"], row["cook_status"], row["food1_status"], row["food2_status"],
                row["food3_status"], row["timer"], row["timer_status"],
            )
            for row in sim.rows
        ],
    )
    conn.commit()
    conn.close()
    return len(sim.rows)


@pytest.fixture(scope="module")
def recording(tmp_path_factory) -> Recording:
    """Three hours in the life of ``TRUTH``: a cold start toward 250 °F, then
    225 °F from 2 h; a brisket on food1 throughout; food2 plugged in (with
    nothing on it) at 1 h; food3 never. Sampled every 5 s like the poller."""
    path = tmp_path_factory.mktemp("history") / "bbq.db"
    sim = Simulator(
        Cooker(Plant(TRUTH, t_amb=AIR_F), (Food(preset("brisket"), temp_f=40.0), None, None)),
        [Probe(), Probe(), Probe(plugged=False), Probe(plugged=False)],
        CyberQDevice(DeviceSettings(cook_set_f=250.0, food_set_f=[203.0, 180.0, 180.0])),
        env=Environment(ambient_f=AIR_F),
        events=[
            Event(2 * HOUR, lambda s: s.device.set_cook_set_tenths(2250), "set 225"),
            Event(1 * HOUR, lambda s: setattr(s.probe("food2"), "plugged", True), "plug food2"),
        ],
        sample_every=5,
        seed=0,
    )
    sim.run(3 * HOUR)
    samples = record(path, sim)
    return Recording(path, TRUTH, AIR_F, samples, path.stat().st_mtime)


@pytest.fixture()
def serve_sim():
    """Factory: serve a simulator on an ephemeral port at ``speed``× real time
    (600× by default, so a cook's first hour takes six seconds). Returns
    ``(base_url, sim)``, as the root conftest does for the fake CyberQ."""
    started: list[tuple[uvicorn.Server, threading.Thread]] = []

    def _start(speed: float = 600.0, **kwargs) -> tuple[str, Simulator]:
        kwargs.setdefault("max_rows", 3600)
        sim = build_sim(**kwargs)
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
