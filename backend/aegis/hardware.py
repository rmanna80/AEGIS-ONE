"""Hardware interfaces: what AEGIS can attach to, and what has actually been shown to work.

This is the single inventory of adapters. Each entry names the interface it
implements, the kind of data it can carry, and the strongest evidence that exists
for it. "Evidence" is deliberately conservative: an adapter is `hardware-tested`
only if it has run against physical equipment, and nothing here is.

It also defines the operation classes. Physical aircraft operation and
hardware-in-the-loop testing are named so they can be refused explicitly; no code
path implements them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from aegis.contracts import Provenance


class Evidence(StrEnum):
    HARDWARE_TESTED = "hardware-tested"  # run against physical equipment
    VERIFIED_SIMULATION = "verified in simulation"  # run end to end against a simulator
    UNIT_TESTED = "unit-tested only"  # logic tested; never run against the real thing
    NOT_IMPLEMENTED = "not implemented"


class OperationClass(StrEnum):
    SIMULATION = "simulation"
    RECORDED_PLAYBACK = "recorded-data playback"
    PHYSICAL_MONITORING = "physical monitoring (read-only)"
    HARDWARE_IN_THE_LOOP = "hardware-in-the-loop testing"
    PHYSICAL_OPERATION = "physical aircraft operation"


OPERATION_CLASSES: list[dict[str, Any]] = [
    {
        "class": OperationClass.SIMULATION.value,
        "available": True,
        "commands": "operator token, and only after the autopilot proves it is a simulator",
        "how": "AEGIS_MODE=simulation (default)",
    },
    {
        "class": OperationClass.RECORDED_PLAYBACK.value,
        "available": True,
        "commands": "none",
        "how": "AEGIS_MODE=replay",
    },
    {
        "class": OperationClass.PHYSICAL_MONITORING.value,
        "available": True,
        "commands": "none: no command path exists in this mode",
        "how": "AEGIS_MODE=monitor. Data from an autopilot that does not prove it is a "
        "simulator is labelled physical. Never run against real hardware so far.",
    },
    {
        "class": OperationClass.HARDWARE_IN_THE_LOOP.value,
        "available": False,
        "commands": "not applicable",
        "how": "Not implemented. A real flight controller driven by simulated sensors would "
        "be commanded through its real interfaces, so it needs the same gating as flight.",
    },
    {
        "class": OperationClass.PHYSICAL_OPERATION.value,
        "available": False,
        "commands": "not applicable",
        "how": "Not implemented and refused at start-up (AEGIS_MODE=physical). No hardware "
        "mission executor exists.",
    },
]

# kind, name, module path, interface, provenance it can carry, evidence, notes
ADAPTERS: list[dict[str, Any]] = [
    {
        "kind": "vehicle telemetry + mission execution",
        "name": "PX4 over MAVLink (MAVSDK)",
        "implementation": "aegis.adapters.px4_sitl.PX4SITLAdapter",
        "interface": "VehicleTelemetrySource, MissionExecutor",
        "provenance": [Provenance.SIMULATED.value, Provenance.PHYSICAL.value],
        "evidence": Evidence.VERIFIED_SIMULATION.value,
        "notes": "Flown on PX4 SITL (built-in simulator and Gazebo), including from a "
        "Raspberry Pi 5 across a network. Commands are refused unless the autopilot proves "
        "it is a simulator. Never attached to a physical flight controller: the read-only "
        "physical path is unit-tested only. Battery, GNSS, health and mode come from PX4.",
    },
    {
        "kind": "vehicle telemetry",
        "name": "Recorded flight",
        "implementation": "aegis.recording.RecordedTelemetryAdapter",
        "interface": "VehicleTelemetrySource",
        "provenance": [Provenance.REPLAYED.value],
        "evidence": Evidence.VERIFIED_SIMULATION.value,
        "notes": "Replays recordings of simulated flights. No recording of a physical "
        "vehicle exists yet.",
    },
    {
        "kind": "RGB camera",
        "name": "Frame stream over TCP",
        "implementation": "aegis.perception.cameras.StreamCameraAdapter",
        "interface": "CameraSource",
        "provenance": [Provenance.SIMULATED.value, Provenance.REPLAYED.value],
        "evidence": Evidence.VERIFIED_SIMULATION.value,
        "notes": "Fed by the Gazebo camera bridge. Frames declaring physical provenance are "
        "rejected in this release, so a real camera cannot be attached through it yet.",
    },
    {
        "kind": "RGB camera",
        "name": "Video file",
        "implementation": "aegis.perception.cameras.VideoFileCameraAdapter",
        "interface": "CameraSource",
        "provenance": [Provenance.REPLAYED.value],
        "evidence": Evidence.VERIFIED_SIMULATION.value,
        "notes": "Any video file, labelled replayed.",
    },
    {
        "kind": "RGB camera",
        "name": "USB webcam (Video4Linux), bench mount",
        "implementation": "aegis.perception.usb_camera.UsbCameraAdapter",
        "interface": "CameraSource",
        "provenance": [Provenance.PHYSICAL.value],
        "evidence": Evidence.HARDWARE_TESTED.value,
        "notes": "Ran on the owner's Raspberry Pi 5 with a USB webcam during a simulated "
        "flight (2026-11-14 and 15, Demo 1 passed every time): 640x480 MJPG agreed at 30 "
        "frames/s, 7.6 to 15 delivered; 2.0 to 2.6 frames/s through the detector, 231 to "
        "348 ms per detection depending on settings. A bench "
        "camera only: not on any vehicle, its detections are never placed in the world. "
        "Video is not saved unless AEGIS_RECORD_CAMERA_VIDEO=always.",
    },
    {
        "kind": "RGB camera",
        "name": "Raspberry Pi camera module (CSI)",
        "implementation": None,
        "interface": "CameraSource",
        "provenance": [Provenance.PHYSICAL.value],
        "evidence": Evidence.NOT_IMPLEMENTED.value,
        "notes": "No adapter (needs libcamera / Picamera2). No camera module owned yet.",
    },
    {
        "kind": "AI camera / object detector",
        "name": "YOLO (ONNX Runtime, CPU)",
        "implementation": "aegis.perception.yolo_onnx.OnnxYoloBackend",
        "interface": "InferenceBackend",
        "provenance": ["inherits the camera's"],
        "evidence": Evidence.VERIFIED_SIMULATION.value,
        "notes": "Timed on a Raspberry Pi 5 (158 ms median per frame, synthetic input). "
        "Not run on the Pi during a flight. No on-sensor AI camera is supported.",
    },
    {
        "kind": "depth sensor",
        "name": "Software depth camera (ray casting)",
        "implementation": "aegis.navigation.depth.RaycastDepthCamera",
        "interface": "DepthSource",
        "provenance": [Provenance.SIMULATED.value],
        "evidence": Evidence.VERIFIED_SIMULATION.value,
        "notes": "A sensor model, not a sensor. Renders from the pose estimate, so pose "
        "error cancels out of its results.",
    },
    {
        "kind": "depth sensor",
        "name": "Depth stream over TCP",
        "implementation": "aegis.navigation.depth_stream.DepthStreamAdapter",
        "interface": "DepthSource",
        "provenance": [Provenance.SIMULATED.value, Provenance.REPLAYED.value],
        "evidence": Evidence.VERIFIED_SIMULATION.value,
        "notes": "Fed by the Gazebo depth bridge. Camera mounting offset is not modelled. "
        "No physical depth camera has been attached.",
    },
    {
        "kind": "thermal sensor",
        "name": "Software thermal camera",
        "implementation": "aegis.sar.thermal.ThermalCameraModel",
        "interface": "ThermalSource",
        "provenance": [Provenance.SIMULATED.value],
        "evidence": Evidence.VERIFIED_SIMULATION.value,
        "notes": "A sensor model on an ideal stabilised mount; no radiometry. Ran on a "
        "Raspberry Pi 5 during a simulated flight. No physical thermal sensor is supported.",
    },
    {
        "kind": "companion computer health",
        "name": "Device monitor",
        "implementation": "aegis.device.DeviceMonitor",
        "interface": "(none: read by the hub)",
        "provenance": [Provenance.PHYSICAL.value],
        "evidence": Evidence.HARDWARE_TESTED.value,
        "notes": "Read real temperature and firmware power flags on a Raspberry Pi 5. Its "
        "alert thresholds have only been exercised with stand-in values.",
    },
]


def operation_class(mode: str, provenance: Provenance) -> str:
    """Which class the running backend is in, from its mode and what its source proved to be."""
    if mode == "replay" or provenance is Provenance.REPLAYED:
        return OperationClass.RECORDED_PLAYBACK.value
    if provenance is Provenance.PHYSICAL:
        return OperationClass.PHYSICAL_MONITORING.value
    return OperationClass.SIMULATION.value


def inventory() -> dict[str, Any]:
    return {"operation_classes": OPERATION_CLASSES, "adapters": ADAPTERS}
