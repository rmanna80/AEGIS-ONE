# Roadmap, limitations and technical debt

## Phase status

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture and setup | Done |
| 1 | Working 3D simulation, telemetry, waypoint mission, dashboard | Done with PX4 SIH; Gazebo launch unverified |
| 2 | Perception: simulated RGB camera, detection, tracking, recording | Next |
| 3 | Navigation and mapping: depth, occupancy, planning, wind | Not started |
| 4 | Thermal and search and rescue | Not started |
| 5 | Multi-drone simulation | Not started |
| 6 | Cybersecurity and anomaly detection | Not started |
| 7 | Raspberry Pi integration | Not started |
| 8 | Physical hardware readiness (documentation and gating only) | Not started |

## Next milestone: finish Phase 1 on Gazebo, then start Phase 2

1. On the WSL2 machine: run `scripts/setup_wsl.sh`, launch `run_px4.sh gazebo`, and pass Demo 1
   and the simulation tests against the x500 in the generated world. Fix whatever the first
   Gazebo launch turns up.
2. Add ROS 2 Jazzy and `ros_gz_bridge`; define the `CameraSource` protocol with a
   `GazeboCameraAdapter` and a `VideoFileCameraAdapter`.
3. Detection behind an `InferenceBackend` protocol (ONNX Runtime on CPU first, so no GPU is
   required), with detections stored and replayed alongside telemetry.
4. Kalman + association tracker with ID-switch metrics.
5. Dashboard video panel with boxes and track IDs.

## Known defects and limitations

- Gazebo path never launched (see docs/simulation).
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
