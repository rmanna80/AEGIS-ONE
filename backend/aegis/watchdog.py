"""Stall watchdog: explains *why* the backend stopped responding.

A background thread watches a heartbeat that the asyncio event loop refreshes.
When the loop goes quiet it logs, once per stall:

* how long the loop has been silent, and whether the watchdog thread itself was
  held up (which points at the whole process or machine, not at Python code
  blocking the loop);
* system load, free memory and swap, and Linux pressure-stall figures;
* the Python stack of every thread, i.e. exactly what the process was doing.

It only reads and logs; it never interferes with the vehicle or the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

BEAT_INTERVAL_S = 0.25
CHECK_INTERVAL_S = 0.5
STALL_AFTER_S = 2.0


def system_snapshot() -> str:
    """One line describing machine load and memory (Linux; empty elsewhere)."""
    parts: list[str] = []
    with contextlib.suppress(OSError):
        load = Path("/proc/loadavg").read_text().split()[:3]
        parts.append(f"load {' '.join(load)}")
    with contextlib.suppress(OSError, ValueError):
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            meminfo[key] = int(value.split()[0]) // 1024  # MB
        parts.append(
            f"memory available {meminfo.get('MemAvailable', 0)} of {meminfo.get('MemTotal', 0)} MB"
            f", swap used {meminfo.get('SwapTotal', 0) - meminfo.get('SwapFree', 0)} MB"
        )
    for resource in ("cpu", "memory", "io"):
        with contextlib.suppress(OSError, IndexError):
            first = Path(f"/proc/pressure/{resource}").read_text().splitlines()[0]
            avg10 = next(f for f in first.split() if f.startswith("avg10="))
            parts.append(f"{resource} pressure {avg10.removeprefix('avg10=')}%")
    return "; ".join(parts)


def memory_available_mb() -> int | None:
    """MemAvailable in MB (Linux), or None when it cannot be read."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return None


def top_memory_processes(count: int = 4) -> str:
    """The processes holding the most memory (resident + swapped), largest first.

    Read from /proc, so it names the culprit when the machine runs out of memory.
    Empty on systems without /proc.
    """
    usage: list[tuple[int, int, str, str]] = []
    with contextlib.suppress(OSError):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields: dict[str, str] = {}
                for line in (entry / "status").read_text().splitlines():
                    key, _, value = line.partition(":")
                    if key in ("Name", "VmRSS", "VmSwap"):
                        fields[key] = value.strip()
                rss = int(fields.get("VmRSS", "0 kB").split()[0]) // 1024
                swap = int(fields.get("VmSwap", "0 kB").split()[0]) // 1024
                if rss + swap < 50:
                    continue
                cmdline = (
                    (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                )
            except (OSError, ValueError, IndexError):
                continue  # the process exited while we were reading it
            usage.append((rss + swap, int(entry.name), fields.get("Name", "?"), cmdline.strip()))
    usage.sort(reverse=True)
    return ", ".join(
        f"{name}[{pid}] {mb} MB ({cmdline[:60]})" if cmdline else f"{name}[{pid}] {mb} MB"
        for mb, pid, name, cmdline in usage[:count]
    )


def thread_stacks(limit: int = 8) -> str:
    names = {t.ident: t.name for t in threading.enumerate()}
    blocks = []
    for ident, frame in sys._current_frames().items():
        name = names.get(ident, "?")
        if name == "aegis-watchdog":
            continue
        stack = "".join(traceback.format_stack(frame, limit=limit))
        blocks.append(f"--- thread {name} ---\n{stack}")
    return "\n".join(blocks)


class StallWatchdog:
    def __init__(self) -> None:
        self._beat = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None
        self.stalls = 0
        self.longest_stall_s = 0.0

    async def start(self) -> None:
        self._beat = time.monotonic()
        self._task = asyncio.create_task(self._heartbeat(), name="watchdog-heartbeat")
        self._thread = threading.Thread(target=self._watch, name="aegis-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _heartbeat(self) -> None:
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(BEAT_INTERVAL_S)

    def _watch(self) -> None:
        reported = False
        last_wake = time.monotonic()
        while not self._stop.wait(CHECK_INTERVAL_S):
            now = time.monotonic()
            overslept = now - last_wake - CHECK_INTERVAL_S  # how late this thread itself woke up
            last_wake = now
            silent = now - self._beat
            if overslept > STALL_AFTER_S:
                # Even this thread could not run: nothing in the process did. That is the
                # operating system (CPU starvation, swapping, a suspended VM), or native
                # code holding Python's lock, not Python code blocking the event loop.
                self.stalls += 1
                self.longest_stall_s = max(self.longest_stall_s, overslept)
                log.error(
                    "BACKEND FROZEN for %.1f s: no thread in the backend ran at all, so the whole "
                    "process or machine stalled.\nSystem now: %s\nLargest processes: %s\n"
                    "Threads now:\n%s",
                    overslept,
                    system_snapshot() or "n/a",
                    top_memory_processes() or "n/a",
                    thread_stacks(limit=4),
                )
                reported = False
                continue
            if silent < STALL_AFTER_S:
                if reported:
                    log.warning("Backend event loop is running again.")
                reported = False
                continue
            self.longest_stall_s = max(self.longest_stall_s, silent)
            if reported:
                continue
            reported = True
            self.stalls += 1
            if overslept > 1.0:
                verdict = (
                    f"the watchdog thread was itself held up for {overslept:.1f} s, so the whole "
                    "process (or machine) was frozen, or native code held Python's lock"
                )
            else:
                verdict = "other threads kept running, so Python code is blocking the event loop"
            log.error(
                "BACKEND STALL: the event loop has been silent for %.1f s; %s.\n"
                "System: %s\nLargest processes: %s\nWhat each thread is doing:\n%s",
                silent,
                verdict,
                system_snapshot() or "n/a",
                top_memory_processes() or "n/a",
                thread_stacks(),
            )
