"""The stall watchdog must say what blocked the event loop."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

import aegis.watchdog as module
from aegis.watchdog import StallWatchdog, system_snapshot, thread_stacks


def blocking_helper_named_for_the_test() -> None:
    time.sleep(1.2)  # synchronous sleep on the event loop: the classic mistake


async def test_blocked_event_loop_is_reported_with_the_guilty_stack(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(module, "STALL_AFTER_S", 0.4)
    monkeypatch.setattr(module, "CHECK_INTERVAL_S", 0.1)
    monkeypatch.setattr(module, "BEAT_INTERVAL_S", 0.05)
    watchdog = StallWatchdog()
    await watchdog.start()
    await asyncio.sleep(0.3)
    assert watchdog.stalls == 0  # a healthy loop is not reported

    with caplog.at_level(logging.WARNING, logger="aegis.watchdog"):
        blocking_helper_named_for_the_test()
        await asyncio.sleep(0.4)
    await watchdog.stop()

    assert watchdog.stalls == 1 and watchdog.longest_stall_s >= 0.4
    report = next(r.getMessage() for r in caplog.records if "BACKEND STALL" in r.getMessage())
    assert "blocking_helper_named_for_the_test" in report  # the stack names the culprit
    assert "Python code is blocking the event loop" in report
    assert any("running again" in r.getMessage() for r in caplog.records)


def test_snapshots_never_raise() -> None:
    assert isinstance(system_snapshot(), str)
    assert "thread" in thread_stacks()
