# Hardware readiness (Phase 8)

Status: **no physical flight hardware has been connected, and physical flight control does
not exist in this software.** This document says what the software can attach to, what
evidence exists for each interface, what has not been verified, and how to measure it.

The live inventory is served at `GET /api/hardware` (source: `backend/aegis/hardware.py`).

## 1. Operation classes

The backend is always in exactly one class, shown in `/api/system` as `operation_class`.

| Class | Available | Commands | How |
| --- | --- | --- | --- |
| Simulation | yes | Operator token, and only after the autopilot proves it is a simulator | `AEGIS_MODE=simulation` (default) |
| Recorded-data playback | yes | none | `AEGIS_MODE=replay` |
| Physical monitoring (read-only) | yes, never exercised | none; no command path exists | `AEGIS_MODE=monitor` |
| Hardware-in-the-loop | **no** | | Not implemented |
| Physical aircraft operation | **no** | | Not implemented; `AEGIS_MODE=physical` stops the backend at start-up |

How the classes are kept apart:

- **By evidence, not by configuration.** Data is labelled `simulated` only after the autopilot
  answers a parameter query that only PX4's simulator build answers. An autopilot that
  responds without that proof is labelled `physical`, and from then on every command is
  refused for the life of the process. Before either, the label is `unknown`.
- **Monitor and replay modes have no mission executor.** The command endpoints return 403
  before authentication is even checked.
- **Fault injection** is off unless `AEGIS_FAULT_INJECTION=true`, is refused unless the vehicle
  is an attested simulator, alters only AEGIS's copy of the telemetry, and labels every
  altered snapshot.
- **AEGIS never bypasses the flight controller.** It uploads missions and requests mode
  changes through MAVLink; PX4's own failsafes, geofence and arming checks stay in charge.

Hardware-in-the-loop is listed separately from simulation on purpose: a real flight
controller driven by simulated sensors is commanded through its real interfaces and can
spin real motors. It needs the same gating as flight and has none yet.

## 2. Interface audit

| Hardware | Interface (Protocol) | Implementations | Strongest evidence |
| --- | --- | --- | --- |
| Flight controller | `VehicleTelemetrySource`, `MissionExecutor` | PX4 over MAVLink (MAVSDK) | Verified in simulation; physical read-only path unit-tested only |
| Recorded flight | `VehicleTelemetrySource` | `RecordedTelemetryAdapter` | Verified in simulation |
| RGB camera | `CameraSource` | TCP frame stream (Gazebo bridge), video file | Verified in simulation |
| USB webcam (physical, bench) | `CameraSource` | `UsbCameraAdapter` (Video4Linux) | Hardware-tested: ran on the owner's Pi 5 during a simulated flight |
| AI camera / detector | `InferenceBackend` | YOLO via ONNX Runtime (CPU) | Verified in simulation; timed on the Pi 5 with synthetic input |
| Depth sensor | `DepthSource` | Software ray caster, TCP depth stream (Gazebo) | Verified in simulation |
| Thermal sensor | `ThermalSource` | Software thermal model | Verified in simulation |
| GNSS | none of its own | Arrives inside the autopilot's telemetry | Simulated GNSS only |
| Battery | none of its own | Arrives inside the autopilot's telemetry | Simulated battery model only |
| Telemetry radio | none | UDP MAVLink over Ethernet/Wi-Fi | LAN only; no radio, no link-quality reading |
| Edge computer | none (read by the hub) | `DeviceMonitor` | Hardware-tested on a Raspberry Pi 5 |

Findings:

1. **The separation holds where it matters.** Planning, safety checks, mapping, search, the
   fleet logic, the anomaly monitor, REPLAY and TRUST depend on the Protocols and the data
   contracts. The PX4 adapter is constructed in one place (`api.py`).
2. **Optional capabilities are reached by attribute name, not declared.** `attested`,
   `return_altitude_m`, `frame_origin`, `last_command_at`, `messages` and the playback fields
   are read with `getattr`/`hasattr`. A new flight-controller adapter would work but would
   silently lack them. They should become a declared capability set before a second adapter
   is written. Not changed in this iteration: there is no second adapter to design against.
3. **The adapter is named for the simulator** (`PX4SITLAdapter`, module `px4_sitl`) although
   it is the generic PX4/MAVLink adapter with an attestation gate. Renaming touches every
   recording's `source` string, so it is deferred and noted.
4. **GNSS, battery and radio have no adapters because nothing reads them directly.** That is
   correct for a companion computer behind an autopilot. It does mean AEGIS cannot see GNSS
   signal quality beyond fix type and satellite count, cell voltages, or radio link quality.
