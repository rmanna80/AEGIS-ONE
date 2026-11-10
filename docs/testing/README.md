# Testing

Results below were produced on 2026-11-10 on the bootstrap machine (Ubuntu 24.04, 2 vCPU, 8 GB,
no GPU) against PX4 v1.17.0 SITL with the SIH simulator. Re-run them on your own machine; they
are a record of what was observed, not a guarantee.

## Commands and results

| Suite | Command | Result |
| --- | --- | --- |
| Backend unit + API | `pytest` | 61 passed, 2 skipped (the SITL tests) in 0.6 s |
| Lint | `ruff check . && ruff format --check .` | clean |
| Types | `mypy` (strict) | no issues in 12 source files |
| Frontend unit | `cd frontend && npm test` | 9 passed |
| Frontend build | `cd frontend && npm run build` | type-checks and builds |
| Live simulation | `AEGIS_SITL_TESTS=1 pytest tests/simulation -v` | 2 passed in 145 s |
| Demo 1 | `python scripts/demo1_survey.py --token ...` | 5 of 5 criteria passed, 136 s |

The live simulation tests bind UDP 14540 themselves, so stop the backend before running them.

### What the live simulation tests assert

`tests/simulation/test_sitl_waypoint_mission.py`, against a running PX4:

1. Telemetry is labelled `simulated`, the simulator is attested, the vehicle is parked at home
   and the autopilot clock is advancing.
2. The demo mission is accepted; the vehicle takes off, passes within 5 m of every waypoint,
   never leaves the geofence, exceeds 10 m altitude, returns, lands within 3 m of home and
   disarms; final status is `completed` with 5 of 5 waypoints.

These cover simulation tests 1 (take-off and landing) and 2 (waypoint mission) from the brief.

### Checked by hand against the live simulator

| Check | Observed |
| --- | --- |
| Mission with a waypoint 300 m east of a +/-80 m fence | `rejected`, "Waypoint 2 is outside the geofence." Nothing sent to PX4. |
| Mission submit without a token | HTTP 401 |
| Hold during the mission | PX4 mode HOLD, status `cancelled`, 0.8 m drift over 5 s |
| Return after hold | PX4 mode RETURN_TO_LAUNCH, landed and disarmed 0.7 m from home |
| Mission started from the dashboard in Chrome | Flew to completion; pose, track, charts and progress updated live |
| Replay mode | Header shows "REPLAYED (of simulated flight)"; commands return HTTP 403 |

These should become automated tests (see ROADMAP).

### What the unit tests cover

- **Frames**: ECEF reference points, geodetic/ECEF and ENU/geodetic round trips, axis
  directions, quaternion to Euler, body-to-NED rotation, gimbal lock, heading wrap.
- **Missions**: point-in-polygon including edges and concave fences, legs that cut a concave
  corner, altitude limits, leg length, start and home outside the fence, schema rejection.
- **Safety**: link, staleness, battery, GNSS, estimator and geofence monitors, masking of secondary alerts
  on link loss, alert raise / escalate / clear transitions, provenance on alerts.
- **Recording**: write/read round trip, malformed files, replay relabelling, replayed mission
  status, replay being read-only, validity of the bundled sample.
- **API**: health, system info, WebSocket snapshots, scenario, token enforcement on all four
  command routes, monitor mode, invalid missions, hub-level fault injection, refusal of
  physical mode.
- **Frontend**: scene axis mapping and the attitude-quaternion change of basis.

## Not tested yet

- Anything under Gazebo (not installed on the bootstrap machine).
- ROS 2, cameras, and every Phase 2+ capability.
- PX4 geofence breach behaviour, wind, in-simulator sensor faults.
- Browser end-to-end tests (Playwright) and React component tests. The dashboard was driven
  by a script in headless Chrome once; that script is not in the repository yet.
- `scripts/setup_wsl.sh` end to end. PX4 was built here with the same tag and `make` target,
  but dependencies were installed by hand rather than through the script.
- CI has been written (`.github/workflows/ci.yml`) but has not run yet.

## Performance

Nothing has been measured formally. The targets below are **engineering hypotheses**.

| Metric | Hypothesis | Measured |
| --- | --- | --- |
| Telemetry broadcast rate | 10 Hz | not measured (configured rate) |
| Telemetry end-to-end latency (PX4 to browser) | under 150 ms on localhost | not measured |
| Dashboard frame rate | 60 fps on the Ryzen desktop with a GPU | not measured (headless software rendering only) |
| Simulation real-time factor (SIH) | 1.0 | 1.0 reported by PX4 at startup; not measured under load |
| Simulation real-time factor (Gazebo, x500) | at least 0.9 | not measured |
