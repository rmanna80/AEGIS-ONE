# Roadmap, limitations and technical debt

## Phase status

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and setup | Done |
| 1 | Working 3D simulation, telemetry, waypoint mission, dashboard | Done (SIH verified; Gazebo launch confirmed by the owner) |
| 2 | Perception: simulated RGB camera, detection, tracking, recording | Done. Demo 2 reported passing on Gazebo by the owner (at 4 m; route since raised to 6 m). |
| 3 | Navigation and mapping: depth, occupancy, planning, wind | Built and verified with the software depth sensor. Gazebo depth runs on the owner's machine but the map misses its accuracy targets (heading-estimate error). Wind and SLAM not built. |
| 4 | Thermal and search and rescue | Built and verified live on PX4 SITL with a software thermal model (Demo 3: 8 of 8). No Gazebo-rendered thermal imagery; no thermal replay. |
| 5 | Multi-drone simulation | Not started |
| 6 | Cybersecurity and anomaly detection | Not started |
| 7 | Raspberry Pi integration | Not started |
| 8 | Physical hardware readiness (documentation and gating only) | Not started |

## Next milestone: heading error, then Phase 5

1. Heading estimate under Gazebo. On the pad the autopilot reports about 95 deg where Gazebo has
   the model at 90 deg; the depth map lands about half a metre east and fails Demo 4. Record the
   owner's Demo 4 numbers with the new true-pose scoring (not captured yet), find why PX4's
   heading is off in the generated world, and re-run.
2. Find the process behind the slow memory decline (about 75 MB per 10 s) in Gazebo depth runs,
   and the fast one with stock 1080p cameras.
3. Remaining Phase 3 items: wind (Gazebo wind plugin), a depth-image view in the dashboard,
   pose estimation from sensors (SLAM / visual odometry), map uncertainty display,
   Environment D (indoor, GNSS-denied).
4. Remaining Phase 4 items: thermal imagery rendered by Gazebo (its thermal camera sensor),
   pose error injected into the thermal model, terrain that is not flat, replay of thermal
   sessions, coverage shown on the 3D view, a Gazebo run of Demo 3.
5. Phase 5: multi-drone simulation.

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