5. **One physical sensor adapter now exists: a USB webcam, as a bench camera.** It is
   labelled `physical`, it is never treated as mounted on the vehicle, and its video is not
   saved by default. Frames that merely *claim* to be physical over the network frame stream
   are still refused: over a socket the label is only the sender's say-so, whereas the USB
   adapter opens the device itself. No Pi camera module (CSI) adapter.
6. **Camera mounting geometry is not modelled** for depth (the sensor is assumed at the
   vehicle origin) and thermal assumes an ideal stabilised mount.

## 3. Raspberry Pi 5 integration review

Device: Raspberry Pi 5, Raspberry Pi OS (Debian 13 "Trixie", 64-bit), Python 3.13.5.
Topology tested: backend on the Pi, PX4 SITL on a PC, MAVLink over the LAN.

### Evidence that exists (reported by the owner from the device)

| What | Result | Source |
| --- | --- | --- |
| Install with `scripts/setup_pi.sh` | Completed | Owner's terminal output |
| Benchmark, each workload alone, synthetic input | Detection 158 ms, thermal 15 ms, mapping 21 ms median per frame | `scripts/pi_benchmark.py` |
| Temperature across the benchmark | 49.6 to 54.0 degC, no throttling or under-voltage flags | same |
| Demo 1 (survey) with the Pi in the loop | 5 of 5, 140 s | `scripts/demo1_survey.py` |
| Demo 3 (thermal search) with the Pi in the loop | 8 of 8, 223 s | `scripts/demo3_sar.py` |
| Link loss when the simulator was restarted without the companion link | Detected; flight stalled at waypoint 4 of 5 | Backend log |
| Demo 1 with the USB webcam and detector running on the Pi | 5 of 5 | `demo1_survey.py`, 2026-11-14 |
| CPU during that flight (all 4 cores) | 92% median, 98% p95, 99.8% max | `edge_measure.py`, 90 s |
| Temperature during that flight | 59.5 °C median, 60.6 °C max; no throttling or under-voltage flags | same |
| Memory available | 5.9 GB median of 8 GB | same |
| Backend loop lag | 1 ms median, 9 ms max | same |
| Age of the vehicle data when read | 57 ms median, 118 ms max | same |
| Detector on live webcam frames, in flight | 2.7 frames/s processed (median); 335 ms median, 387 ms p95 per frame, with the default 2 threads | same |
| MAVSDK port closed to the network | From the PC: connected before `lock_mavsdk_port.sh`, timed out after | `nc -zv`, 2026-11-14 |

