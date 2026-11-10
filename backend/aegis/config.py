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
    scenario_file: Path = REPO_ROOT / "simulation" / "scenarios" / "open_field.json"
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"

    broadcast_hz: float = 10.0
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @field_validator("mode", mode="before")
    @classmethod
    def _no_physical_mode(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in {"physical", "hardware", "real"}:
            raise ValueError(
                "Physical flight is not available in this release. It is a separately "
                "gated future milestone (see docs/safety/README.md)."
            )
        return value
