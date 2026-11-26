"""Background poller: GET /all.xml every 5 s into SQLite (task 4).

The poller owns the in-memory "latest" sample and the device health flags
(reachable, consecutive failures, last error). Read failures never crash
the loop — the device is an unauthenticated embedded server on WiFi, so
transient errors are expected.
"""

from __future__ import annotations

import asyncio
import time

from app import db


class Poller:
    def __init__(
        self,
        client,
        conn,
        interval: float = 5.0,
        stale_after: float = 15.0,
    ) -> None:
        self.client = client
        self.conn = conn
        self.interval = interval
        self.stale_after = stale_after
        self.latest: dict | None = None
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0

    @property
    def reachable(self) -> bool:
        return (
            self.last_ok is not None
            and (time.time() - self.last_ok) <= self.stale_after
        )

    def device_state(self) -> dict:
        age = time.time() - self.last_ok if self.last_ok is not None else None
        return {
            "reachable": self.reachable,
            "age_s": round(age, 1) if age is not None else None,
            "last_ok_ts": self.last_ok,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
        }

    async def poll_once(self) -> dict | None:
        """One poll: read /all.xml, store it, update health. Returns the
        new latest sample, or None if the device could not be read."""
        try:
            sample = await self.client.read_all()
        except Exception as exc:  # network or parse errors — keep polling
            self.consecutive_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        ts = int(time.time())
        db.insert_sample(self.conn, ts, sample)
        self.latest = {"ts": ts, **sample}
        self.last_ok = time.time()
        self.consecutive_failures = 0
        self.last_error = None
        return self.latest

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.interval)
