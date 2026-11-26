"""Probe model (docs/SIMULATOR.md): lag, noise, tenths, OPEN.

One :class:`Probe` per channel (cook, food1..3). It follows the true
temperature with a first-order lag, adds Gaussian noise and reports tenths of
°F, as the device does on the wire. An unplugged probe reports ``None``, the
device's ``OPEN`` — and while it is unplugged it keeps tracking the ambient
air, so plugging it back in shows a reading that has to warm (or cool) its
way back to the truth rather than snapping there.

The default noise, 0.12 °F, is measured: the sd of successive readings on a
real device's pit probe over two idle stretches (1263 and 545 samples) came
to 0.112 and 0.136 °F, and 0.128 °F on a food probe. The lag (20 s) is an
estimate, to be fitted from a real heat-up.
"""

from __future__ import annotations

import math
import random


def to_tenths(temp_f: float) -> int:
    """°F -> tenths of °F, half-up — the device's wire unit (``757`` = 75.7)."""
    return int(math.floor(temp_f * 10.0 + 0.5))


class Probe:
    def __init__(
        self,
        *,
        tau_s: float = 20.0,
        sigma_f: float = 0.12,
        rng: random.Random | None = None,
        plugged: bool = True,
    ) -> None:
        if tau_s < 0:
            raise ValueError("tau_s must be non-negative")
        if sigma_f < 0:
            raise ValueError("sigma_f must be non-negative")
        self.tau_s = float(tau_s)
        self.sigma_f = float(sigma_f)
        self.rng = rng if rng is not None else random.Random(0)
        self.reading: int | None = None  # last reading, tenths °F; None = OPEN
        self._x: float | None = None     # the lagged, noise-free value
        self._plugged = True
        self.plugged = bool(plugged)

    @property
    def plugged(self) -> bool:
        return self._plugged

    @plugged.setter
    def plugged(self, value: bool) -> None:
        """Pulling a probe makes it OPEN at once — whatever it read last."""
        self._plugged = bool(value)
        if not self._plugged:
            self.reading = None

    @property
    def reading_f(self) -> float | None:
        """The last reading in °F, or None while unplugged."""
        return None if self.reading is None else self.reading / 10.0

    @property
    def tracked(self) -> float | None:
        """The lagged, noise-free value behind the last reading (°F), or None
        until the probe has stepped once."""
        return self._x

    def settle(self, true_f: float) -> None:
        """Skip the lag: the probe is already at ``true_f`` (a preheated start)."""
        self._x = float(true_f)
        if self.plugged:
            self.reading = to_tenths(self._x)

    def step(self, dt: float, true_f: float, ambient_f: float) -> int | None:
        """Advance ``dt`` seconds with the tip at ``true_f``; return the reading."""
        track = true_f if self.plugged else ambient_f
        if self._x is None or self.tau_s <= 0:
            self._x = track
        else:
            self._x += (track - self._x) * (1.0 - math.exp(-dt / self.tau_s))
        if not self.plugged:
            self.reading = None
        else:
            self.reading = to_tenths(self._x + self.rng.gauss(0.0, self.sigma_f))
        return self.reading
