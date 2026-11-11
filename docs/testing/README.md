# Testing

Results below were produced on 2026-11-10 on the bootstrap machine (Ubuntu 24.04, 2 vCPU, 8 GB,
no GPU) against PX4 v1.17.0 SITL with the SIH simulator. Re-run them on your own machine; they
are a record of what was observed, not a guarantee.

## Commands and results

| Suite | Command | Result |
| --- | --- | --- |
| Backend unit + API + perception + navigation + search + fleet + security | `pytest` | 203 passed, 1 skipped here (the SITL tests are collected separately) |
| Lint | `ruff check . && ruff format --check .` | clean |
| Types | `mypy` (strict) | no issues in 39 source files |
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

## Perception results (Phase 2, measured 2026-11-10)

Machine: the bootstrap VM, 2 logical CPUs, no GPU, ONNX Runtime CPU provider. Your Ryzen
desktop should be considerably faster; these are not targets.

**Detector** (`python scripts/eval_detector.py`, YOLO11n ONNX, confidence 0.35, IoU 0.5):

| Metric | Value |
| --- | --- |
| Dataset | COCO8: 8 images, 30 labelled objects. A smoke test, **not** a benchmark. |
| True / false positives / false negatives | 16 / 2 / 14 |
| Precision / recall | 0.889 / 0.533 |
| Inference latency, median / p95 | 87 ms / 104 ms (about 11.5 fps) |

The low recall is mostly small or crowded objects at this threshold; with 30 objects the
figure moves several points per object, so do not read much into it.

**Tracker on synthetic ground truth** (`backend/tests/test_tracker.py`): three objects, 300
frames, 5 random seeds, 20% missed detections and 3 px jitter: 0 identity switches, lost-track
rate under 2%. A 15-frame full occlusion keeps the same ID.

**End-to-end with the real model**: a 16 s clip built from real photos moving across a frame,
one passing behind a pillar, streamed through `scripts/stream_video.py` into the live backend
and dashboard. Pipeline ran at about 12 fps (60 of 240 frames dropped because inference is
slower than the 15 fps clip). One object kept a single ID for the whole clip. The object that
passed behind the pillar **did get a new ID**: it was hidden for roughly 3 s, longer than the
30-frame coast limit. There was also one duplicate track from a double detection. Short
occlusions hold; long ones need appearance-based re-identification, which is not implemented.

Stopping the video sender raised a `camera.no_frames` warning within a few seconds.

**Session replay**: a full run (urban scenario, built-in simulator, streamed test clip) was
recorded and replayed with `AEGIS_REPLAY_SESSION=latest`; flight, video, boxes and track IDs
played back together, labelled `replayed`. A unit test checks alignment directly: at a given
replay time the decoded video frame shows the object where that frame's recorded box says it is,
including after a jump and after the replay loops.

**Demo 2 rehearsal**: `demo2_detection.py --rehearsal` was run with the urban mission on the
built-in simulator and a streamed video file. It correctly refused to run without
`--rehearsal` (camera not `simulated`), and correctly failed criteria 2 and 3 (no vehicles in
the clip). That exercises the script's logic only; it is not a Demo 2 result.

**Demo 2 against Gazebo (run by the project owner, 2026-11-10)**: the urban world loaded, the
Fuel models downloaded, and criteria 2 to 4 passed: simulated camera continuous, `person`
(best confidence 0.90), `car` (0.91) and `truck` (0.81) tracked on Gazebo-rendered imagery,
detections stored. Criteria 1 and 5 failed: the flight was ended by an operator Land command
during the return leg, and a critical alert was raised (cause not yet identified). The detector
also produced 64 spurious `airplane` tracks at up to 0.90 confidence plus a few other false
classes; the urban scenario now restricts the detector to people and road vehicles. Demo 2 has
therefore **not passed yet**.

The critical alert was `link.lost`, and it was a false alarm caused by my code. With the camera
rendering, Gazebo ran at roughly 0.3x real time (the same 22 m leg took 38 s against 12 s on the
built-in simulator). PX4's 1 Hz heartbeat is in simulation time, so it arrived about every 3 s of
wall time and tripped a 3 s wall-clock link timeout. Reproduced here by running the built-in
simulator with `PX4_SIM_SPEED_FACTOR=0.3`: the link flapped every 3 s, and a mission submitted
in that state crashed the MAVSDK server process. Fixes, all verified at 0.3x:

