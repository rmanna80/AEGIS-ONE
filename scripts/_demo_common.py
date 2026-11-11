"""Shared helpers for the demo scripts (HTTP calls to the AEGIS API, reports)."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TERMINAL = ("completed", "failed", "stalled", "cancelled", "blocked", "rejected")


class Api:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base, self.token = base, token

    def call(self, path: str, body: Any = None, post: bool = False, check: bool = True) -> Any:
        method = "POST" if (post or body is not None) else "GET"
        request = urllib.request.Request(self.base + path, method=method)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            request.add_header("Content-Type", "application/json")
        elif post:
            data = b""
        if self.token:
            request.add_header("X-Aegis-Operator-Token", self.token)
        try:
            with urllib.request.urlopen(request, data=data, timeout=120) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()
            if check:
                raise SystemExit(f"{path} -> HTTP {exc.code}: {detail}") from exc
            return {"http_error": exc.code, "detail": detail}


def require_simulator(api: Api) -> dict[str, Any]:
    system: dict[str, Any] = api.call("/api/system")
    print(f"mode={system['mode']} provenance={system['provenance']} source={system['source']}")
    if system["provenance"] != "simulated" or not system.get("simulator_attested"):
        reason = system.get("attestation_error") or f"provenance is {system['provenance']}"
        raise SystemExit(
            f"Refusing to run: not attached to an attested simulator.\nReason: {reason}"
        )
    return system


class Flight:
    """Follows a mission to a terminal phase, collecting the track and critical alerts."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.track: list[tuple[float, float, float, float]] = []  # t, east, north, up
        self.critical: list[str] = []
        self._seen: set[str] = set()
        self.started = time.monotonic()
        self.status: dict[str, Any] = {}
        self._last_line = ""

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def poll(self) -> dict[str, Any]:
        snap: dict[str, Any] = self.api.call("/api/state")
        self.status = snap["mission"]
        state = snap["state"]
        if state and state["position_enu_m"]:
            p = state["position_enu_m"]
            self.track.append((self.elapsed(), p["x"], p["y"], p["z"]))
        for alert in snap["alerts"]:
            if alert["severity"] == "critical" and alert["code"] not in self._seen:
                self._seen.add(alert["code"])
                detail = (
                    f"{alert['code']} at {self.elapsed():.1f}s ({self.status['phase']}): "
                    f"{alert['message']}"
                )
                self.critical.append(detail)
                print(f"[{self.elapsed():6.1f}s] CRITICAL ALERT {detail}")
        line = f"{self.status['phase']:>10}  {self.status['message']}"
        if line != self._last_line:
            print(f"[{self.elapsed():6.1f}s] {line}")
            self._last_line = line
        return snap

    def follow(self, timeout_s: float, until: tuple[str, ...] = TERMINAL) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.poll()
            if self.status["phase"] in until:
                break
            time.sleep(0.25)
        return str(self.status["phase"])

    def landed_at_home(self) -> bool:
        state = self.api.call("/api/state")["state"]
        p = state["position_enu_m"]
        return math.hypot(p["x"], p["y"]) < 3.0 and state["armed"] is False


def write_report(
    name: str, report: dict[str, Any], criteria: dict[str, bool], passed: bool
) -> None:
    out_dir = REPO_ROOT / "data" / "generated" / "demo_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps({**report, "criteria": criteria, "passed": passed}, indent=2))
    for criterion, ok in criteria.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {criterion}")
    print(f"{'PASSED' if passed else 'FAILED'} - report: {out.relative_to(REPO_ROOT)}")
