"""Search-and-rescue service: runs the thermal pipeline and keeps the candidate list.

Advisory only. This service is given the telemetry hub to read the vehicle state
and write to the flight log; it is never given the mission executor, so it cannot
command the vehicle. Candidates change status only through :meth:`review`, which
the API exposes to the authenticated operator.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from aegis.contracts import Severity
from aegis.hub import TelemetryHub
from aegis.missions import Mission, MissionConstraints, Point2
from aegis.safety import Finding
from aegis.sar.candidates import Candidate, CandidateRegistry
from aegis.sar.coverage import CoveragePlanError, CoverageTracker, plan_lawnmower, split_area
from aegis.sar.detect import HeatBlob, detect_heat_sources
from aegis.sar.review import ReviewRequest, ReviewStatus
from aegis.sar.thermal import ThermalFrame, ThermalSource

log = logging.getLogger(__name__)

STALE_AFTER_S = 3.0
MATCH_RADIUS_M = 4.0
LIMITATIONS = [
    "Thermal imagery is a software sensor model (simulated), not a physical camera.",
    "Positions assume flat ground at home elevation and use the autopilot's pose estimate.",
    "Categories describe size and temperature only; they do not identify what an object is.",
    "Warm objects under cover (roofs, dense canopy) cannot be seen from above.",
    "Every candidate needs an operator's decision; nothing is confirmed automatically.",
]


class SarService:
    def __init__(
        self,
        hub: TelemetryHub,
        scenario: dict[str, Any],
        thermal: ThermalSource | None,
        save_dir: Path | None = None,
        default_altitude_m: float = 25.0,
        default_overlap: float = 0.2,
        min_delta_c: float = 5.0,
        extra_thermals: Sequence[ThermalSource] | None = None,
    ) -> None:
        if "search_area" not in scenario:
            raise ValueError(f"Scenario {scenario.get('scenario_id')!r} has no search_area.")
        self.hub = hub
        self.scenario = scenario
        self.scenario_id = str(scenario["scenario_id"])
        self.thermal = thermal
        # One thermal camera per vehicle; every camera feeds the same candidate list.
        self.thermals: list[ThermalSource] = ([thermal] if thermal is not None else []) + list(
            extra_thermals or []
        )
        self._lock = threading.Lock()
        self.save_dir = save_dir
        self.default_altitude_m = default_altitude_m
        self.default_overlap = default_overlap
        self.min_delta_c = min_delta_c
        self.area: list[Point2] = [
            (float(e), float(n)) for e, n in scenario["search_area"]["polygon_enu_m"]
        ]
        self.constraints = MissionConstraints.model_validate(
            scenario["demo_mission"]["constraints"]
        )
        self.registry = CandidateRegistry()
        self.coverage = CoverageTracker(self.area)
        self.frames_processed = 0
        self.last_error: str | None = None
        self.process_ms: float | None = None
        self._latest: tuple[ThermalFrame, list[HeatBlob]] | None = None
        self._last_frame_at = 0.0
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        for index, thermal in enumerate(self.thermals):
            self._tasks.append(
                asyncio.create_task(self._loop(thermal), name=f"sar-thermal-{index}")
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self.save_dir is not None and self.frames_processed > 0:
            with contextlib.suppress(OSError):
                self.save_report(self.save_dir)

    async def _loop(self, thermal: ThermalSource) -> None:
        while True:
            try:
                frame = await thermal.read_frame()
                if frame is None:
                    await asyncio.sleep(0.2)
                    continue
                started = time.monotonic()
                await asyncio.to_thread(self.process, frame)
                self.process_ms = (time.monotonic() - started) * 1000.0
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                log.exception("Thermal frame could not be processed")
                await asyncio.sleep(0.5)

    def process(self, frame: ThermalFrame) -> list[HeatBlob]:
        """Detect, merge into candidates and update coverage for one frame (public for tests)."""
        blobs = detect_heat_sources(frame, min_delta_c=self.min_delta_c)
        with self._lock:  # frames from several vehicles arrive on different threads
            self.registry.observe(blobs, frame.provenance, frame.source, frame.captured_at_unix_s)
            if frame.pose.position_enu_m[2] >= 5.0:
                self.coverage.mark(frame.pose, frame.horizontal_fov_rad, frame.width, frame.height)
            # The display follows the first vehicle's camera, or any camera that sees something.
            first = self.thermals[0].source if self.thermals else frame.source
            if self._latest is None or frame.source == first or blobs:
                self._latest = (frame, blobs)
            self._last_frame_at = time.monotonic()
            self.frames_processed += 1
            self._drain_events()
        return blobs

    def _drain_events(self) -> None:
        events, self.registry.events = self.registry.events, []
        recorder = self.hub.recorder
        for event in events:
            if event["event"] == "candidate":
                log.info(
                    "Heat source %s (%s) near E %.0f N %.0f awaits operator review",
                    event["candidate_id"],
                    event["category"],
                    event["east_m"],
                    event["north_m"],
                )
            else:
                log.info(
                    "Operator marked %s as %s%s",
                    event["candidate_id"],
                    event["status"],
                    f": {event['review_note']}" if event["review_note"] else "",
                )
            if recorder is not None:
                with contextlib.suppress(OSError, ValueError):
                    recorder.record_event("sar", event)

    # --------------------------------------------------------------------- alerts

    def findings(self) -> list[Finding]:
        found: list[Finding] = []
        waiting = self.registry.awaiting_review()
        if waiting:
            found.append(
                (
                    "sar.review_needed",
                    Severity.INFO,
                    f"{waiting} heat source{'s' if waiting != 1 else ''} "
                    "await operator review. The vehicle continues its mission.",
                )
            )
        state = self.hub.state
        if (
            bool(self.thermals)
            and state is not None
            and state.connected
            and self.frames_processed > 0
            and time.monotonic() - self._last_frame_at > STALE_AFTER_S
        ):
            found.append(("thermal.stale", Severity.WARNING, "No thermal frames are arriving."))
        return found

    # ------------------------------------------------------------------- operator

    def candidates(self) -> list[Candidate]:
        return self.registry.established()

    def review(self, candidate_id: str, request: ReviewRequest) -> Candidate | None:
        candidate = self.registry.review(candidate_id, request)
        self._drain_events()
        return candidate

    def plan(
        self,
        start_enu_m: Point2,
        altitude_m: float | None = None,
        overlap: float | None = None,
        horizontal_fov_rad: float | None = None,
    ) -> tuple[Mission, dict[str, Any]]:
        hfov = horizontal_fov_rad
        if hfov is None:
            sensor_hfov = getattr(self.thermal, "hfov", None)
            hfov = float(sensor_hfov) if sensor_hfov is not None else math.radians(57.0)
        return plan_lawnmower(
            self.area,
            self.constraints,
            altitude_m if altitude_m is not None else self.default_altitude_m,
            hfov,
            overlap if overlap is not None else self.default_overlap,
            start_enu_m,
            mission_id=f"sar-{self.scenario_id}".replace("_", "-"),
            name=f"Search pattern over {self.scenario.get('name', self.scenario_id)}",
        )

    def plan_fleet(
        self,
        starts: list[tuple[str, Point2]],
        altitude_m: float | None = None,
        overlap: float | None = None,
        altitude_step_m: float = 6.0,
    ) -> list[dict[str, Any]]:
        """One search pattern per vehicle: the area cut into side-by-side strips, each
        vehicle at its own height (``altitude_step_m`` apart) so routes never share a level.
        Proposals only; the operator submits each one."""
        strips = split_area(self.area, len(starts))
        if len(strips) != len(starts):
            raise CoveragePlanError("The search area is too small to share between the vehicles.")
        base = altitude_m if altitude_m is not None else self.default_altitude_m
        hfov = getattr(self.thermal, "hfov", math.radians(57.0))
        plans = []
        for index, ((vehicle_id, start), strip) in enumerate(zip(starts, strips, strict=True)):
            mission, summary = plan_lawnmower(
                strip,
                self.constraints,
                base + index * altitude_step_m,
                float(hfov),
                overlap if overlap is not None else self.default_overlap,
                start,
                mission_id=f"sar-{self.scenario_id}-{vehicle_id}".replace("_", "-"),
                name=f"Search strip {index + 1} of {len(strips)} ({vehicle_id})",
            )
            plans.append(
                {
                    "vehicle_id": vehicle_id,
                    "mission": mission,
                    "summary": summary,
                    "strip_enu_m": [list(p) for p in strip],
                }
            )
        return plans

    # --------------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        frame = self._latest[0] if self._latest else None
        established = self.registry.established()
        return {
            "enabled": True,
            "thermal_source": frame.source if frame else getattr(self.thermal, "source", None),
            "thermal_provenance": frame.provenance.value if frame else None,
            "frames_processed": self.frames_processed,
            "frame_age_s": (
                round(time.monotonic() - self._last_frame_at, 2) if self._last_frame_at else None
            ),
            "process_ms": self.process_ms,
            "candidates": len(established),
            "awaiting_review": self.registry.awaiting_review(),
            "coverage_fraction": round(self.coverage.fraction(), 4),
            "search_area_enu_m": [list(p) for p in self.area],
            "default_altitude_m": self.default_altitude_m,
            "last_error": self.last_error,
            "advisory_only": True,
        }

    def thermal_jpeg(self, scale: int = 4) -> bytes | None:
        """The latest thermal frame as a false-colour JPEG with detected blobs outlined."""
        if self._latest is None:
            return None
        import cv2

        frame, blobs = self._latest
        image = frame.temperature_c
        ambient = float(np.median(image))
        normalised = np.clip((image - (ambient - 3.0)) / 28.0, 0.0, 1.0)
        colour = cv2.applyColorMap((normalised * 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
        colour = cv2.resize(colour, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        for blob in blobs:
            x0, y0, x1, y1 = (v * scale for v in blob.bbox_px)
            cv2.rectangle(colour, (x0 - 3, y0 - 3), (x1 + 3, y1 + 3), (255, 255, 255), 1)
        ok, encoded = cv2.imencode(".jpg", colour, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return encoded.tobytes() if ok else None

    def accuracy(self) -> dict[str, Any]:
        """Score the candidate list against the scenario's ground-truth heat sources."""
        candidates = self.registry.established()
        used: set[str] = set()
        sources = []
        for source in self.scenario.get("heat_sources", []):
            best = min(
                (c for c in candidates if c.candidate_id not in used),
                key=lambda c: math.hypot(
                    c.east_m - source["east_m"], c.north_m - source["north_m"]
                ),
                default=None,
            )
            error = (
                math.hypot(best.east_m - source["east_m"], best.north_m - source["north_m"])
                if best is not None
                else None
            )
            matched = best is not None and error is not None and error <= MATCH_RADIUS_M
            if matched and best is not None:
                used.add(best.candidate_id)
            sources.append(
                {
                    "id": source["id"],
                    "kind": source["kind"],
                    "expected_detectable": source.get("expected_detectable", True),
                    "detected": matched,
                    "candidate_id": best.candidate_id if matched and best is not None else None,
                    "position_error_m": round(error, 2) if matched and error is not None else None,
                    "category": best.category if matched and best is not None else None,
                }
            )
        unmatched = [c.candidate_id for c in candidates if c.candidate_id not in used]
        return {
            "scenario": self.scenario_id,
            "ground_truth": "heat sources listed in the scenario file",
            "match_radius_m": MATCH_RADIUS_M,
            "position_error_note": "The thermal model renders from the same pose estimate "
            "that geolocation uses, so these errors exclude autopilot pose error (which "
            "moved a Gazebo depth map by about half a metre).",
            "sources": sources,
            "candidates_without_a_source": unmatched,
            "coverage_fraction": round(self.coverage.fraction(), 4),
        }

    # --------------------------------------------------------------------- report

    def report(self) -> dict[str, Any]:
        candidates = self.registry.established()
        by_status = {
            status.value: sum(1 for c in candidates if c.status is status)
            for status in ReviewStatus
        }
        frame = self._latest[0] if self._latest else None
        return {
            "report": "AEGIS ONE search report",
            "generated_at_unix_s": time.time(),
            "scenario": self.scenario_id,
            "provenance": frame.provenance.value if frame else None,
            "thermal_source": frame.source if frame else getattr(self.thermal, "source", None),
            "mission": self.hub.mission.model_dump(mode="json"),
            "search_area_enu_m": [list(p) for p in self.area],
            "coverage_fraction": round(self.coverage.fraction(), 4),
            "thermal_frames_processed": self.frames_processed,
            "candidates_by_status": by_status,
            "candidates": [c.model_dump(mode="json") for c in candidates],
            "human_confirmation": "Candidates are confirmed or dismissed only by the operator. "
            "The vehicle was never commanded toward a candidate.",
            "limitations": LIMITATIONS,
        }

    def save_report(self, folder: Path) -> dict[str, str]:
        report = self.report()
        folder.mkdir(parents=True, exist_ok=True)
        json_path, md_path = folder / "sar_report.json", folder / "sar_report.md"
        json_path.write_text(json.dumps(report, indent=2))
        md_path.write_text(report_markdown(report))
        return {"json": str(json_path), "markdown": str(md_path)}


