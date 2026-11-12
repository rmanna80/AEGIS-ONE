"""Security monitoring of AEGIS's own command channel (defensive only).

* A tamper-evident audit log: every command request and its outcome, each record
  chained to the previous one by a SHA-256 hash, so an edited or deleted line is
  detectable afterwards.
* Watching for failed operator authentication and for command requests that do
  not come from this machine.
* A posture check of this installation's configuration.

Nothing here probes, scans or acts on anything outside the AEGIS process.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from aegis.contracts import Severity
from aegis.safety import Finding

GENESIS = "0" * 64


def _digest(previous: str, body: dict[str, Any]) -> str:
    payload = previous + json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    """Append-only JSON-Lines log; record n carries the hash of record n-1."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._previous = GENESIS
        self.count = 0
        self.recent: deque[dict[str, Any]] = deque(maxlen=50)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        body = {"seq": self.count, "t_unix_s": round(time.time(), 3), "event": event, **fields}
        record = {**body, "prev": self._previous, "hash": _digest(self._previous, body)}
        self._previous = record["hash"]
        self.count += 1
        self.recent.append(record)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        return record


def verify_audit_log(path: Path) -> dict[str, Any]:
    """Check a saved audit log's hash chain. Reports the first record that does not fit."""
    previous = GENESIS
    count = 0
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            body = {k: v for k, v in record.items() if k not in ("prev", "hash")}
            ok = (
                record["prev"] == previous
                and record["seq"] == count
                and record["hash"] == _digest(previous, body)
            )
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            return {"valid": False, "records": count, "first_bad_line": number}
        previous = record["hash"]
        count += 1
    return {"valid": True, "records": count, "first_bad_line": None}


def is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SecurityMonitor:
    def __init__(
        self,
        audit: AuditLog,
        failure_threshold: int = 5,
        failure_window_s: float = 60.0,
        remote_latch_s: float = 300.0,
    ) -> None:
        self.audit = audit
        self.failure_threshold = failure_threshold
        self.failure_window_s = failure_window_s
        self.remote_latch_s = remote_latch_s
        self._failures: deque[float] = deque()
        self.failures_total = 0
        self.commands_total = 0
        self._remote: tuple[float, str] | None = None

    def auth_failed(self, client: str | None, path: str) -> None:
        self.failures_total += 1
        self._failures.append(time.monotonic())
        self.audit.append("auth_failed", client=client, path=path)

    def authorised(self, client: str | None, path: str) -> None:
        self.commands_total += 1
        self.audit.append("command_request", client=client, path=path)
        if not is_loopback(client):
            self._remote = (time.monotonic(), str(client))

    def outcome(self, path: str, result: str, **fields: Any) -> None:
        self.audit.append("command_outcome", path=path, result=result, **fields)

    def recent_failures(self) -> int:
        now = time.monotonic()
        while self._failures and now - self._failures[0] > self.failure_window_s:
            self._failures.popleft()
        return len(self._failures)

    def findings(self) -> list[Finding]:
        found: list[Finding] = []
        failures = self.recent_failures()
        if failures >= self.failure_threshold:
            found.append(
                (
                    "security.auth_failures",
                    Severity.WARNING,
                    f"{failures} command requests with a wrong operator token in the last "
                    f"{self.failure_window_s:.0f} s. Someone or something is guessing.",
                )
            )
        if self._remote is not None and time.monotonic() - self._remote[0] <= self.remote_latch_s:
            found.append(
                (
                    "security.remote_command",
                    Severity.WARNING,
                    f"A command request came from another machine ({self._remote[1]}).",
                )
            )
        return found


def posture(
    *,
    operator_token: str | None,
    token_generated: bool,
    px4_url: str,
    cors_origins: list[str],
    fault_injection: bool,
    mode: str,
    host: str = "127.0.0.1",
) -> list[dict[str, str]]:
    """Checks of this installation's configuration. level: ok | info | warning."""
    checks: list[dict[str, str]] = []

    def add(check_id: str, level: str, text: str) -> None:
        checks.append({"id": check_id, "level": level, "text": text})

    if mode != "simulation":
        add("commands", "ok", f"Commands are disabled in {mode} mode.")
    elif token_generated:
        add(
            "operator_token",
            "info",
            "The operator token was generated for this run and printed to the backend log.",
        )
    elif operator_token is not None and len(operator_token) < 24:
        add(
            "operator_token",
            "warning",
            f"The operator token is only {len(operator_token)} characters long. Use a random "
            "value of at least 24 (for example: openssl rand -hex 32).",
        )
    else:
        add("operator_token", "ok", "The operator token is set and long enough.")

    if is_loopback(host):
        add("api_listener", "ok", "The dashboard and API are reachable from this machine only.")
    else:
        add(
            "api_listener",
            "warning",
            f"The dashboard and API listen on {host}: anyone on the network can reach them, "
            "the operator token is the only protection, and it travels unencrypted. Prefer "
            "AEGIS_HOST=127.0.0.1 with an SSH tunnel.",
        )

    mavlink_host = px4_url.split("://", 1)[-1].rsplit(":", 1)[0]
    if is_loopback(mavlink_host):
        add(
            "mavlink_listener", "ok", "The MAVLink listener accepts packets from this machine only."
        )
    else:
        add(
            "mavlink_listener",
            "warning",
            f"The MAVLink listener ({px4_url}) accepts packets from any network interface. "
            "Needed when the simulated autopilot runs on another machine (a companion-computer "
            "setup); otherwise set AEGIS_PX4_URL=udpin://127.0.0.1:14540.",
        )
    add(
        "mavsdk_grpc",
        "warning",
        "The bundled MAVSDK server opens its control port (50051) on every network interface "
        "without authentication, and has no option to restrict it. Keep this machine behind a "
        "firewall; WSL2's default networking does not expose it to the local network.",
    )
    add(
        "mavlink_signing",
        "info",
        "MAVLink messages between AEGIS and the autopilot are not signed. Acceptable on one "
        "machine; a radio link would need MAVLink 2 signing.",
    )
    if any(origin.strip() == "*" for origin in cors_origins):
        add("cors", "warning", "Any website may call this API (CORS is set to *).")
    else:
        add("cors", "ok", "Only the listed dashboard origins may call this API from a browser.")
    if fault_injection:
        add(
            "fault_injection",
            "info",
            "Fault injection is enabled (AEGIS_FAULT_INJECTION). Telemetry can be altered on "
            "purpose by the operator for testing.",
        )
    else:
        add("fault_injection", "ok", "Fault injection is disabled.")
    return checks
