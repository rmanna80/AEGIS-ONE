"""Simulator attestation logic, exercised with a stubbed MAVSDK system."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aegis.adapters.px4_sitl import PX4SITLAdapter

SIH = {"SYS_AUTOSTART": 10040, "SIM_BAT_ENABLE": 1}
GAZEBO = {"SYS_AUTOSTART": 4001, "SIM_BAT_ENABLE": 1, "SIM_GZ_EN": 1}
GAZEBO_CAMERA = {**GAZEBO, "SYS_AUTOSTART": 4010}
# A real Pixhawk with PX4's "Generic Quadcopter" airframe: same number as gz_x500,
# and no simulation modules in the firmware.
REAL_GENERIC_QUAD = {"SYS_AUTOSTART": 4001}


class FakeAutopilot:
    """Parameter server stub. ``params`` can be swapped to model a restart."""

    def __init__(self, params: dict[str, int]) -> None:
        self.params = params
        self.fail_next = 0

    async def get_param_int(self, name: str) -> int:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TimeoutError
        if name not in self.params:
            raise TimeoutError  # PX4 does not answer for unknown parameters
        return self.params[name]


def adapter_for(params: dict[str, int]) -> tuple[PX4SITLAdapter, FakeAutopilot]:
    autopilot = FakeAutopilot(dict(params))
    adapter = PX4SITLAdapter()
    adapter._system = SimpleNamespace(param=autopilot)
    return adapter, autopilot


async def test_simulators_are_attested() -> None:
    for params, name in (
        (SIH, "sih_quadx"),
        (GAZEBO, "gz_x500"),
        (GAZEBO_CAMERA, "gz_x500_mono_cam"),
    ):
        adapter, _ = adapter_for(params)
        await adapter._attest_simulator()
        assert adapter.attested and adapter.source == f"px4_sitl:{name}"
        assert adapter.attestation_error is None


async def test_real_hardware_sharing_a_simulation_airframe_number_is_refused() -> None:
    """Regression: SYS_AUTOSTART=4001 alone used to be accepted as 'gz_x500'."""
    adapter, _ = adapter_for(REAL_GENERIC_QUAD)
    await adapter._attest_simulator()
    assert not adapter.attested
    assert "may be real hardware" in (adapter.attestation_error or "")


async def test_gazebo_airframe_without_gazebo_bridge_enabled_is_refused() -> None:
    adapter, _ = adapter_for({**GAZEBO, "SIM_GZ_EN": 0})
    await adapter._attest_simulator()
    assert not adapter.attested
    assert "SIM_GZ_EN=0" in (adapter.attestation_error or "")


async def test_unknown_airframe_is_refused() -> None:
    adapter, _ = adapter_for({"SYS_AUTOSTART": 6001, "SIM_BAT_ENABLE": 1})
    await adapter._attest_simulator()
    assert not adapter.attested
    assert "SYS_AUTOSTART=6001" in (adapter.attestation_error or "")


async def test_failed_read_is_retried_until_it_succeeds() -> None:
    """Regression: PX4 still booting made the one-and-only attempt time out for good."""
    adapter, autopilot = adapter_for(GAZEBO)
    autopilot.fail_next = 2
    await adapter._attest_simulator()
    assert not adapter.attested and "retrying" in (adapter.attestation_error or "")
    await adapter._attest_simulator()
    assert not adapter.attested
    await adapter._attest_simulator()
    assert adapter.attested and adapter.attestation_error is None


async def test_transient_failure_does_not_revoke_an_attested_simulator() -> None:
    adapter, autopilot = adapter_for(SIH)
    await adapter._attest_simulator()
    autopilot.fail_next = 1
    await adapter._attest_simulator()
    assert adapter.attested


async def test_recheck_revokes_when_the_vehicle_is_no_longer_a_simulator() -> None:
    adapter, autopilot = adapter_for(GAZEBO)
    await adapter._attest_simulator()
    assert adapter.attested
    autopilot.params = dict(REAL_GENERIC_QUAD)
    # Same airframe number: the cached name must not short-circuit forever.
    adapter._attested_airframe = None  # what a link drop does
    await adapter._attest_simulator()
    assert not adapter.attested


async def test_recheck_follows_a_simulator_swap() -> None:
    adapter, autopilot = adapter_for(SIH)
    await adapter._attest_simulator()
    autopilot.params = dict(GAZEBO)
    await adapter._attest_simulator()
    assert adapter.source == "px4_sitl:gz_x500"


# ----------------------------------------------------- link state under slow simulation


def feed_clock(adapter: PX4SITLAdapter, wall_start: float, factor: float, seconds: float) -> float:
    """Feed attitude timestamps as a simulator running at ``factor`` x real time would."""
    wall = wall_start
    for i in range(int(seconds * 10) + 1):
        wall = wall_start + i * 0.1
        adapter._note_vehicle_clock(wall, 100.0 + i * 0.1 * factor)
    return wall


def test_real_time_factor_is_measured_from_the_vehicle_clock() -> None:
    adapter = PX4SITLAdapter()
    assert adapter.real_time_factor is None
    feed_clock(adapter, 1000.0, factor=0.32, seconds=4.0)
    assert adapter.real_time_factor == pytest.approx(0.32, abs=0.01)
    assert adapter._link_timeout_s() == pytest.approx(3.0 / 0.32, rel=0.05)

    fast = PX4SITLAdapter()
    feed_clock(fast, 1000.0, factor=1.0, seconds=4.0)
    assert fast.real_time_factor == pytest.approx(1.0, abs=0.01)
    assert fast._link_timeout_s() == pytest.approx(3.0)

    faster = PX4SITLAdapter()  # faster than real time never shortens the timeout
    feed_clock(faster, 1000.0, factor=4.0, seconds=4.0)
    assert faster._link_timeout_s() == pytest.approx(3.0)


def test_slow_simulation_does_not_look_like_a_lost_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: Gazebo at ~0.3x real time made the link flap and raised link.lost."""
    import aegis.adapters.px4_sitl as module

    now = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    adapter = PX4SITLAdapter()
    assert adapter._connected() is False  # nothing received yet

    now[0] = feed_clock(adapter, 1000.0, factor=0.3, seconds=5.0)
    adapter._last_rx = now[0]
    now[0] += 5.0  # 5 s of wall-clock silence = 1.5 s of simulation: still healthy
    assert adapter._connected() is True
    now[0] += 6.0  # 11 s wall = 3.3 s of simulation: now it really is lost
    assert adapter._connected() is False

    realtime = PX4SITLAdapter()
    now[0] = feed_clock(realtime, 2000.0, factor=1.0, seconds=5.0)
    realtime._last_rx = now[0]
    now[0] += 3.5
    assert realtime._connected() is False  # at 1x the usual 3 s still applies


def test_vehicle_clock_reset_restarts_the_estimate() -> None:
    adapter = PX4SITLAdapter()
    feed_clock(adapter, 1000.0, factor=1.0, seconds=3.0)
    adapter._note_vehicle_clock(1004.0, 1.0)  # autopilot rebooted: clock went backwards
    assert adapter.real_time_factor is None
