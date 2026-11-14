# Roadmap, limitations and technical debt

## Phase status

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and setup | Done |
| 1 | Working 3D simulation, telemetry, waypoint mission, dashboard | Done (SIH verified; Gazebo launch confirmed by the owner) |
| 2 | Perception: simulated RGB camera, detection, tracking, recording | Done. Demo 2 reported passing on Gazebo by the owner (at 4 m; route since raised to 6 m). |
| 3 | Navigation and mapping: depth, occupancy, planning, wind | Built and verified with the software depth sensor. Gazebo depth passes Demo 4 on the owner's machine after a compass calibration (precision 0.97, recall 0.81). Wind and SLAM not built. |
| 4 | Thermal and search and rescue | Built and verified live on PX4 SITL with a software thermal model (Demo 3: 8 of 8). No Gazebo-rendered thermal imagery; no thermal replay. |
| 5 | Multi-drone simulation | Built and verified live with two PX4 instances on the built-in simulator (Demo 5: 8 of 8). The automatic separation Hold is unit-tested only. No Gazebo fleet, no fleet replay; camera, mapping and the mission planner serve the first vehicle only. |
| 6 | Cybersecurity and anomaly detection | Built and verified live on PX4 SITL (Demo 6: 8 of 8). Rule-based checks on the first vehicle only; no learned model; MAVLink is unsigned and the MAVSDK control port is open on all interfaces. |
| 7 | Raspberry Pi integration | Built: setup script, companion MAVLink link, device health monitor, benchmark, systemd unit, guide. Done for the core path: installed, benchmarked and flown on the owner's Pi 5 (Demo 1, 5 of 5, simulator on the PC). Other demos and sensors on the Pi not yet run. |
| 8 | Physical hardware readiness (documentation and gating only) | Not started |

## Next milestone: Phase 8

1. Gazebo heading: resolved by calibration on the owner's machine (Demo 4 now 5 of 5). Left
   over: 2.3 deg of heading error on the pad, a 5.7 deg transient in flight, and a recall that
   passes by under 0.01. Repeat Demo 4 a few times to see the spread.
2. Find the process behind the slow memory decline (about 75 MB per 10 s) in Gazebo depth runs,
   and the fast one with stock 1080p cameras.
3. Remaining Phase 3 items: wind, a depth-image view, pose estimation from sensors, map
   uncertainty display, Environment D (indoor, GNSS-denied).
4. Remaining Phase 4 items: thermal imagery rendered by Gazebo, pose error injected into the
   thermal model, non-flat terrain, thermal replay, a Gazebo run of Demo 3.
5. Remaining Phase 5 items: a live test that provokes the separation Hold, timing-aware route
   deconfliction (today any two legs that pass close are refused, whenever they are flown),
   return legs included in the route check, other vehicles' planned routes on the 3D view,
   a vehicle selector for the mission planner, fleets under Gazebo, fleet replay.
6. Remaining Phase 6 items: anomaly checks for every vehicle of a fleet and under Gazebo,
   detection of a second ground station from MAVLink traffic itself, MAVLink 2 signing, a
   MAVSDK server bound to localhost, an anchor for the audit log's final hash, a learned
   model only if the rule-based checks prove insufficient.
7. Remaining Phase 7 items: Demos 3 and 6 and object detection on the Pi during a flight,
   temperature over a long run, the systemd unit, Gazebo with the Pi in the loop.
8. Phase 8: hardware readiness (documentation and gating only).

## Known defects and limitations

- Tracker has no appearance model: an occlusion longer than the coast limit produces a new ID,
  and double detections can create a duplicate track.
- Coast limit is counted in processed frames, so its duration depends on inference speed.
- Detection model licence is AGPL-3.0 (Ultralytics). Fine for private research; check it before
  distributing a product. The backend interface accepts any compatible ONNX model.
- No user-defined object classes yet (needs a fine-tuned model).
- Camera frames claiming `physical` provenance are refused until Phase 7.
- Gazebo with the camera ran at about 0.3x real time on the owner's WSL2 machine; the cause
  (software rendering, camera resolution or CPU contention) has not been established.
- MAVSDK v4.0.5's server crashed when commanded while it considered the system timed out.
  Worked around (commands wait for a heartbeat; the server is restarted), not fixed upstream.
- Replay has no pause or seek; speed and session are fixed at backend start.
- Route checking treats unobserved space as free and needs the perception extra installed;
  without it missions are checked against the geofence only.
- The occupancy map uses PX4's pose estimate as given. There is no SLAM and no map-uncertainty
  display; altitude noise of about 1 m smears thin horizontal surfaces.
- The `gazebo-depth` vehicle carries both a 1920x1080 RGB camera and the depth camera, so both
  bridges can run together, but rendering both will slow Gazebo further.
- Maps are tied to a scenario and resolution; loading one made for another is refused.
- Under RTL the vehicle climbs to PX4's return altitude (30 m) before flying home.
- Session recordings are large: about 8 MB per minute of video plus about 4 MB per minute of
  detections. Nothing is rotated or deleted automatically.
- The dashboard draws staged people and vehicles as proxy shapes, not the Gazebo models.
- Simulator start / pause / reset and fault injection are not available from the dashboard.
- Camera modes for thermal and depth do not exist yet (no such sensors until Phases 3 to 4).
- Dashboard layout has a 1100 px minimum width; it is not designed for phones.
- One vehicle only. `vehicle_id` is carried everywhere, but the hub is single-vehicle.
- The mission editor edits waypoints only; the geofence and constraints come from the scenario.
- Charts reset on page reload (history is kept in the browser, not fetched from the recorder).

## Technical debt

- `frontend/src/types.ts` is hand-mirrored from the Pydantic models; generate it from OpenAPI.
- The hand-run live checks in docs/testing should be automated; add Playwright end-to-end tests.
- `PX4SITLAdapter.stop()` calls a private MAVSDK method to end the server process.
- Simulation tests are not in CI (needs a PX4 SITL build or container image in the pipeline).
- Recordings are uncompressed JSONL at 10 Hz (about 0.8 MB per minute); fine for now, will
  need rotation and an index when detections and video references are added.
- JS bundle is about 1.6 MB (three.js + Recharts); split when it matters.
- No structured (JSON) logging yet; logs are plain text with provenance in alert lines.
- Operator token is a single shared secret; replace with users, roles and an audit log in
  Phase 6.
- Bundled favicon and UI use system fonts only; no design tokens shared with future pages.

## Unverified assumptions

- WSL2 localhost forwarding makes the dashboard reachable from a Windows browser.
- PX4's `ubuntu.sh` installs a Gazebo Harmonic build that works with WSLg on your GPU driver.
- The generated SDF world is accepted by Gazebo Harmonic as written.
- PX4 v1.17.0 remains the right pin once ROS 2 `px4_msgs` enters in Phase 2.