def report_markdown(report: dict[str, Any]) -> str:
    counts = report["candidates_by_status"]
    generated = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(report["generated_at_unix_s"]))
    lines = [
        "# AEGIS ONE search report",
        "",
        f"- Scenario: `{report['scenario']}`",
        f"- Data provenance: **{report['provenance'] or 'no thermal data'}** "
        f"({report['thermal_source']})",
        f"- Generated: {generated}",
        f"- Mission: {report['mission'].get('name')} - {report['mission'].get('phase')}",
        f"- Search area covered by the thermal camera: {report['coverage_fraction'] * 100:.1f}%",
        f"- Thermal frames processed: {report['thermal_frames_processed']}",
        f"- Candidates: {counts['confirmed']} confirmed, {counts['dismissed']} dismissed, "
        f"{counts['unreviewed']} not yet reviewed",
        "",
        "## Candidates",
        "",
        "| ID | East m | North m | Description | Peak C | Area m2 | Seen | Decision | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in report["candidates"]:
        lines.append(
            f"| {c['candidate_id']} | {c['east_m']:.1f} | {c['north_m']:.1f} | {c['category']} | "
            f"{c['peak_c']:.1f} | {c['area_m2']:.2f} | {c['observations']} | {c['status']} | "
            f"{c['review_note'] or ''} |"
        )
    lines += ["", f"{report['human_confirmation']}", "", "## Limitations", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    return "\n".join(lines) + "\n"
