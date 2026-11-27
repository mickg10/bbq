"""A fake CyberQ with a cook on, for the ETA browser test.

``dev.fake_cyberq`` answers everything but the two sample pages. Those come
from a scripted cook that moves with the clock, so that a history seeded from
the same script and the dashboard's live samples agree:

* the pit at 225 °F;
* Food 1 past its stall and relaxing towards the pit with a 2 h time
  constant: 190 °F an hour into the cook, 203 °F (its setpoint) 55 minutes later;
* Food 2 stalled at 158 °F, set to 195 °F;
* Food 3 open.

Run as ``uvicorn tests.eta_device:app`` with ``ETA_COOK_START`` set to the
unix time the cook began. :func:`state_at` is what the device shows at a
given moment, for seeding a history from the same script.
"""

from __future__ import annotations

import math
import os
import time

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from dev import fake_cyberq

TAU_S = 2.0 * 3600.0
#: Food 1 this many seconds into the cook, °F
HOUR_MARK_F = 190.0


def state_at(now: float, start: float | None = None) -> dict:
    """The device's state ``now``, for a cook that began at ``start`` (by
    default the one named in the environment)."""
    start = float(os.environ["ETA_COOK_START"]) if start is None else start
    food1 = 225.0 - (225.0 - HOUR_MARK_F) * math.exp(-(now - start - 3600.0) / TAU_S)
    return {
        **fake_cyberq.DEFAULTS,
        "COOK_TEMP": 2250, "COOK_SET": 2250, "COOK_STATUS": 0, "OUTPUT_PERCENT": 40,
        "FOOD1_TEMP": round(food1 * 10), "FOOD1_SET": 2030, "FOOD1_STATUS": 0,
        "FOOD2_TEMP": 1580, "FOOD2_SET": 1950, "FOOD2_STATUS": 0,
        "FOOD3_TEMP": None, "FOOD3_STATUS": 4,
    }


app = FastAPI(title="Fake CyberQ WiFi, cooking")


@app.get("/all.xml")
async def all_xml() -> PlainTextResponse:
    return PlainTextResponse(fake_cyberq._all_xml(state_at(time.time())))


@app.get("/status.xml")
async def status_xml() -> PlainTextResponse:
    return PlainTextResponse(fake_cyberq._status_xml(state_at(time.time())))


# everything else is the ordinary fake's
app.mount("/", fake_cyberq.app)