| Detector settings compared in flight (2 threads uncapped, 4 threads, 2 threads capped at 2 frames/s) | CPU 92% / 99.5% / 78%; frames detected 2.6 / 2.0 / 2.0 per second; longest gap in fresh vehicle data 205 / 505 / 202 ms | `edge_measure.py`, 2026-11-15, one run each; details in `docs/raspberry-pi/README.md` |
| Network round trip Pi to PC | 4.8 ms median, 16 ms p95 (rises to 139 ms when the Pi's CPU is saturated) | same |
| Webcam | Agreed to 640×480 MJPG at 30 frames/s; delivered 7.6 to 15 | backend log, same runs |
| Firewall rule after enabling the boot service | Present; service enabled (not yet checked across a reboot) | `lock_mavsdk_port.sh --status` |

Reading those numbers:

- **The detector takes almost all the CPU.** It runs as fast as it can, and on the Pi that
  leaves about 8% of the processor free. Nothing suffered in this run (the backend's loop
  lag stayed under 10 ms and the data stayed fresh), but there is no headroom for mapping
  or thermal search at the same time. `AEGIS_DETECTOR_MAX_FPS` now caps the detector.
- **Detection is half the speed of the benchmark** (335 ms vs 158 ms). The backend gives
  the detector half the cores, a default meant for a PC that also runs the simulator; the
  benchmark used all four. On the Pi `AEGIS_DETECTOR_THREADS=4` is worth measuring.
- **Two figures from that run are not usable.** The measurement script sampled once a
  second instead of ten times, because its ping to the PC waited a second for an answer
  that never came (Windows blocks ping by default). The "updates per second" it printed
  (1.05) was the sampling rate, not the data rate; the data age (57 ms) shows updates were
  arriving far more often. The script now pings on its own thread and says when its
  sampling is too slow to judge the rate. The network round trip was not measured.

### Not verified on the Pi

| Item | Why it matters |
| --- | --- |
| Where the CPU goes when the detector is capped (about half is unaccounted for) | Needs a run without the camera |
| ONNX Runtime spinning off, on the Pi | Measured only on the development machine (a third less detector CPU) |
| Depth mapping in flight (Demo 4) | Needs Gazebo on the PC streaming depth to the Pi |
| Temperature over a long run, in an enclosure, without a fan | The benchmark lasted about a minute |
| Demos 5 and 6, the TRUST scenarios, incident capture | Not run on the Pi |
| The systemd unit (`deploy/aegis-backend.service`) | Written, never installed |
| Any flight controller, any radio attached to the Pi | None owned or connected |
| Power from a flight battery / BEC | Bench supply only |

### Procedure to collect the missing measurements

**Awaiting physical verification: these commands have not been run on the Pi.** The
measurement script itself was run on the development machine (x86) against a live simulated
flight and produced a complete report there.

On the PC (WSL), start the simulator with the companion link:

```bash
cd ~/AEGIS-ONE && git pull
AEGIS_COMPANION_IP=192.168.1.57 bash scripts/run_px4.sh sih
```

On the Pi, terminal 1:

```bash
cd ~/AEGIS-ONE && git pull
bash scripts/run_backend.sh
```

On the Pi, terminal 2 (start a flight, then measure during it):

```bash
cd ~/AEGIS-ONE
.venv/bin/python scripts/demo1_survey.py --token "$(grep ^AEGIS_OPERATOR_TOKEN .env | cut -d= -f2)" &
sleep 25
.venv/bin/python scripts/edge_measure.py --seconds 90 --peer 192.168.1.7 --label "Pi 5, Demo 1, wired LAN"
```

It reports CPU, memory, temperature, throttling flags, backend loop lag, API round trip,
telemetry age and arrival rate, and network round trip to the PC. Camera frame rate and
detection time are reported when perception is on; to collect them, run Gazebo with the
camera on the PC, set `AEGIS_PERCEPTION=true` and the camera address in the Pi's `.env`,
restart the backend and repeat during Demo 2.

For temperature over time, run it for 20 minutes during repeated flights
(`--seconds 1200`). The result file is `data/generated/edge_measurements/edge_*.json`; send
it back and it will be added to this table as hardware-tested evidence.

For the webcam, stop the backend and run `.venv/bin/python scripts/camera_check.py
--seconds 30` on the Pi (details in `docs/raspberry-pi/README.md`, Part 4). It reports the
negotiated format, the delivered frame rate, detection time on live frames, frames the
detector had no time for, and the temperature. Then set `AEGIS_CAMERA=usb` and repeat the
measurement above during a flight.

What the script cannot measure: one-way delay from the autopilot to the Pi (needs clocks
synchronised better than the delay), and power draw (needs a meter).

## 4. Flight-controller readiness

**Not ready, and not claimed.** Simulation tests passing says nothing about a physical
integration. Before a physical flight controller is even connected for monitoring:

| Requirement | State |
| --- | --- |
| Commands impossible against a non-simulator | Implemented, unit-tested. Never exercised against real hardware |
| Read-only monitoring mode | Implemented. Never exercised against real hardware |
| Data labelled `physical` and visibly different in the UI | Implemented, unit-tested |
| MAVSDK control port closed to other computers | **Done on the owner's Pi, checked from a second computer.** The server cannot be bound to localhost; `scripts/lock_mavsdk_port.sh` adds a firewall rule. Lost on reboot unless `deploy/aegis-firewall.service` is installed |
| MAVLink 2 message signing | **Not possible with PX4 v1.17** (no signing in its MAVLink module) |
| Serial (UART) connection configuration and documentation | **Not done** (UDP only so far) |
| Bench procedure with propellers removed | **Not written** |
| Physical camera adapter | USB webcam adapter (bench only), run on the owner's Pi 5 with a real webcam |
| Recorded physical telemetry replayed through every monitor to check for false alarms | **No such recording exists** |
| Independent review of this gating | **None** |

Physical flight needs, in addition and outside this software: a separately validated
configuration, explicit operator authorisation, independent flight-controller safeguards
(RC override, geofence and failsafes configured in the autopilot itself), a test plan, a
supervised site, and compliance with the applicable aviation rules. None of that is in
scope here and no code path enables it.

## 5. Phase 8 completion gate

| Gate | Evidence | Met |
| --- | --- | --- |
| Hardware interfaces inventoried, each with its evidence | `/api/hardware`, section 2 | yes |
| Simulation, playback, physical monitoring, HIL and physical operation are separate and named | Section 1; `test_hardware.py` | yes |
| Physical control disabled by default and not enableable by configuration | `AEGIS_MODE=physical` refused; no hardware executor exists | yes |
| Provenance decided by evidence | `test_hardware.py`; defect 1 in `docs/ASSESSMENT.md` | yes |
| Pi integration reviewed, unverified items listed | Section 3 | yes |
| Reproducible Pi measurement procedure | `scripts/edge_measure.py`, run on x86 only | **procedure yes, Pi measurements awaiting the owner** |
| Flight-controller readiness documented without claiming readiness | Section 4 | yes |

Phase 8 is complete as a software and documentation phase. The Pi measurements are open and
are carried into Phase 14.