- Link health is judged from telemetry freshness in simulation time; the measured real-time
  factor is shown in the dashboard and a `sim.slow` warning is raised below 0.5x.
- Waypoint leg timeouts are counted on the vehicle clock.
- Commands wait for a current MAVSDK heartbeat, and the MAVSDK server is restarted
  automatically if it exits (killed on purpose here: back and re-attested within 25 s).
- Demo 1 at 0.3x: 5 of 5 criteria, 447 s, no critical alerts.

**Urban scenario**: the mission (three waypoints at 4 m, then return) was flown on the built-in
simulator, which has no buildings to hit. Route clearance from every obstacle is asserted by
`test_scenarios.py`.

## Navigation and mapping results (Phase 3, measured 2026-11-11)

All with PX4 v1.17.0 SITL on the built-in simulator and the software depth sensor (96 x 72 px,
73 deg, 19 m range, 1% range noise, 5 Hz). Ground truth is the scenario's own geometry.

**Demo 4, mapping (urban street, one pass at 6 m):** 5 of 5 criteria.

| Metric | Value |
| --- | --- |
| Depth frames fused | 552 |
| Occupied voxels (0.5 m) | 3,365 |
| Precision (within one voxel of real geometry) | 0.992 |
| Recall of the obstacle surface that was observed | 0.932 |
| Surface coverage | 0.120: one pass down a street only sees the faces that front it |

**What that result does not show.** The software depth sensor renders from the same estimated
pose the mapper then uses, so pose-estimate error cancels out of it. Gazebo renders from where
the vehicle really is.

**Demo 4 with the Gazebo depth camera (reported by the owner, WSL2, reduced cameras):** 3 of 5
criteria; the mission, depth fusion and map save passed, accuracy failed.

| Metric | Value |
| --- | --- |
| Depth frames fused | 371 at scoring (518 in the saved map) |
| Precision | 0.630 (pass mark 0.90) |
| Recall of the observed surface | 0.590 (pass mark 0.80) |
| Same map moved 0.5 m west (`scripts/analyze_map.py`) | precision 0.904, recall 0.814 |

Analysis of the saved map and flight log (done here): both street walls are mapped about
0.5-0.75 m east of where they are; the recorded track stays within 0.1 m of the centreline. On
the pad Gazebo reports the model at yaw 0 (facing east, heading 90 deg) while the autopilot
reports heading 95.3 deg. Integrating software-rendered depth with a deliberately biased heading
reproduces the failure (precision / recall: 0.99 / 0.99 at 0 deg, 0.86 / 0.95 at 2 deg,
0.58 / 0.78 at 3.5 deg, 0.51 / 0.19 at 5.26 deg). The in-flight heading error has not been
measured yet, and why PX4's heading estimate is off in this world is not known.

To measure it, the Gazebo depth bridge now attaches the simulator's ground-truth pose to each
frame. AEGIS integrates a second, scoring-only map with it and reports both scores plus the
autopilot's pose error (`/api/map/accuracy`). Route checks and holds never use the true pose.
This path has unit tests for the frame conversion and parsing but has NOT been run on Gazebo.

**Obstacle detection and replanning (open field, tower unknown to the route checker):** 6 of 6
criteria. The mission was accepted, the tower was mapped in flight, Hold was commanded with the
conflict 14 m ahead, and the vehicle stopped 11.7 m from the tower. A two-waypoint detour was
planned, validated, flown, and the vehicle returned and landed. Clearance (3 m) was kept
throughout.

Two things these runs turned up and fixed:

- A detour planned exactly along the clearance boundary was rejected seconds later, because the
  hovering vehicle had mapped a little more of the tower. Detours are now planned with 1.5 m of
  extra room.
- The urban route at 4 m was stopped by the route monitor above the parked cars: with about
  1 m of altitude-estimate noise the vehicle came within the clearance rules of a 1.9 m roof.
  The pre-flight check had passed only by a rounding margin. The urban route now flies at 6 m.
  **Demo 2 has not been re-run at 6 m.**

