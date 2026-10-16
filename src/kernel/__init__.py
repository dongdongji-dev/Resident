"""kernel — the Agent OS Kernel package.

Top-level namespace for the Resident project. The agent cognition layer
lives in :mod:`kernel.agent_framework`; future subsystems (skills, drivers,
scheduler, http interface) mount alongside it.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
