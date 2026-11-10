# AEGIS ONE

Simulation-first autonomous drone research platform: PX4 software-in-the-loop flight, a typed
telemetry/mission backend, and a 3D mission-control dashboard. Intended for authorized,
non-weapon robotics research, environmental inspection and search and rescue, always with a
human operator in control.

**Status: Phase 0 and Phase 1 complete.** A simulated PX4 quadcopter flies a geofenced waypoint
mission commanded from the dashboard, with live telemetry, alerts, flight recording and replay.
Perception, mapping, thermal, multi-drone, anomaly detection and security monitoring are later
phases and are **not** implemented yet (see [docs/ROADMAP.md](docs/ROADMAP.md)).

No drone hardware is needed. No physical-flight code path exists in this release.

![Dashboard during a live PX4 SITL mission](docs/images/dashboard_live_sitl.png)

## What works today

| Capability | State |
| --- | --- |
| PX4 v1.17.0 SITL flight stack with PX4's built-in physics (SIH quadcopter) | Verified |
| Telemetry adapter (MAVSDK), typed contracts with provenance, frames and units | Verified |
| Waypoint missions: validate, upload, arm, fly, return, land | Verified against PX4 SITL |
| Geofence: checked before upload, uploaded to PX4, monitored during flight | Verified (pre-flight + monitor); PX4 breach action not yet exercised |
| Operator intervention: hold / return / land, token-gated | Verified against PX4 SITL |
| Unreachable-waypoint detection (leg timeout, then hold) | Verified |
| Advisory alerts: link loss, stale data, battery, GNSS, estimator, geofence | Unit-tested with injected faults |
| Flight recording (JSONL) and replay, re-labelled as `replayed` | Verified |
| 3D dashboard: drone pose, trajectory, route, geofence, obstacles, 5 camera modes, charts | Verified in a browser |
| Gazebo Harmonic world for Environment A (generated from the scenario file) | **Written, not yet run** |
| ROS 2 Jazzy bridge, cameras, LiDAR, thermal | Not started (Phase 2+) |

"Verified" means it was executed and observed; the commands and results are in
[docs/testing/README.md](docs/testing/README.md).

### About the simulator

The project brief names Gazebo as the authoritative physics simulation. The machine this
repository was first built on could not install Gazebo, so Phase 1 was verified with **SIH**,
the rigid-body quadcopter simulator that ships inside PX4. It runs the same PX4 flight stack,
estimator, mission logic and MAVLink interface; it has no cameras, LiDAR or collision geometry.
AEGIS talks to PX4 only over MAVLink, so switching to Gazebo changes one launch command and no
application code. Obstacles shown in the dashboard are physical only under Gazebo.
Details: [docs/simulation/README.md](docs/simulation/README.md).

## Quick start (Windows + WSL2, Ubuntu 24.04)

```bash
# 1. One-time setup inside WSL2 (installs PX4 v1.17.0 + Gazebo Harmonic, Python and Node deps)
git clone https://github.com/rmanna80/AEGIS-ONE.git && cd AEGIS-ONE
bash scripts/setup_wsl.sh              # or: bash scripts/setup_wsl.sh --no-gazebo

# 2. Terminal A: the simulated drone
bash scripts/run_px4.sh sih            # or: bash scripts/run_px4.sh gazebo

# 3. Terminal B: backend + dashboard
#    (edit .env first and set AEGIS_OPERATOR_TOKEN to a value of your choice)
bash scripts/run_backend.sh
```

`setup_wsl.sh` has not been run end to end yet (PX4 was built with the same tag and target, but
by hand), so expect to adjust it on first use.

Open <http://127.0.0.1:8000>. Paste your operator token, press **Upload & start mission**, then
**Confirm**. The drone takes off, flies five waypoints, returns and lands (about 2.5 minutes).

No PX4 yet? Watch a recorded flight instead:

```bash
AEGIS_MODE=replay bash scripts/run_backend.sh
```

Frontend development with hot reload: `cd frontend && npm run dev` (http://localhost:5173,
proxies to the backend).

## Demo 1: Autonomous survey

```bash
python scripts/demo1_survey.py --token "$AEGIS_OPERATOR_TOKEN"
```

Submits the scenario mission through the API, follows it, and checks five acceptance criteria
(accepted, all waypoints reached, stayed inside the geofence, landed within 3 m of home and
disarmed, no critical alerts). A JSON report is written to `data/generated/demo_results/`.
Reset: the vehicle ends landed at home, so just run it again. Demos 2 to 5 arrive with their
phases.

## Tests

```bash
pytest                                   # backend unit + API tests (no PX4 needed)
ruff check . && ruff format --check . && mypy
cd frontend && npm test && npm run build
# Live simulation tests: start PX4, stop the backend (it owns UDP 14540), then:
AEGIS_SITL_TESTS=1 pytest tests/simulation -v
```

## Operating modes

| `AEGIS_MODE` | Data source | Commands |
| --- | --- | --- |
| `simulation` (default) | PX4 SITL | Allowed with the operator token, and only after the autopilot is identified as a simulation airframe |
| `monitor` | PX4 SITL | None (read-only) |
| `replay` | Recorded flight | None (read-only) |
| `physical` | n/a | Rejected at startup. Physical flight is a separately gated future milestone. |

Every state, alert and recording carries its provenance (`simulated`, `replayed`, `physical`),
and the dashboard shows it in the header at all times.

## Repository layout

```text
backend/aegis/          Python package
  contracts.py          typed data contracts (provenance, frames, units)
  frames.py             WGS84 / ECEF / ENU / NED / body conversions
  missions.py           mission model, geofence geometry, pre-flight validation
  safety.py             advisory monitors and alert tracking
  interfaces.py         hardware-abstraction protocols
  adapters/px4_sitl.py  PX4 SITL adapter (telemetry + mission executor)
  recording.py          flight recorder and replay adapter
  hub.py                sample -> monitor -> record -> broadcast loop
  api.py                FastAPI REST + WebSocket
backend/tests/          unit and API tests
tests/simulation/       tests that need a live PX4 SITL
frontend/               React + TypeScript + React Three Fiber dashboard
simulation/scenarios/   environment + demo mission (single source of truth)
simulation/gazebo/      generated Gazebo world
scripts/                setup, launch, demo and generator scripts
data/sample/            a real recorded SITL flight for replay mode
docs/                   architecture, simulation, safety, testing, roadmap
```

This is flatter than the structure suggested in the brief: directories are added when their
first real code lands, rather than created empty. The reasoning is in
[docs/architecture/README.md](docs/architecture/README.md).

## Safety boundaries

- AEGIS never commands actuators. It uses PX4's own mission, hold, return and land modes, so the
  flight controller's failsafes always remain in charge.
- AEGIS monitors are advisory. They raise alerts; they do not override the autopilot.
- Commands are refused unless the autopilot reports a known simulation airframe.
- Passing simulation tests is not evidence of flight readiness.

More in [docs/safety/README.md](docs/safety/README.md).
