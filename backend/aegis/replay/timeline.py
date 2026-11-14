"""Timeline of a recording: the events an investigator scrubs between."""

from __future__ import annotations

from typing import Any


def build_timeline(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Markers from a recording's records (alerts, mission phases, injected faults,
    link gaps, search events), in time order. Times are recording seconds."""
    events: list[dict[str, Any]] = []
    faults: tuple[str, ...] = ()
    previous_state_t: float | None = None
    for record in records:
        t, kind, data = float(record["t"]), record["kind"], record["data"]
        if kind == "alert":
            events.append(
                {
                    "t": t,
                    "kind": "alert",
                    "severity": data["severity"],
                    "label": f"{data['code']} {'raised' if data['active'] else 'cleared'}",
                    "detail": data["message"],
                }
            )
        elif kind == "mission":
            events.append(
                {
                    "t": t,
                    "kind": "mission",
                    "label": f"mission {data['phase']}",
                    "detail": data.get("message", ""),
                }
            )
        elif kind == "fault":
            events.append(
                {
                    "t": t,
                    "kind": "fault",
                    "label": f"fault injected: {data.get('kind')} for {data.get('duration_s')} s",
                    "detail": "Injected into AEGIS's copy of the telemetry for testing.",
                }
            )
        elif kind == "sar":
            events.append(
                {
                    "t": t,
                    "kind": "search",
                    "label": f"{data.get('event')} {data.get('candidate_id', '')}".strip(),
                    "detail": data.get("category", ""),
                }
            )
        elif kind == "state":
            if previous_state_t is not None and t - previous_state_t > 1.0:
                events.append(
                    {
                        "t": previous_state_t,
                        "kind": "gap",
                        "label": f"no vehicle data for {t - previous_state_t:.1f} s",
                        "detail": "State is recorded only while the vehicle link is up.",
                    }
                )
            previous_state_t = t
            now = tuple(data.get("injected_faults") or ())
            if now != faults:
                started = sorted(set(now) - set(faults))
                ended = sorted(set(faults) - set(now))
                if started:
                    events.append(
                        {
                            "t": t,
                            "kind": "fault",
                            "label": "fault injected: " + ", ".join(started),
                            "detail": "",
                        }
                    )
                if ended:
                    events.append(
                        {
                            "t": t,
                            "kind": "fault",
                            "label": "fault ended: " + ", ".join(ended),
                            "detail": "",
                        }
                    )
                faults = now
    events.sort(key=lambda e: e["t"])
    return events
