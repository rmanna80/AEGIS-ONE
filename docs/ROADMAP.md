# Roadmap, limitations and technical debt

## Phase status

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and setup | Done |
| 1 | Working 3D simulation, telemetry, waypoint mission, dashboard | Done (SIH verified; Gazebo launch confirmed by the owner) |
| 2 | Perception: simulated RGB camera, detection, tracking, recording | In progress: detection, tracking, video panel and recording done; Gazebo camera link unverified; playback missing |
| 3 | Navigation and mapping: depth, occupancy, planning, wind | Not started |
| 4 | Thermal and search and rescue | Not started |
| 5 | Multi-drone simulation | Not started |
| 6 | Cybersecurity and anomaly detection | Not started |
| 7 | Raspberry Pi integration | Not started |
| 8 | Physical hardware readiness (documentation and gating only) | Not started |

## Next milestone: finish Phase 2

1. Run the Gazebo camera path (`run_px4.sh gazebo-cam` + `gz_camera_bridge.py`) and fix
   whatever its first launch turns up.
2. Environment B (urban) with human-shaped and vehicle models, so there is something to detect
   in simulation; then Demo 2 with acceptance criteria.
3. Synchronized playback of recorded detections with telemetry (store video alongside the
   detection log).
4. Precision / recall on a meaningful labelled set, and on Gazebo-rendered imagery.
5. Decide whether ROS 2 is needed before Phase 3. The camera link currently uses Gazebo's own
   transport, which avoided a ROS 2 install; mapping libraries in Phase 3 will likely need it.

## Known defects and limitations

- Tracker has no appearance model: an occlusion longer than the coast limit produces a new ID,
  and double detections can create a duplicate track.
- Coast limit is counted in processed frames, so its duration depends on inference speed.
- Detection model licence is AGPL-3.0 (Ultralytics). Fine for private research; check it before
  distributing a product. The backend interface accepts any compatible ONNX model.
- No user-defined object classes yet (needs a fine-tuned model).
- Camera frames claiming `physical` provenance are refused until Phase 7.
- Replay has no pause or seek; speed and file are fixed at backend start.
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
