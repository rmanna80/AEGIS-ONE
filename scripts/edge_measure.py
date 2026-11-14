#!/usr/bin/env python3
"""Measure how this computer is coping while AEGIS runs on it (Raspberry Pi or any Linux).

    .venv/bin/python scripts/edge_measure.py --seconds 120 --peer 192.168.1.7

Run it on the computer that runs the backend, while a flight (or a replay) is in
progress. It samples once a second and reports, for the whole period:

  * processor use, memory available, processor temperature, throttling flags;
  * how late the backend's 10 Hz loop ran;
  * how often fresh autopilot data arrived, and the longest silence;
  * the time an API request takes on this machine;
  * camera frame rate and detection time, when perception is on;
  * network round-trip time to --peer (the simulator PC), when given.

It only reads. Nothing is sent to the vehicle. A measurement that is not available
is reported as not available, with the reason; it is never filled in.

What it cannot measure: the one-way delay from the autopilot to this computer. That
needs both clocks synchronised to better than the delay itself. The round trip to the
peer and the arrival rate of autopilot data are reported instead.

Results are saved under data/generated/edge_measurements/.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def get(base: str, path: str) -> tuple[Any, float]:
    started = time.perf_counter()
    with urllib.request.urlopen(base + path, timeout=5) as response:
        body = json.loads(response.read())
    return body, (time.perf_counter() - started) * 1000.0


def cpu_times() -> tuple[int, int] | None:
    try:
        fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def memory_available_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        pass
    return None


def ping_ms(peer: str) -> float | None:
    try:
        out = subprocess.run(
            ["ping", "-c", "1", "-W", "1", peer], capture_output=True, text=True, timeout=3
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"time[=<]([0-9.]+) ms", out)
    return float(match.group(1)) if match else None


def summary(values: list[float], unit: str, digits: int = 1) -> dict[str, Any]:
    if not values:
        return {"available": False}
    ordered = sorted(values)
    return {
        "available": True,
        "unit": unit,
        "samples": len(values),
        "median": round(statistics.median(values), digits),
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], digits),
        "min": round(ordered[0], digits),
        "max": round(ordered[-1], digits),
    }


def unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--peer", help="address of the simulator PC, for round-trip time")
    parser.add_argument("--label", default="", help="a note stored with the result")
    args = parser.parse_args()

    try:
        system, _ = get(args.api, "/api/system")
    except (urllib.error.URLError, OSError) as exc:
        print(f"Cannot reach the backend at {args.api}: {exc}")
        print("Start it first (bash scripts/run_backend.sh), then run this again.")
        return 2

    series: dict[str, list[float]] = {
        k: []
        for k in (
            "cpu_percent",
            "memory_available_mb",
            "temperature_c",
            "backend_lag_ms",
            "api_round_trip_ms",
            "state_age_ms",
            "fresh_state_interval_ms",
            "ping_ms",
            "camera_fps",
            "inference_ms_p50",
            "inference_ms_p95",
        )
    }
    throttled: set[str] = set()
    connected_samples = samples = 0
    provenances: set[str] = set()
    previous_cpu = cpu_times()
    last_vehicle_time: float | None = None
    last_fresh_at: float | None = None
    perception_on: bool | None = None
    device_seen = False
    errors = 0

    print(
        f"Measuring for {args.seconds:.0f} s against {args.api} "
        f"(mode {system.get('mode')}, data {system.get('provenance')}) ..."
    )
    started = time.monotonic()
    next_slow = started + 1.0
    while time.monotonic() - started < args.seconds:
        tick = time.monotonic()
        try:
            snapshot, round_trip = get(args.api, "/api/state")
        except (urllib.error.URLError, OSError, ValueError):
            errors += 1
            time.sleep(0.2)
            continue
        samples += 1
        series["api_round_trip_ms"].append(round_trip)
        state = snapshot.get("state")
        if state:
            provenances.add(str(state.get("provenance")))
            if state.get("connected"):
                connected_samples += 1
                received = state.get("received_at_unix_s")
                if received:
                    series["state_age_ms"].append(max(0.0, (time.time() - received) * 1000.0))
                vehicle_time = state.get("vehicle_time_s")
                if vehicle_time is not None and vehicle_time != last_vehicle_time:
                    if last_fresh_at is not None:
                        series["fresh_state_interval_ms"].append((tick - last_fresh_at) * 1000.0)
                    last_vehicle_time, last_fresh_at = vehicle_time, tick

        if tick >= next_slow:  # once a second
            next_slow = tick + 1.0
            now_cpu = cpu_times()
            if previous_cpu and now_cpu and now_cpu[0] > previous_cpu[0]:
                total, idle = now_cpu[0] - previous_cpu[0], now_cpu[1] - previous_cpu[1]
                series["cpu_percent"].append(100.0 * (1.0 - idle / total))
            previous_cpu = now_cpu
            memory = memory_available_mb()
            if memory is not None:
                series["memory_available_mb"].append(memory)
            try:
                health, _ = get(args.api, "/api/health")
                lag = health.get("backend_lag_s")
                if lag is not None:
                    series["backend_lag_ms"].append(lag * 1000.0)
                device, _ = get(args.api, "/api/device")
                device_seen = True
                if device.get("temperature_c") is not None:
                    series["temperature_c"].append(device["temperature_c"])
                throttled.update(device.get("throttled") or [])
                perception, _ = get(args.api, "/api/perception")
                perception_on = bool(perception.get("enabled"))
                if perception_on:
                    for key, name in (
                        ("fps", "camera_fps"),
                        ("inference_latency_ms_p50", "inference_ms_p50"),
                        ("inference_latency_ms_p95", "inference_ms_p95"),
                    ):
                        if perception.get(key) is not None:
                            series[name].append(float(perception[key]))
            except (urllib.error.URLError, OSError, ValueError):
                errors += 1
            if args.peer:
                rtt = ping_ms(args.peer)
                if rtt is not None:
                    series["ping_ms"].append(rtt)
        time.sleep(max(0.0, 0.1 - (time.monotonic() - tick)))

    duration = time.monotonic() - started
    fresh = series["fresh_state_interval_ms"]
    results: dict[str, Any] = {
        "cpu_percent_all_cores": summary(series["cpu_percent"], "%")
        if series["cpu_percent"]
        else unavailable("/proc/stat could not be read"),
        "memory_available_mb": summary(series["memory_available_mb"], "MB", 0)
        if series["memory_available_mb"]
        else unavailable("/proc/meminfo could not be read"),
        "temperature_c": summary(series["temperature_c"], "degC")
        if series["temperature_c"]
        else unavailable(
            "this computer reports no processor temperature"
            if device_seen
            else "/api/device did not answer"
        ),
        "throttling_flags_seen": sorted(throttled),
        "backend_loop_lag_ms": summary(series["backend_lag_ms"], "ms")
        if series["backend_lag_ms"]
        else unavailable("/api/health reported no loop lag"),
        "api_round_trip_ms": summary(series["api_round_trip_ms"], "ms", 2),
        "vehicle_state_age_ms": summary(series["state_age_ms"], "ms")
        if series["state_age_ms"]
        else unavailable("no vehicle was connected"),
        "fresh_autopilot_data": {
            **(
                summary(fresh, "ms between updates")
                if fresh
                else unavailable("no vehicle was connected, or its clock never advanced")
            ),
            "updates_per_second": round(len(fresh) / duration, 2) if fresh else None,
            "longest_silence_ms": round(max(fresh), 0) if fresh else None,
            "note": "Sampled at about 10 Hz, so rates above 10 per second cannot be seen.",
        },
        "network_round_trip_to_peer_ms": summary(series["ping_ms"], "ms", 2)
        if series["ping_ms"]
        else unavailable(
            "no --peer given"
            if not args.peer
            else "the ping command is not installed"
            if shutil.which("ping") is None
            else f"{args.peer} did not answer a ping"
        ),
        "camera_frame_rate": summary(series["camera_fps"], "frames/s")
        if series["camera_fps"]
        else unavailable(
            "perception is off (AEGIS_PERCEPTION=false)"
            if perception_on is False
            else "no frames were processed"
        ),
        "inference_latency_ms_p50": summary(series["inference_ms_p50"], "ms")
        if series["inference_ms_p50"]
        else unavailable("perception is off or idle"),
        "inference_latency_ms_p95": summary(series["inference_ms_p95"], "ms")
        if series["inference_ms_p95"]
        else unavailable("perception is off or idle"),
        "one_way_vehicle_to_computer_delay": unavailable(
            "needs clocks synchronised better than the delay; not attempted"
        ),
    }
    document = {
        "schema": "aegis.edge-measurement/1",
        "label": args.label,
        "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_s": round(duration, 1),
        "host": {
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "backend": {
            k: system.get(k)
            for k in ("version", "mode", "provenance", "source", "operation_class", "scenario_id")
        },
        "data_provenance_seen": sorted(provenances),
        "vehicle_connected_fraction": round(connected_samples / samples, 3) if samples else None,
        "samples": samples,
        "request_errors": errors,
        "results": results,
    }
    folder = REPO_ROOT / "data/generated/edge_measurements"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"edge_{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(document, indent=2))

    print(
        f"\n{duration:.0f} s, {samples} samples, vehicle connected "
        f"{100 * (connected_samples / samples if samples else 0):.0f}% of the time, "
        f"data: {', '.join(sorted(provenances)) or 'none'}"
    )
    for name, value in results.items():
        if isinstance(value, list):
            print(f"  {name:36s} {', '.join(value) or 'none'}")
        elif value.get("available"):
            extra = ""
            if "updates_per_second" in value:
                extra = f"  ({value['updates_per_second']} per second)"
            print(
                f"  {name:36s} median {value['median']}  p95 {value['p95']}  "
                f"max {value['max']} {value['unit']}{extra}"
            )
        else:
            print(f"  {name:36s} not available: {value.get('reason')}")
    print(f"\nsaved: {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