**Unit tests** cover ray casting against boxes (including yawed) and cylinders, planar-depth
rendering, body-to-ENU rotation, log-odds fusion and clearing of stale obstacles, the ground
filter, save / load, A* (no corner cutting, no-route case), detour planning, return-leg
checking, the automatic Hold (once per mission, off when disabled), proximity alerts, lost depth
sensing, and the API paths (rejection before upload, planning, map endpoints, map reload).

## Search and rescue results (Phase 4, measured 2026-11-11)

PX4 v1.17.0 SITL on the built-in simulator, `sar_field` scenario, software thermal camera
(160 x 120 px, 57 deg, 4 Hz, 0.08 C noise, ideal stabilised mount), search at 25 m.

**Demo 3:** 8 of 8 criteria, 230 s from upload to disarm.

| Item | Result |
| --- | --- |
| Planned pattern | 4 passes, 27.1 m swath, 20 m spacing, 576 m |
| Search area seen by the camera | 100% |
| Thermal frames processed | 969 |
| Heat sources visible from above | 6 of 6 listed as candidates |
| Person under the shelter roof | Not found (expected: not visible from above) |
| Candidates with no source behind them | 0 |
| "Person-sized" candidates | 3: two people and the deer |
| Candidates confirmed before the operator's review | 0 |
| Review without the operator token | Refused (401) |

Read these with care:

- Reported position errors were 0.00-0.01 m. That is not a real-world figure: the model
  renders from the same pose estimate that geolocation then uses, so pose error cancels, as it
  did for the software depth sensor. Under Gazebo the heading error alone moved a depth map by
  about half a metre.
- The first run found only 2 of 6. With the camera fixed to the body, the vehicle's 26 deg
  lean at cruise in this simulator exceeded the detector's 25 deg tilt limit for almost the
  whole flight. The camera model now sits on an ideal stabilised mount (a real one has lag and
  travel limits).
- The script, not a person, submitted the mission and entered the review decisions, using the
  scenario's answer key. It exercises the workflow; it says nothing about how well a person
  would judge the candidates.

Unit tests cover the camera model's geometry (position, heading, occlusion under a roof),
geolocation from an offset and tilted view, candidate merging and review rules, pattern
planning for wide, tall and triangular areas, coverage accounting, the API's token check, and
that neither a detection nor a confirmation issues a vehicle command.

## Fleet results (Phase 5, measured 2026-11-11)

Two PX4 v1.17.0 instances on the built-in simulator (`scripts/run_fleet.sh 2 sar_field`), homes
12.6 m apart, one backend with `AEGIS_FLEET_SIZE=2`, software thermal camera on each vehicle.

**Demo 5:** 8 of 8 criteria.

