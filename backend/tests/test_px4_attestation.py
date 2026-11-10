"""Simulator attestation logic, exercised with a stubbed MAVSDK system."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aegis.adapters.px4_sitl import PX4SITLAdapter


def adapter_with(results: list[Any]) -> PX4SITLAdapter:
    """An adapter whose SYS_AUTOSTART reads return / raise the given results in order."""

    async def get_param_int(name: str) -> int:
        assert name == "SYS_AUTOSTART"
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return int(result)

    adapter = PX4SITLAdapter()
    adapter._system = SimpleNamespace(param=SimpleNamespace(get_param_int=get_param_int))
    return adapter


async def test_sih_and_gazebo_airframes_are_attested() -> None:
    for autostart, name in ((10040, "sih_quadx"), (4001, "gz_x500")):
        adapter = adapter_with([autostart])
        await adapter._attest_simulator()
        assert adapter.attested and adapter.source == f"px4_sitl:{name}"
        assert adapter.attestation_error is None


async def test_unknown_airframe_is_refused() -> None:
    adapter = adapter_with([4014])  # a real-hardware quadcopter airframe id
    await adapter._attest_simulator()
    assert not adapter.attested
    assert "SYS_AUTOSTART=4014" in (adapter.attestation_error or "")


async def test_failed_read_is_retried_until_it_succeeds() -> None:
    """Regression: PX4 still booting made the one-and-only attempt time out for good."""
    adapter = adapter_with([TimeoutError(), TimeoutError(), 4001])
    await adapter._attest_simulator()
    assert not adapter.attested and "retrying" in (adapter.attestation_error or "")
    await adapter._attest_simulator()
    assert not adapter.attested
    await adapter._attest_simulator()
    assert adapter.attested and adapter.attestation_error is None


async def test_transient_failure_does_not_revoke_an_attested_simulator() -> None:
    adapter = adapter_with([10040, TimeoutError()])
    await adapter._attest_simulator()
    await adapter._attest_simulator()
    assert adapter.attested


async def test_recheck_revokes_when_the_airframe_changes_to_non_simulation() -> None:
    adapter = adapter_with([10040, 4014])
    await adapter._attest_simulator()
    assert adapter.attested
    await adapter._attest_simulator()
    assert not adapter.attested


async def test_recheck_follows_a_simulator_swap() -> None:
    adapter = adapter_with([10040, 4001])
    await adapter._attest_simulator()
    await adapter._attest_simulator()
    assert adapter.source == "px4_sitl:gz_x500"
