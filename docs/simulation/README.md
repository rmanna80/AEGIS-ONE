# Simulation

## Pinned versions

| Component | Pin | Notes |
| --- | --- | --- |
| Ubuntu | 24.04 LTS (WSL2) | |
| PX4 Autopilot | `v1.17.0` | Latest stable tag when the project started (Nov 2026). Built and flown. |
| Gazebo | Harmonic (LTS) | The release PX4 supports since v1.16. Installed by PX4's `Tools/setup/ubuntu.sh`. Not yet run by this project. |
| ROS 2 | Jazzy | PX4's recommended pairing for Ubuntu 24.04. Not used until Phase 2. |
| px4_msgs | `release/1.17` | Must match the firmware line. Not used until Phase 2. |
| MAVSDK | `mavsdk-grpc` 4.0.x (MAVSDK v4.0.5 server) | See docs/architecture for why not `mavsdk`. |
| Python | 3.12 | |
| Node.js | 22 | |

Pins live in `scripts/versions.env`, `pyproject.toml` and `frontend/package-lock.json`.
Do not move a pin without re-running the simulation tests.

## Two physics back-ends, one flight stack

Both options run the real PX4 flight stack (EKF2 estimator, navigator, commander, failsafes) as
a Linux process and expose MAVLink on UDP 14540. AEGIS cannot tell them apart except by the
airframe id it reads for attestation.

| | SIH (`run_px4.sh sih`) | Gazebo Harmonic (`run_px4.sh gazebo`) |
| --- | --- | --- |
| Dynamics | Rigid-body quadcopter model inside PX4 (250 Hz, lockstep) | Gazebo ODE physics, x500 model |
| Sensors | Simulated IMU, GNSS, barometer, magnetometer | Same, plus cameras, depth, LiDAR (to be configured) |
| World geometry and collisions | None (flat ground only) | Yes: `aegis_open_field.sdf` |
| Wind | Not available in PX4 v1.17 SIH | Gazebo wind plugin (Phase 3) |
| Needs a GPU / GUI | No | Recommended |
| Airframe id (`SYS_AUTOSTART`) | 10040 | 4001 |
| Status in this project | **Built and flown** | Launch confirmed by the project owner on WSL2 |

### Why SIH first

Gazebo's package repository was unreachable from the machine that bootstrapped this repository,
so Gazebo could not be installed there. SIH let Phase 1 be verified against the real PX4 stack
anyway. On your WSL2 machine the Gazebo path should work, but treat its first launch as a test:
if `bash scripts/run_px4.sh gazebo` fails, the log line to look for is
`Starting gazebo with world: .../aegis_open_field.sdf`.

What that first Gazebo run has to confirm:

1. The generated world loads (it copies the physics, scene and spherical-coordinate blocks of
   PX4's stock `default.sdf`).
2. The x500 spawns on the landing pad at the world origin.
3. AEGIS attests airframe 4001 and Demo 1 passes unchanged.
4. Obstacles are where the dashboard draws them (both come from the same scenario file).

## Environments

| Environment | State |
| --- | --- |
| A: Open test field | Scenario + generated world. Used by Demo 1. |
| B: Urban, C: Forest, D: Indoor lab, E: Multi-drone arena | Not started (Phases 2 to 5) |

Add an environment by writing `simulation/scenarios/<name>.json` and running
`python scripts/generate_gazebo_world.py simulation/scenarios/<name>.json`.

## Known simulation limitations

- **Battery**: PX4's SITL battery model drains to 50% and then holds
  (`SIM_BAT_MIN_PCT`). The dashboard labels it "simulated model". Low-battery behaviour is
  therefore only covered by injected-fault unit tests so far.
- **No collisions under SIH**: the dashboard's obstacles are drawn but not simulated.
- **Simulator controls** (pause, reset, fault injection from the UI) are not implemented. Reset
  is "restart `run_px4.sh`".
- **Sim time** shown in the dashboard is the autopilot clock. SIH ran at 1.0x real time here;
  real-time factor has not been measured under load.
- **Rotor animation** in the 3D view reflects the armed flag only, not rotor speed.

## Ports

| Port | Purpose |
| --- | --- |
| UDP 14540 | PX4 offboard MAVLink, used by AEGIS (one client at a time) |
| UDP 14550 | PX4 GCS MAVLink, free for QGroundControl |
| TCP 50051 | MAVSDK gRPC server started by the backend (localhost) |
| TCP 8000 | AEGIS API + dashboard |
| TCP 5173 | Vite dev server (optional) |

WSL2 forwards localhost ports to Windows, so open the dashboard in a Windows browser at
`http://127.0.0.1:8000`. QGroundControl on Windows may need WSL2 mirrored networking to see
UDP 14550; that has not been tested by this project.
