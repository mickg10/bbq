"""The control law recovered from the CyberQ firmware — placeholder.

Until the firmware phase lands (docs/SIMULATOR.md, "Firmware phase") there is
nothing to recover from, so this is the documented baseline under the name the
scenarios and ``sim compare`` will use for the comparison. The analysis
replaces :meth:`FirmwareController.step` and this docstring with the control
law it found and the addresses it found it at; no vendor code is copied.
"""

from __future__ import annotations

from sim.controllers import register
from sim.controllers.proportional import ProportionalController


@register
class FirmwareController(ProportionalController):
    """NOT the firmware yet: the baseline, standing in for it."""

    name = "firmware"
