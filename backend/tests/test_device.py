"""Device health monitor (the Raspberry Pi specifics are exercised with stand-in values)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aegis import device
from aegis.api import create_app
from aegis.config import Settings
from aegis.contracts import Severity
from aegis.device import DeviceMonitor, decode_throttled
from aegis.hub import TelemetryHub
from aegis.security import posture
from test_api import TOKEN, FakeVehicle


def test_throttle_flags_decode() -> None:
    assert decode_throttled(0x0) == []
    assert decode_throttled(0x50005) == [
        "under-voltage now",
        "throttled now",
        "under-voltage has occurred since boot",
        "throttling has occurred since boot",
    ]
    assert decode_throttled(0x80008) == [
        "soft temperature limit active now",
        "soft temperature limit has occurred since boot",
    ]


def monitor_with(tmp_path: Path, **values: object) -> DeviceMonitor:
    monitor = DeviceMonitor(tmp_path)
    monitor.snapshot = {**monitor.snapshot, **values}
    return monitor


def test_alerts_for_heat_power_and_storage(tmp_path: Path) -> None:
    healthy = monitor_with(tmp_path, temperature_c=52.0, throttled=[], disk_free_mb=50_000)
    assert healthy.findings() == []
    hot = monitor_with(tmp_path, temperature_c=81.0, throttled=[], disk_free_mb=50_000)
    assert [(c, s) for c, s, _ in hot.findings()] == [("device.hot", Severity.WARNING)]
    cooking = monitor_with(tmp_path, temperature_c=86.0, throttled=[], disk_free_mb=50_000)
    assert cooking.findings()[0][:2] == ("device.overheating", Severity.CRITICAL)
    brownout = monitor_with(
        tmp_path, temperature_c=50.0, throttled=decode_throttled(0x50005), disk_free_mb=50_000
    )
    assert [c for c, _, _ in brownout.findings()] == ["device.undervoltage", "device.throttled"]
    earlier = monitor_with(
        tmp_path, temperature_c=50.0, throttled=decode_throttled(0x10000), disk_free_mb=50_000
    )
    assert [c for c, _, _ in earlier.findings()] == ["device.undervoltage_seen"]
    full = monitor_with(tmp_path, temperature_c=50.0, throttled=None, disk_free_mb=200)
    assert [c for c, _, _ in full.findings()] == ["device.disk_low"]
    unknown = monitor_with(tmp_path, temperature_c=None, throttled=None, disk_free_mb=None)
    assert unknown.findings() == []  # a machine that reports nothing raises nothing


def test_reads_this_machine_without_raspberry_pi_tools(tmp_path: Path, monkeypatch: object) -> None:
    monitor = DeviceMonitor(tmp_path)
    snapshot = monitor.refresh()
    assert snapshot["cpu_count"] and snapshot["disk_free_mb"] is not None
    assert snapshot["machine"]
    if device.shutil.which("vcgencmd") is None:
        assert snapshot["throttled"] is None and snapshot["throttled_raw"] is None


def test_device_api_and_network_listener_posture(tmp_path: Path) -> None:
    vehicle = FakeVehicle()
    settings = Settings(
        operator_token=TOKEN, record_flights=False, data_dir=tmp_path, frontend_dist=tmp_path / "x"
    )
    with TestClient(create_app(settings, TelemetryHub(vehicle, vehicle))) as client:
        assert "temperature_c" in client.get("/api/device").json()
        assert "device" in client.get("/api/security").json()

    def level(host: str) -> str:
        checks = posture(
            operator_token="x" * 40,
            token_generated=False,
            px4_url="udpin://127.0.0.1:14540",
            cors_origins=[],
            fault_injection=False,
            mode="simulation",
            host=host,
        )
        return next(c["level"] for c in checks if c["id"] == "api_listener")

    assert level("127.0.0.1") == "ok" and level("0.0.0.0") == "warning"
