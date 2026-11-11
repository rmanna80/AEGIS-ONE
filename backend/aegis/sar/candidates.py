"""Heat-source candidates: observations merged over time, reviewed by the operator.

A candidate starts ``unreviewed``. Only an operator decision (through the
authenticated API) can make it ``confirmed`` or ``dismissed``. Nothing in this
module, or anywhere else in AEGIS, changes that status automatically, and no
status triggers a vehicle command.
"""

from __future__ import annotations

import math
import time
from typing import Any

from pydantic import BaseModel

from aegis.contracts import Provenance
from aegis.sar.detect import HeatBlob, categorise
from aegis.sar.review import ReviewRequest, ReviewStatus


class Candidate(BaseModel):
    candidate_id: str
    east_m: float
    north_m: float
    category: str
    peak_c: float
    ambient_c: float
    area_m2: float
    observations: int
    established: bool  # seen often enough to be shown to the operator
    first_seen_unix_s: float
    last_seen_unix_s: float
    provenance: Provenance
    sensor: str
    status: ReviewStatus = ReviewStatus.UNREVIEWED
    reviewed_at_unix_s: float | None = None
    review_note: str = ""
    position_note: str = "flat-ground estimate from the autopilot pose; expect metres of error"


class CandidateRegistry:
    def __init__(self, merge_radius_m: float = 3.0, min_observations: int = 3) -> None:
        self.merge_radius_m = merge_radius_m
        self.min_observations = min_observations
        self._candidates: list[Candidate] = []
        self._areas: dict[str, list[float]] = {}
        self.events: list[dict[str, Any]] = []  # drained by the service into the flight log

    def observe(
        self, blobs: list[HeatBlob], provenance: Provenance, sensor: str, now: float | None = None
    ) -> None:
        now = time.time() if now is None else now
        for blob in blobs:
            nearest = min(
                self._candidates,
                key=lambda c: math.hypot(c.east_m - blob.east_m, c.north_m - blob.north_m),
                default=None,
            )
            if nearest is not None and (
                math.hypot(nearest.east_m - blob.east_m, nearest.north_m - blob.north_m)
                <= self.merge_radius_m
            ):
                self._merge(nearest, blob, now)
                continue
            if blob.touches_edge:
                continue  # a clipped blob has a biased position; wait for a full view
            candidate = Candidate(
                candidate_id=f"C{len(self._candidates) + 1:03d}",
                east_m=blob.east_m,
                north_m=blob.north_m,
                category=blob.category,
                peak_c=blob.peak_c,
                ambient_c=blob.ambient_c,
                area_m2=blob.area_m2,
                observations=1,
                established=self.min_observations <= 1,
                first_seen_unix_s=now,
                last_seen_unix_s=now,
                provenance=provenance,
                sensor=sensor,
            )
            self._candidates.append(candidate)
            self._areas[candidate.candidate_id] = [blob.area_m2]

    def _merge(self, candidate: Candidate, blob: HeatBlob, now: float) -> None:
        candidate.last_seen_unix_s = now
        if blob.touches_edge:
            return
        n = candidate.observations
        candidate.east_m = (candidate.east_m * n + blob.east_m) / (n + 1)
        candidate.north_m = (candidate.north_m * n + blob.north_m) / (n + 1)
        candidate.observations = n + 1
        candidate.peak_c = max(candidate.peak_c, blob.peak_c)
        candidate.ambient_c = blob.ambient_c
        areas = self._areas[candidate.candidate_id]
        areas.append(blob.area_m2)
        del areas[:-200]
        candidate.area_m2 = sorted(areas)[len(areas) // 2]
        candidate.category = categorise(candidate.area_m2, candidate.peak_c)
        if not candidate.established and candidate.observations >= self.min_observations:
            candidate.established = True
            self.events.append({"event": "candidate", **candidate.model_dump(mode="json")})

    def review(self, candidate_id: str, request: ReviewRequest) -> Candidate | None:
        """Record an OPERATOR decision. Returns None for an unknown candidate."""
        for candidate in self._candidates:
            if candidate.candidate_id == candidate_id and candidate.established:
                candidate.status = request.decision
                candidate.review_note = request.note
                candidate.reviewed_at_unix_s = (
                    None if request.decision is ReviewStatus.UNREVIEWED else time.time()
                )
                self.events.append({"event": "review", **candidate.model_dump(mode="json")})
                return candidate
        return None

    def established(self) -> list[Candidate]:
        return [c for c in self._candidates if c.established]

    def awaiting_review(self) -> int:
        return sum(1 for c in self.established() if c.status is ReviewStatus.UNREVIEWED)
