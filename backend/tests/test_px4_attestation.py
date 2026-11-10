"""Simulator attestation logic, exercised with a stubbed MAVSDK system."""

from __future__ import annotations

from types import SimpleNamespace

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
