"""Health of the computer AEGIS runs on (a desktop, or a Raspberry Pi companion computer).

Reads temperature, load, memory, disk and, on a Raspberry Pi, the firmware's
under-voltage and throttling flags. A companion computer that is overheating or
browning out drops frames and telemetry, so these become operator alerts. Read-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from aegis.contracts import Severity
from aegis.safety import Finding

# `vcgencmd get_throttled` bits (Raspberry Pi firmware).
THROTTLE_BITS = {
    0: "under-voltage now",
    1: "CPU frequency capped now",
    2: "throttled now",
    3: "soft temperature limit active now",
    16: "under-voltage has occurred since boot",
    17: "CPU frequency capping has occurred since boot",
    18: "throttling has occurred since boot",
    19: "soft temperature limit has occurred since boot",
}
HOT_C = 80.0  # a Raspberry Pi 5 starts limiting itself here
CRITICAL_C = 85.0  # and throttles hard here
DISK_LOW_MB = 1024


def decode_throttled(value: int) -> list[str]:
    return [text for bit, text in THROTTLE_BITS.items() if value & (1 << bit)]


def read_model() -> str | None:
    with contextlib.suppress(OSError):
        return Path("/proc/device-tree/model").read_text().strip("\x00\n ") or None
    return None


def read_temperature_c() -> float | None:
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        with contextlib.suppress(OSError, ValueError):
            return int(zone.read_text().strip()) / 1000.0
    return None


def read_throttled() -> int | None:
    """Raspberry Pi firmware flags, or None where `vcgencmd` does not exist."""
    if shutil.which("vcgencmd") is None:
        return None
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3, check=False
        ).stdout
        return int(out.strip().split("=", 1)[1], 16)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


class DeviceMonitor:
    def __init__(self, data_dir: Path, interval_s: float = 5.0) -> None:
        self.data_dir = data_dir
        self.interval_s = interval_s
        self.model = read_model()
        self.is_raspberry_pi = bool(self.model and "raspberry pi" in self.model.lower())
        self.snapshot: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None
        self.refresh()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="device-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.refresh)

    def refresh(self) -> dict[str, Any]:
        throttled = read_throttled()
        disk_free_mb: int | None = None
        with contextlib.suppress(OSError):
            folder = self.data_dir if self.data_dir.exists() else Path.cwd()
            disk_free_mb = shutil.disk_usage(folder).free // (1024 * 1024)
        load: float | None = None
        with contextlib.suppress(OSError):
            load = os.getloadavg()[0]
        self.snapshot = {
            "model": self.model,
            "is_raspberry_pi": self.is_raspberry_pi,
            "machine": os.uname().machine,
            "cpu_count": os.cpu_count(),
            "temperature_c": read_temperature_c(),
            "load_1min": None if load is None else round(load, 2),
            "disk_free_mb": disk_free_mb,
            "throttled_raw": None if throttled is None else f"0x{throttled:x}",
            "throttled": None if throttled is None else decode_throttled(throttled),
            "read_at_unix_s": time.time(),
        }
        return self.snapshot

    def findings(self) -> list[Finding]:
        s = self.snapshot
        found: list[Finding] = []
        temperature = s.get("temperature_c")
        if temperature is not None and temperature >= CRITICAL_C:
            found.append(
                (
                    "device.overheating",
                    Severity.CRITICAL,
                    f"This computer's processor is at {temperature:.0f} C and is throttling. "
                    "Expect dropped frames and late telemetry. Improve cooling.",
                )
            )
        elif temperature is not None and temperature >= HOT_C:
            found.append(
                (
                    "device.hot",
                    Severity.WARNING,
                    f"This computer's processor is at {temperature:.0f} C, close to throttling.",
                )
            )
        flags = s.get("throttled") or []
        if "under-voltage now" in flags:
            found.append(
                (
                    "device.undervoltage",
                    Severity.CRITICAL,
                    "The power supply cannot keep this computer's voltage up. It may slow down "
                    "or reset. Use the official 27 W supply for a Raspberry Pi 5.",
                )
            )
        elif "under-voltage has occurred since boot" in flags:
            found.append(
                (
                    "device.undervoltage_seen",
                    Severity.WARNING,
                    "This computer's supply voltage dipped at some point since it started.",
                )
            )
        if "throttled now" in flags or "CPU frequency capped now" in flags:
            found.append(
                ("device.throttled", Severity.WARNING, "This computer is running below full speed.")
            )
        disk = s.get("disk_free_mb")
        if disk is not None and disk < DISK_LOW_MB:
            found.append(
                (
                    "device.disk_low",
                    Severity.WARNING,
                    f"Only {disk} MB of storage is free; recordings will stop when it runs out.",
                )
            )
        return found
