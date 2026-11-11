"""Runtime configuration, read from ``AEGIS_*`` environment variables or ``.env``."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import field_validator
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

    px4_url: str = "udpin://0.0.0.0:14540"
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