| Item | Result |
| --- | --- |
| Plans | sim-1: 3 passes at 25 m, 330 m. sim-2: 3 passes at 31 m, 374 m. No conflicts. |
| Deliberate conflict (sim-1's route given to sim-2) | Refused before upload |
| Missions | Both completed; each vehicle landed at its own home |
| Search area seen | 100% (2,052 thermal frames from two cameras) |
| Heat sources visible from above | 6 of 6, candidates first seen by both cameras |
| Closest approach in the air | 7.8 m horizontally with 5.7 m of height: inside the warning distance, outside the Hold distance |
| Automatic separation Holds | 0 |
| First upload to last landing | 207 s (one vehicle alone: about 228 s) |

Read these with care:

- Two vehicles were barely faster than one here. The area is small, so transit, climb, return
  and landing dominate; the search legs themselves took roughly half as long.
- The automatic separation Hold never fired in a live flight. Its logic is covered by unit
  tests against stand-in vehicles (hold both once, re-arm after separating, alerts, off switch).
- The built-in simulator has no collisions and the vehicles do not sense each other. This tests
  AEGIS's bookkeeping, not physical separation.
- The dashboard was also driven here through the Fleet panel (plan, confirm, launch two
  vehicles) and screenshotted mid-flight; that run was not followed to landing.

## Anomaly and security results (Phase 6, measured 2026-11-11)

PX4 v1.17.0 SITL on the built-in simulator, open-field mission, backend started with
`AEGIS_FAULT_INJECTION=true`.

**Demo 6:** 8 of 8 criteria.

| Injected | Expected alert | Raised after |
| --- | --- | --- |
| Position drifting at 3 m/s | `nav.position_drift` | 3.8 s |
| Frozen telemetry | `telemetry.frozen` | 2.2 s |
| Battery reported at 20% | `battery.implausible` | 0.2 s |
| Autopilot clock set back 30 s | `telemetry.clock_reversed` | 0.2 s |
| Position jump of 25 m | `nav.position_jump` | 0.2 s |
| Hold from outside the operator's command path | `control.unexpected_mode` | 1.2 s |
| Five requests with a wrong token (all refused) | `security.auth_failures` | 0.2 s |

Also: no anomaly or security alert in 12 s of normal flight; every altered snapshot carried
its fault label; the vehicle kept flying its mission through the five telemetry faults; the
audit log (21 records) verified and an edited copy was detected; the vehicle landed at home
after the operator's Return. Demo 1 was re-run afterwards with the monitors on: 5 of 5, no
critical alerts. The two live simulation tests pass on the new localhost-only listener.

False alarms: the detectors were replayed over 21 recorded flights (33,465 states, including
the owner's Gazebo flight); none raised a navigation, telemetry, battery, route or mode alert.
The largest position-against-velocity disagreement in those clean flights was 2.9 m over 1 s
and 5.8 m over 10 s; the alert thresholds are 5 m and 10 m. That margin is thin, and the
flights are all gentle missions on two simulators.

Found while building this, and fixed:

- **Missions could resume part-way.** PX4 remembers its progress through a mission. Submitting
  the same mission again after an interrupted flight started at a later waypoint instead of
  the first. The off-route check caught it in the demo's own baseline. Upload now clears the
  old mission and resets the index; verified on a PX4 instance holding an interrupted mission.
- **A first version of the drift check never ran.** PX4 corrects the home altitude every couple
  of seconds in flight, and the monitor treated each correction as a change of frame and
  discarded its history, so the 10 s check was never evaluated. My first false-alarm run
  looked clean for that reason. Fixed, and the replay above is from after the fix.

Limits: the checks are fixed rules with hand-set thresholds, on the first vehicle only. A
careful attacker who alters position and velocity consistently would pass the navigation
checks. The audit chain cannot show a log truncated at the end or replaced wholesale. The
"outside Hold" is produced inside AEGIS; no second ground station was connected.

## Not tested yet

- Anomaly detection during Gazebo flights on the owner's machine (only replayed from one
  recorded Gazebo flight), and with a fleet.

- Fleets of three or four vehicles, fleets under Gazebo, a fleet with the camera or depth
  pipelines on, and anything about a fleet on the owner's machine.

- Demo 3 under Gazebo, and the Search panel driven by a person through a whole flight (it was
  loaded and screenshotted after the flight here).
- The Gazebo ground-truth pose path (`gz_camera_bridge.py --depth` pose subscription, scoring
  map, pose-error report). The owner reports the run worked; the numbers were not captured.

- Anything under Gazebo by me (not installable on the bootstrap machine). The project owner
  reports the Gazebo launch works on WSL2.
- A complete Demo 2 run at 6 m. The owner's run at 6 m tracked `person`, `car` and `truck`
  through the return leg; the final verdict was not captured.
- Mapping while the simulation runs well below real time.
- Detection on Gazebo-rendered imagery (rendered people and vehicles may detect poorly).
- ROS 2, and every Phase 5+ capability.
- PX4 geofence breach behaviour, wind, in-simulator sensor faults.
- Browser end-to-end tests (Playwright) and React component tests. The dashboard was driven
  by a script in headless Chrome once; that script is not in the repository yet.
- `scripts/setup_wsl.sh` end to end. PX4 was built here with the same tag and `make` target,
  but dependencies were installed by hand rather than through the script.
- Simulation tests in CI. The CI workflow (lint, types, backend tests, frontend tests and build) passed on its first run on GitHub; it does not start PX4.

## Performance

Nothing has been measured formally. The targets below are **engineering hypotheses**.

| Metric | Hypothesis | Measured |
| --- | --- | --- |
| Telemetry broadcast rate | 10 Hz | not measured (configured rate) |
| Telemetry end-to-end latency (PX4 to browser) | under 150 ms on localhost | not measured |
| Dashboard frame rate | 60 fps on the Ryzen desktop with a GPU | not measured (headless software rendering only) |
| Simulation real-time factor (SIH) | 1.0 | 1.0 reported by PX4 at startup; not measured under load |
| Simulation real-time factor (Gazebo, x500) | at least 0.9 | not measured |
