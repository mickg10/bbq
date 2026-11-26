"""Controller plug-ins (docs/SIMULATOR.md).

``controllers.base`` holds the contract every law follows; the laws are
``proportional`` (the documented baseline), its ``bias`` preset, ``pi`` (the
baseline plus integral action) and ``firmware`` (recovered from the device's
firmware, in the firmware phase). :func:`get_controller` picks one by name
for the command line.
"""

from __future__ import annotations

from sim.controllers.base import (
    ControlConfig,
    ControlOutput,
    Controller,
    Mode,
    duty_in_step,
)

CONTROLLERS: dict[str, type] = {}


def register(cls: type) -> type:
    CONTROLLERS[cls.name] = cls
    return cls


def get_controller(name: str, **options):
    """A fresh controller by name (``proportional``, ``bias``, ``pi``,
    ``firmware``).

    ``options`` go to its constructor — e.g. ``bias_pct=15`` or ``ki=0.4``.
    """
    try:
        cls = CONTROLLERS[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown controller {name!r}; choose from {sorted(CONTROLLERS)}"
        ) from exc
    return cls(**options)


# The built-in laws register themselves; imported last so ``register`` exists.
from sim.controllers import firmware, proportional  # noqa: E402,F401

__all__ = [
    "CONTROLLERS",
    "ControlConfig",
    "ControlOutput",
    "Controller",
    "Mode",
    "duty_in_step",
    "get_controller",
    "register",
]
