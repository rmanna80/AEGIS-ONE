"""Incident capture: preserve what surrounded a failure, as a package that can be checked.

An incident opens by itself when a critical alert is raised, or when the operator
marks one. It closes once the alerts have been clear for a while, and the part of
the session around it is copied into its own folder with a manifest of hashes.

An incident package is evidence of what AEGIS recorded. It is not a diagnosis:
nothing here names a root cause.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import time
import zipfile
from pathlib import Path
from typing import Any

from aegis.contracts import Severity
from aegis.hub import TelemetryHub
from aegis.replay.evidence import MANIFEST_NAME, verify_manifest, write_manifest
from aegis.security import AuditLog

log = logging.getLogger(__name__)

INCIDENT_SCHEMA = "aegis.incident/1"
GAP_S = 1.0  # a silence between state records longer than this is reported as missing data


def slice_flight_log(
    flight_log: Path, start_s: float, end_s: float, incident_id: str
) -> tuple[list[str], dict[str, Any]]:
    """Lines of the flight log between two recording times, re-based to start at 0, and
    what they contain. The header is kept so the slice replays on its own."""
    lines: list[str] = []
    states = 0
    alerts: list[dict[str, Any]] = []
    missions: list[dict[str, Any]] = []
    faults: set[str] = set()
    gaps: list[dict[str, float]] = []
    previous_state_t: float | None = None
    first_utc = last_utc = None
    first_vehicle = last_vehicle = None
    with flight_log.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            record = json.loads(raw)
            if record["kind"] == "header":
                record["data"] = {
                    **record["data"],
                    "incident_id": incident_id,
                    "session_time_of_t0_s": round(start_s, 4),
                }
                lines.append(json.dumps(record, separators=(",", ":")))
                continue
            t = float(record["t"])
            if t < start_s or t > end_s:
                continue
            record["t_session"] = t
            record["t"] = round(t - start_s, 4)
            lines.append(json.dumps(record, separators=(",", ":")))
            utc = record.get("utc")
            if utc is not None:
                first_utc = utc if first_utc is None else first_utc
                last_utc = utc
            if record["kind"] == "state":
                states += 1
                if previous_state_t is not None and t - previous_state_t > GAP_S:
                    gaps.append(
                        {
                            "from_s": round(previous_state_t - start_s, 2),
                            "to_s": round(t - start_s, 2),
                            "seconds": round(t - previous_state_t, 2),
                        }
                    )
                previous_state_t = t
                faults.update(record["data"].get("injected_faults") or [])
                vehicle = record["data"].get("vehicle_time_s")
                if vehicle is not None:
                    first_vehicle = vehicle if first_vehicle is None else first_vehicle
                    last_vehicle = vehicle
            elif record["kind"] == "alert":
                data = record["data"]
                alerts.append(
                    {
                        "t": record["t"],
                        "code": data["code"],
                        "severity": data["severity"],
                        "active": data["active"],
                        "message": data["message"],
                    }
                )
            elif record["kind"] == "mission":
                missions.append({"t": record["t"], "phase": record["data"]["phase"]})
            elif record["kind"] == "fault":
                faults.add(str(record["data"].get("kind")))
    summary = {
        "state_records": states,
        "alerts": alerts,
        "mission_phases": missions,
        "injected_faults_seen": sorted(faults),
        "gaps_in_state_records": gaps,
        "utc_start_s": first_utc,
        "utc_end_s": last_utc,
        "vehicle_time_start_s": first_vehicle,
        "vehicle_time_end_s": last_vehicle,
    }
    return lines, summary


class IncidentManager:
    def __init__(
        self,
        hub: TelemetryHub,
        session_dir: Path | None,
        audit: AuditLog,
        session: dict[str, Any],
        pre_s: float = 15.0,
        post_s: float = 8.0,
        max_s: float = 180.0,
    ) -> None:
        self.hub = hub
        self.session_dir = session_dir
        self.audit = audit
        self.session = session
        self.pre_s, self.post_s, self.max_s = pre_s, post_s, max_s
        self.incidents: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None
        self._clear_since: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._packaging: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ lifecycle

    @property
    def enabled(self) -> bool:
        return self.session_dir is not None and self.hub.recorder is not None

    async def start(self) -> None:
        if self.enabled:
            self._task = asyncio.create_task(self._loop(), name="incident-manager")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._open is not None:  # do not lose an incident that was still open
            await self._close(reason="the backend was stopped while the incident was open")
        for task in list(self._packaging):
            with contextlib.suppress(Exception):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Incident manager step failed")
            await asyncio.sleep(0.5)

    # ---------------------------------------------------------------- open / close

    def _recording_time(self) -> float | None:
        recorder = self.hub.recorder
        return recorder.elapsed_s() if recorder is not None and recorder.is_open else None

    def _critical_codes(self) -> list[str]:
        return sorted(a.code for a in self.hub.alerts.active if a.severity is Severity.CRITICAL)

    def _begin(self, trigger: str, title: str, note: str = "") -> dict[str, Any] | None:
        t = self._recording_time()
        if t is None:
            return None
        incident = {
            "incident_id": f"INC-{time.strftime('%Y%m%dT%H%M%S')}-{len(self.incidents) + 1:02d}",
            "title": title,
            "note": note,
            "trigger": trigger,
            "status": "open",
            "opened_utc_s": round(time.time(), 3),
            "opened_t_s": round(t, 3),
            "alert_codes": self._critical_codes(),
        }
        self.incidents.append(incident)
        self._open = incident
        self._clear_since = None
        log.warning("Incident %s opened: %s", incident["incident_id"], title)
        self.audit.append("incident_opened", incident_id=incident["incident_id"], trigger=trigger)
        return incident

    def mark(self, title: str, note: str = "") -> dict[str, Any] | None:
        """Operator marks an incident now. Adds the note to one that is already open."""
        if self._open is not None:
            self._open["note"] = (self._open["note"] + " " + note).strip()
            self._open["trigger"] += " + operator mark"
            return self._open
        return self._begin("operator mark", title or "Marked by the operator", note)

    async def step(self) -> None:
        """One check (public for tests): open on a critical alert, close once it has cleared."""
        critical = self._critical_codes()
        now = time.monotonic()
        if self._open is None:
            if critical:
                self._begin("critical alert", "Critical alert: " + ", ".join(critical))
            return
        incident = self._open
        incident["alert_codes"] = sorted(set(incident["alert_codes"]) | set(critical))
        t = self._recording_time()
        if t is None:
            return
        if t - incident["opened_t_s"] > self.max_s:
            await self._close(
                reason=f"reached the {self.max_s:.0f} s limit with alerts still active"
            )
        elif critical:
            self._clear_since = None
        elif self._clear_since is None:
            self._clear_since = now
        elif now - self._clear_since >= self.post_s:
            await self._close(reason="critical alerts cleared")

    async def _close(self, reason: str) -> None:
        incident, self._open = self._open, None
        if incident is None:
            return
        t = self._recording_time()
        incident["closed_t_s"] = round(t if t is not None else incident["opened_t_s"], 3)
        incident["closed_utc_s"] = round(time.time(), 3)
        incident["closed_because"] = reason
        incident["status"] = "packaging"
        task = asyncio.create_task(asyncio.to_thread(self._package, incident))
        self._packaging.add(task)
        task.add_done_callback(self._packaging.discard)

    # ------------------------------------------------------------------- packaging

    def _package(self, incident: dict[str, Any]) -> None:
        assert self.session_dir is not None and self.hub.recorder is not None
        try:
            folder = self.session_dir / "incidents" / incident["incident_id"]
            folder.mkdir(parents=True, exist_ok=True)
            start = max(0.0, incident["opened_t_s"] - self.pre_s)
            end = incident["closed_t_s"]
            lines, summary = slice_flight_log(
                self.hub.recorder.path, start, end, incident["incident_id"]
            )
            (folder / "events.jsonl").write_text("\n".join(lines) + "\n")

            audit_records = []
            if self.audit.path is not None and self.audit.path.is_file():
                lo = (summary["utc_start_s"] or incident["opened_utc_s"]) - 1.0
                hi = incident["closed_utc_s"] + 1.0
                for raw in self.audit.path.read_text().splitlines():
                    if raw.strip() and lo <= json.loads(raw).get("t_unix_s", 0.0) <= hi:
                        audit_records.append(raw)
            (folder / "audit_excerpt.jsonl").write_text("\n".join(audit_records) + "\n")
            (folder / "session.json").write_text(json.dumps(self.session, indent=2))

            duration = end - start
            expected = duration / self.hub.period_s
            gap_seconds = sum(g["seconds"] for g in summary["gaps_in_state_records"])
            kept_elsewhere = [
                name
                for name in ("video.mp4", "detections.jsonl", "map.npz", "sar_report.json")
                if (self.session_dir / name).exists()
            ]
            document = {
                "schema": INCIDENT_SCHEMA,
                **{k: v for k, v in incident.items() if k != "status"},
                "window": {
                    "recording_start_s": round(start, 3),
                    "recording_end_s": round(end, 3),
                    "seconds": round(duration, 2),
                    "seconds_kept_before_the_trigger": round(incident["opened_t_s"] - start, 2),
                    "utc_start_s": summary["utc_start_s"],
                    "utc_end_s": summary["utc_end_s"],
                    "vehicle_time_start_s": summary["vehicle_time_start_s"],
                    "vehicle_time_end_s": summary["vehicle_time_end_s"],
                },
                "alerts": summary["alerts"],
                "mission_phases": summary["mission_phases"],
                "injected_faults_seen": summary["injected_faults_seen"],
                "completeness": {
                    "state_records": summary["state_records"],
                    "state_records_expected_at_full_rate": round(expected),
                    "fraction_of_expected": round(summary["state_records"] / max(1.0, expected), 3),
                    "gaps_in_state_records": summary["gaps_in_state_records"],
                    "seconds_without_state_records": round(gap_seconds, 2),
                    "note": "State is recorded only while the vehicle link is up, so a link "
                    "loss appears as a gap.",
                },
                "not_in_this_package": {
                    "kept_in_the_session_folder": kept_elsewhere,
                    "never_recorded": [
                        "raw MAVLink traffic",
                        "application log text",
                        "resource utilisation over time",
                    ],
                },
                "software": self.session.get("software"),
                "device": self.session.get("device"),
                "reproduce": {
                    "replay_this_incident": "AEGIS_MODE=replay AEGIS_REPLAY_FILE="
                    f"{folder / 'events.jsonl'} bash scripts/run_backend.sh",
                    "verify_this_package": f"python scripts/incident_tool.py verify {folder}",
                    "limits": "Replay shows what was recorded. It does not re-run the software "
                    "and is not a deterministic reproduction of the event.",
                },
                "root_cause": "Not determined. This package is evidence, not a diagnosis.",
            }
            (folder / "incident.json").write_text(json.dumps(document, indent=2))
            manifest = write_manifest(folder, f"AEGIS incident {incident['incident_id']}")
            incident.update(
                status="packaged",
                folder=str(folder),
                manifest_sha256=manifest["manifest_sha256"],
                files=len(manifest["files"]),
                state_records=summary["state_records"],
                seconds=round(duration, 2),
            )
            self.audit.append(
                "incident_packaged",
                incident_id=incident["incident_id"],
                manifest_sha256=manifest["manifest_sha256"],
            )
            log.warning(
                "Incident %s packaged (%d files, manifest %s)",
                incident["incident_id"],
                len(manifest["files"]),
                manifest["manifest_sha256"][:16],
            )
        except Exception as exc:
            incident.update(status="failed", error=repr(exc))
            log.exception("Could not package incident %s", incident["incident_id"])

    # ------------------------------------------------------------------- reading

    def get(self, incident_id: str) -> dict[str, Any] | None:
        return next((i for i in self.incidents if i["incident_id"] == incident_id), None)

    def document(self, incident_id: str) -> dict[str, Any] | None:
        incident = self.get(incident_id)
        if incident is None or incident.get("status") != "packaged":
            return None
        folder = Path(incident["folder"])
        return {
            "incident": json.loads((folder / "incident.json").read_text()),
            "verification": verify_manifest(folder, incident["manifest_sha256"]),
        }

    def export_zip(self, incident_id: str) -> bytes | None:
        incident = self.get(incident_id)
        if incident is None or incident.get("status") != "packaged":
            return None
        folder = Path(incident["folder"])
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    archive.write(path, f"{incident_id}/{path.relative_to(folder).as_posix()}")
        return buffer.getvalue()


def import_package(archive: Path, destination: Path) -> dict[str, Any]:
    """Unpack an exported incident and verify it. Refuses paths that leave the folder."""
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise ValueError("The archive contains unsafe paths.")
        roots = {name.split("/", 1)[0] for name in names}
        if len(roots) != 1:
            raise ValueError("Not an AEGIS incident export (expected one top-level folder).")
        destination.mkdir(parents=True, exist_ok=True)
        bundle.extractall(destination)
    folder = destination / roots.pop()
    if not (folder / MANIFEST_NAME).is_file():
        raise ValueError("Not an AEGIS incident export (no manifest).")
    return {"folder": str(folder), "verification": verify_manifest(folder)}
