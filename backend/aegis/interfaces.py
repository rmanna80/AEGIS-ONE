"""Hardware-abstraction interfaces.

Everything above this layer (API, safety monitors, recorder, dashboard) depends
only on these protocols, never on MAVSDK, Gazebo or a specific sensor. Camera,
thermal and depth protocols are added with the perception phases, alongside
their first real implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aegis.contracts import Provenance, VehicleState
from aegis.missions import Mission, MissionResult, MissionStatus


@runtime_checkable
class VehicleTelemetrySource(Protocol):
    """Read-only access to one vehicle's state."""

    vehicle_id: str
    provenance: Provenance

    async def start(self) -> None:
        """Begin acquiring telemetry. Must return promptly; connect in the background."""
        ...

    async def stop(self) -> None: ...

    async def get_state(self) -> VehicleState:
        """Latest consolidated snapshot (``connected=False`` until a vehicle is seen)."""
        ...


@runtime_checkable
class MissionExecutor(Protocol):
    """Operator-commanded mission control. Implementations must refuse to act
    unless they have positively established what they are connected to."""

    async def submit_mission(self, mission: Mission) -> MissionResult: ...

    async def mission_status(self) -> MissionStatus: ...

    async def hold(self, reason: str | None = None) -> None:
        """Stop and hover in place. ``reason`` is given when AEGIS, not the operator,
        asked for the hold (e.g. the route ahead is blocked)."""
        ...

    async def return_to_home(self) -> None: ...

    async def land(self) -> None: ...
