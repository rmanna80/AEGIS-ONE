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
| 7 | Raspberry Pi integration | Built: setup script, companion MAVLink link, device health monitor, benchmark, systemd unit, guide. Done for the core path: installed, benchmarked and flown on the owner's Pi 5 (Demo 1, 5 of 5; Demo 3, 8 of 8; simulator on the PC). Demos 2, 4, 5 and 6 on the Pi not yet run. |
| 8 | Hardware readiness and current-state assessment | Done as software and documentation: provenance by evidence, operation classes, interface inventory, Pi review, flight-controller readiness, measurement script. Pi measurements await the owner. See `docs/ASSESSMENT.md`, `docs/hardware/README.md`. |
| 9 | AEGIS REPLAY | First end-to-end version built and verified live on PX4 SITL: session manifest, incident capture and packaging with a SHA-256 manifest, export/import, timeline, pause/seek/speed. See `docs/replay/README.md`. |
| 10 | AEGIS TRUST scenario engine | Started: scenario and result contracts, a runner, two scenarios passing live. See `docs/trust/README.md`. |
| 11 | Independent assurance (requirements, traceability, inventory, assurance cases) | Not started |
| 12 | Cybersecurity and distributed trust | Not started (Phase 6 groundwork exists) |
| 13 | Continuous validation and regression | Not started (results are saved in a comparable format) |
| 14 | Physical hardware validation | Not started. No physical flight hardware has been connected. |
| 15 | Product demonstration, research report, customer discovery | Not started. No market claim has been tested with a prospective user. |

The product names map onto the one codebase; nothing was split out:

| Name | What it is in this repository |
| --- | --- |
| AEGIS AIR | The simulated vehicle path: PX4 adapter, missions, navigation, search, fleet |
| AEGIS EDGE | The backend running on a companion computer (Raspberry Pi 5), device monitor |
| AEGIS CORE | Contracts, hub, safety and anomaly monitors, audit log |
| AEGIS COMMAND | The dashboard and operator API |
| AEGIS REPLAY | `backend/aegis/replay/`, `recording.py` |
| AEGIS TRUST | `backend/aegis/trust/`, `trust/scenarios/`, `scripts/trust_run.py` |

New services or applications are added only when one process can no longer do the job.

## Plan

### Phase 9: finish REPLAY

1. Camera video, detections and map revisions in incident packages (they are recorded per
   session already; the package only needs to slice them).
2. Fleet sessions: one flight log per vehicle, one timeline.
3. An incident report (HTML/PDF) generated from `incident.json`.
4. Clock offset estimate between the companion computer and the PC, with its uncertainty,
   so two devices' events can share a timeline without hiding the error.
5. An incident browser in the dashboard that opens a past package for playback without
   restarting the backend.
6. Raw MAVLink capture (bounded ring buffer) so an incident keeps the bytes, not only
   AEGIS's interpretation of them.

### Phase 10: TRUST scenario engine

1. Scenario suites, batch runs, and N repeats with the spread reported.
2. More faults: delay, packet loss, sensor dropout, storage full, CPU and memory pressure.
3. Faults that reach the simulated vehicle (PX4 failure injection, simulator-side GNSS
   loss), kept inside the simulator.
4. Crash tests: kill the backend, the MAVSDK server and the camera bridge mid-flight;
   measure detection, recovery and what the record lost.
5. The existing demos (1 to 6) expressed as scenarios, so there is one test format.
6. A provoked separation Hold for the fleet (open since Phase 5).

### Phase 11: assurance

Requirements with identifiers, each traced to scenarios and results; a hazard list; an
inventory of components with versions and hashes; assurance cases that state claim,
argument, evidence and the gaps. A failed safety-critical requirement is shown as failed,
never averaged away.

### Phase 12: cybersecurity and distributed trust

MAVSDK server bound to localhost; MAVLink 2 signing; device identity and signed incident
manifests; an external anchor for the audit log; authenticated links between the companion
computer and the ground station. Test tooling stays inside the simulator and the owner's
own devices.

### Phase 13: continuous validation

Scenario suite in CI against PX4 SITL; results compared between commits using only
collected measurements; a regression is a changed verdict or a measurement outside its
recorded spread.

### Phase 14: physical hardware validation

Starts with the Pi measurements (`docs/hardware/README.md`, section 3), then bench work
with a flight controller with propellers removed, read-only. Flight tests are separately
planned, authorised and supervised, and are outside this software.

### Phase 15: demonstration and discovery

A scripted demonstration from recorded evidence, a research report, and interviews with
prospective users. No market opportunity is claimed until those interviews have happened.

## Carried over from earlier phases

1. Gazebo heading: resolved by calibration on the owner's machine (Demo 4 now 5 of 5). Left
   over: 2.3 deg of heading error on the pad, a 5.7 deg transient in flight, and a recall that
   passes by under 0.01. Repeat Demo 4 a few times to see the spread.
2. Find the process behind the slow memory decline (about 75 MB per 10 s) in Gazebo depth runs,
   and the fast one with stock 1080p cameras.
3. Phase 3: wind, a depth-image view, pose estimation from sensors, map uncertainty display,
   Environment D (indoor, GNSS-denied).
4. Phase 4: thermal imagery rendered by Gazebo, pose error injected into the thermal model,
   non-flat terrain, thermal replay, a Gazebo run of Demo 3.
5. Phase 5: timing-aware route deconfliction, return legs in the route check, other
   vehicles' routes on the 3D view, a vehicle selector for the planner, fleets under Gazebo.
6. Phase 6: anomaly checks for every vehicle of a fleet and under Gazebo, detection of a
   second ground station from MAVLink traffic itself, a learned model only if the rule
   checks prove insufficient.
7. Phase 7: Demos 2, 4, 5, 6 on the Pi, temperature over a long run, the systemd unit.
8. Declare adapter capabilities instead of reading them by attribute name; rename the PX4
   adapter (see `docs/hardware/README.md`, section 2).

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
- Replay's recording is fixed at backend start (pause, seek and speed are live).
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
