"""Session manifest: which software, configuration and hardware produced a recording."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from aegis import __version__
from aegis.config import REPO_ROOT, Settings
from aegis.replay.evidence import sha256_file

SESSION_SCHEMA = "aegis.session/1"
# Settings that could reveal a credential are never written to a manifest.
SECRET_FIELDS = {"operator_token"}


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _clock_synchronised() -> bool | None:
    """Whether the system clock is NTP-synchronised, or None when that cannot be read."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(out)


def session_manifest(settings: Settings, device: dict[str, Any], session_id: str) -> dict[str, Any]:
    config = {
        key: (str(value) if isinstance(value, Path) else value)
        for key, value in settings.model_dump(mode="json").items()
        if key not in SECRET_FIELDS
    }
    scenario_file = settings.resolved_scenario_file()
    status = _git("status", "--porcelain")
    return {
        "schema": SESSION_SCHEMA,
        "session_id": session_id,
        "software": {
            "aegis_version": __version__,
            "git_commit": _git("rev-parse", "HEAD"),
            "git_uncommitted_changes": None if status is None else bool(status),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "device": {
            "model": device.get("model"),
            "machine": device.get("machine"),
            "cpu_count": device.get("cpu_count"),
            "hostname_sha256": hashlib.sha256(platform.node().encode()).hexdigest()[:16],
        },
        "configuration": config,
        "configuration_note": "The operator token is never written to a manifest.",
        "scenario": {
            "id": settings.scenario,
            "file_sha256": sha256_file(scenario_file) if scenario_file.is_file() else None,
        },
        "detector_model": {
            "file": settings.detector_model.name,
            "sha256": sha256_file(settings.detector_model)
            if settings.detector_model.is_file()
            else None,
        },
        "clocks": {
            "started_utc_s": round(time.time(), 3),
            "started_monotonic_s": round(time.monotonic(), 3),
            "utc_synchronised": _clock_synchronised(),
            "domains": {
                "t": "seconds on this computer's monotonic clock since the recording opened",
                "utc": "this computer's wall clock (UTC, seconds); only as good as its "
                "synchronisation, see utc_synchronised",
                "vehicle_time_s": "the autopilot's own clock inside each state: simulation "
                "time in a simulator, time since boot on hardware",
            },
            "note": "The three clocks are recorded side by side and never converted into "
            "one another. No offset between this computer and another device is measured.",
        },
        "record_schemas": {"flight_log": 1, "audit_log": 1, "session": SESSION_SCHEMA},
    }


def write_session_manifest(folder: Path, manifest: dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "session.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path
