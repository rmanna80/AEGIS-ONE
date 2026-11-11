"""Runtime configuration, read from ``AEGIS_*`` environment variables or ``.env``."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class OperatingMode(StrEnum):
    SIMULATION = "simulation"  # PX4 SITL; operator commands allowed with a token
    MONITOR = "monitor"  # PX4 SITL; strictly read-only
    REPLAY = "replay"  # recorded flight; strictly read-only


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEGIS_", env_file=".env", extra="ignore")

    mode: OperatingMode = OperatingMode.SIMULATION
    vehicle_id: str = "sim-1"

    # Listen for the autopilot's MAVLink on this machine only. Use udpin://0.0.0.0:14540
    # only when the autopilot runs on another machine.
    px4_url: str = "udpin://127.0.0.1:14540"
    mavsdk_grpc_port: int = 50051

    replay_file: Path = REPO_ROOT / "data" / "sample" / "demo1_survey_sih.jsonl"
    replay_speed: float = 1.0

    operator_token: str | None = None  # generated at startup when unset

    record_flights: bool = True
    data_dir: Path = REPO_ROOT / "data" / "generated"
    scenario: str = "open_field"  # name of a file in simulation/scenarios/
    scenario_file: Path | None = None  # explicit path; overrides `scenario`
    # Replay a whole recorded session (flight + camera + detections). A directory
    # under data/generated/sessions, or "latest". Overrides replay_file.
    replay_session: str | None = None
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"

    # Perception (optional). camera: "none", "stream" (TCP frame stream, e.g. the
    # Gazebo camera bridge) or "video:<path to a video file>".
    camera: str = "none"
    camera_stream_host: str = "127.0.0.1"
    camera_stream_port: int = 5600
    detector_model: Path = REPO_ROOT / "models" / "yolo11n.onnx"
    detector_confidence: float = 0.35
    detector_labels: list[str] = []  # empty = every class the model knows
    # CPU threads for inference. 0 = half the cores, at most 4, so the detector
    # does not starve the simulator (a starved simulator runs below real time).
    detector_threads: int = 0

    # Navigation / mapping (optional). depth: "none", "raycast" (software depth
    # sensor against the scenario's geometry; works on any simulator) or "stream"
    # (Gazebo depth camera through scripts/gz_camera_bridge.py --depth).
    depth: str = "none"
    depth_stream_port: int = 5601
    depth_hfov_deg: float = 73.0
    depth_max_range_m: float = 19.0
    map_resolution_m: float = 0.5
    nav_clearance_m: float = 3.0
    # Check routes against the scenario's known obstacles as well as the live map.
    # Turn off to rely on sensing alone (e.g. to exercise detection and replanning).
    nav_use_prior_map: bool = True
    # Command PX4 Hold when the route ahead is blocked or depth sensing is lost.
    nav_auto_hold: bool = True
    nav_load_map: Path | None = None  # start from a previously saved map.npz

    # Search and rescue (optional). thermal: "none" or "model" (software thermal camera
    # against the scenario's heat sources; works on any simulator). Needs a scenario
    # with a search_area. Advisory only: it never commands the vehicle.
    thermal: str = "none"
    thermal_hfov_deg: float = 57.0
    thermal_min_delta_c: float = 5.0
    sar_altitude_m: float = 25.0
    sar_overlap: float = 0.2

    # Fleet (optional): number of simulated vehicles. Vehicle i (from 0) is expected on
    # UDP port 14540+i (PX4 instance i; see scripts/run_fleet.sh) and is named sim-(i+1).
    fleet_size: int = Field(default=1, ge=1, le=4)
    # Ask PX4 to Hold two vehicles that get too close to each other in the air.
    fleet_auto_hold: bool = True

    # Anomaly detection on telemetry (advisory alerts only; it cannot command the vehicle).
    anomaly: bool = True
    # Allow the operator to inject faults into AEGIS's copy of the telemetry, to test the
    # monitors. Simulation mode and an attested simulator only. Off unless set.
    fault_injection: bool = False

    broadcast_hz: float = 10.0
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    def resolved_scenario_file(self) -> Path:
        if self.scenario_file is not None:
            return self.scenario_file
        return REPO_ROOT / "simulation" / "scenarios" / f"{self.scenario}.json"

    def resolved_replay_session(self) -> Path | None:
        """Directory of the session to replay, or ``None`` to replay ``replay_file``."""
        if not self.replay_session:
            return None
        sessions = self.data_dir / "sessions"
        if self.replay_session == "latest":
            found = sorted(p for p in sessions.glob("*") if (p / "flight.jsonl").is_file())
            if not found:
                raise ValueError(f"No recorded sessions in {sessions}.")
            return found[-1]
        candidate = Path(self.replay_session)
        if not candidate.is_absolute() and not candidate.is_dir():
            candidate = sessions / self.replay_session
        if not (candidate / "flight.jsonl").is_file():
            raise ValueError(f"{candidate} is not a recorded session (no flight.jsonl).")
        return candidate

    @field_validator("mode", mode="before")
    @classmethod
    def _no_physical_mode(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in {"physical", "hardware", "real"}:
            raise ValueError(
                "Physical flight is not available in this release. It is a separately "
                "gated future milestone (see docs/safety/README.md)."
            )
        return value
