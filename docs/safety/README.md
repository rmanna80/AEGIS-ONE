# Safety architecture

AEGIS ONE is a research tool for simulation. Nothing in this repository can fly a physical
aircraft, and nothing here is evidence that one would be safe to fly.

## Principles and how they are enforced

| Principle | Mechanism | Where |
| --- | --- | --- |
| The flight controller stays in charge | AEGIS only requests PX4's own modes (Mission, Hold, Return, Land). No offboard setpoints, no actuator commands. | `adapters/px4_sitl.py` |
| A human authorises every command | Commands need the operator token; the dashboard adds an explicit confirm step before arming | `api.py`, `MissionPanel.tsx` |
| The operator can always intervene | Hold, Return and Land are available whenever a vehicle is connected, independent of mission state | `api.py` |
| Know what you are connected to | Commands are refused until `SYS_AUTOSTART` matches a known simulation airframe **and** simulation-only parameters can be read (`SIM_BAT_ENABLE`; plus `SIM_GZ_EN=1` for Gazebo). Retried until it succeeds; reset on link loss. | `PX4SITLAdapter._attest_simulator` |
| Simulation and reality are never confused | Every state, alert and recording carries provenance; replays are re-labelled | `contracts.py`, `recording.py` |
| Physical flight cannot be enabled by accident | `AEGIS_MODE=physical` is rejected at startup; no hardware executor exists | `config.py` |
| Missions are checked before they are sent | Waypoints, legs and the return leg must stay inside the geofence and altitude limits | `missions.validate_mission` |
| The geofence is enforced by the autopilot | The fence polygon is uploaded to PX4; AEGIS only monitors it | `px4_sitl.py`, `safety.py` |
| Do not trust acknowledgements | Mission start is confirmed by observing PX4's flight mode, with bounded retries | `px4_sitl.py` |
| Unreachable waypoints do not hang forever | Per-leg timeout, then Hold and a `stalled` status | `px4_sitl._supervise_mission` |
| Monitors advise; the one automatic action is to stop | Alerts for link loss, stale data, battery, GNSS, estimator, geofence and obstacles. When the route ahead is blocked or depth sensing is lost during a mission, AEGIS requests PX4's Hold mode once and reports `blocked`. It never steers or resumes; the operator plans a detour or intervenes. Can be turned off with `AEGIS_NAV_AUTO_HOLD=false`. | `safety.py`, `navigation/service.py` |
| Routes are checked against obstacles before upload | A mission passing within the clearance distance (3 m) of a known or mapped obstacle is refused and never sent | `navigation/planner.py`, `api.py` |
| Detections are for a person to judge | Heat-source candidates start `unreviewed`; only an authenticated operator decision makes one `confirmed` or `dismissed`. Descriptions give size and temperature, never identity. | `sar/candidates.py`, `api.py` |
| Finding something never moves the vehicle | The search service is constructed without the mission executor, so it has no way to command the vehicle; a candidate or a confirmation triggers no flight action. Tests assert no command is issued. | `api.build_sar`, `sar/service.py`, `tests/test_sar.py` |
| Vehicles do not coordinate on their own | Each vehicle flies only a mission the operator submitted to it. There is no vehicle-to-vehicle tasking and no following. | `fleet.py`, `api.py` |
| Routes of different vehicles are kept apart | A mission passing within 10 m of a route another vehicle is flying, without 5 m of height between them, is refused before upload. Timing is ignored, which refuses some safe routes and never accepts a route on the hope of a miss. | `fleet.route_conflicts` |
| Two vehicles that get too close are stopped, not steered | Within 6 m horizontally and 4 m vertically, AEGIS requests Hold for both, once per encounter, and reports `blocked`. Warning from 12 m / 6 m. Can be turned off with `AEGIS_FLEET_AUTO_HOLD=false`. | `fleet.check_separation` |
| The operator can stop everything at once | Hold all commands every connected vehicle and reports per vehicle | `api.py`, `FleetPanel.tsx` |
| Anomaly detection advises and never acts | The anomaly monitor reads telemetry and raises alerts. It is built without the mission executor, so it cannot command the vehicle or interfere with the autopilot's own failsafes. | `anomaly.py`, `tests/test_security.py` |
| Commands leave a record that shows tampering | Every command request and outcome is appended to a hash-chained audit log; the token is never written | `security.AuditLog`, `scripts/verify_audit.py` |
| Test faults cannot be mistaken for real data or reach a real vehicle | Fault injection is off by default, needs the operator token and an attested simulator, alters only AEGIS's copy of telemetry, and labels every altered snapshot | `faults.py`, `api.py` |

## Limits of these mechanisms

- **Attestation is a parameter check, not proof.** The first version checked `SYS_AUTOSTART`
  only, which was unsound: PX4 hardware firmware reuses simulation numbers (4001 is "Generic
  Quadcopter" on a real Pixhawk). It now also requires parameters that exist only in PX4's
  POSIX simulation builds, which stock hardware firmware cannot answer. A custom firmware
  build could still defeat it. Physical operation will need its own adapter, a validated
  configuration and explicit authorisation; it must not reuse this check.
- **The operator token is a shared secret over plain HTTP on localhost.** It is a guard against
  accidental commands, not an authentication system. Users, roles and audit logs are Phase 6.
- **PX4's geofence breach action is left at its default** and a breach has not been provoked in
  simulation yet.
- **Low-battery, GNSS-loss and estimator alerts are unit-tested with injected states only.**
  They have not been triggered by faults inside the simulator.
- **Watchdog scope**: link loss is detected from missing telemetry within 3 s of simulation
  time (the timeout stretches when the simulator runs slowly, up to 60 s of wall time). The
  MAVSDK server process is restarted if it exits; while it is down AEGIS cannot see or command
  the vehicle, which stays under PX4's control. There is no watchdog on the backend itself.

## Limits of obstacle avoidance

AEGIS cannot guarantee collision avoidance, in simulation or anywhere else.

- **Unobserved space is treated as free.** The planner is optimistic about what the depth sensor
  has not seen; the route monitor only catches obstacles once they are mapped, within the
  sensor's 19 m range and field of view. Something approached from outside the field of view
  (from the side, above or below) is not seen.
- **Stopping distance is not modelled.** Hold is commanded when a conflict is found within 30 m
  of route ahead. At 4 m/s the live test stopped 11.7 m short; at higher speeds or with a
  shorter-range sensor there may not be room.
- **PX4's return path is fixed.** Return-to-home climbs to 30 m and flies straight home. That
  leg is checked before take-off, but AEGIS cannot re-route it.
- **Pose and depth are paired approximately.** Frames from a network depth source are fused with
  the latest telemetry pose, ignoring transport latency, and the position estimate itself is
  noisy (about 1 m in altitude in simulation). Clearance margins have to absorb both.
- **Thin and moving objects.** 0.5 m voxels and a static-world assumption: wires, branches and
  anything that moves are out of scope.
- **The software depth sensor sees exactly the scenario's geometry.** It is a sensor model, not
  a rendering; results with it are an upper bound on what a real or Gazebo depth camera gives.

## Fault-injection status (brief section 9)

| Fault | Injected how | Result |
| --- | --- | --- |
| Communication timeout / link loss | Unit test (`connected=False`) | `link.lost` critical alert; other checks suppressed |
| Stale telemetry | Unit test | `telemetry.stale` critical alert |
| Battery degradation | Unit test, hub-level | warning at 25%, critical at 15%, clears on recovery |
| GPS outage / degradation | Unit test | `gps.no_fix` critical, `gps.degraded` warning |
| Invalid local position while airborne | Unit test | `estimator.position` critical |
| Mission not started by PX4 | Occurred for real during development | detected, `stalled`, Hold commanded; start logic then hardened |
| Camera disconnect | Live (video sender stopped) | `camera.no_frames` warning |
| Excessive inference latency | Unit test of the status path | `inference.slow` warning above 500 ms p95 |
| Depth sensing lost during a mission (mapping failure) | Unit test | `depth.stale` critical, Hold commanded once |
| Obstacle on the route | Live against PX4 SITL | `route.blocked` critical, Hold commanded, stopped 11.7 m short |
| IMU anomalies | Not applicable yet | Phase 6 |

## Before any hardware is connected (Phase 8, not started)

Simulation results do not transfer automatically. Hardware work requires, at minimum: a separate
hardware adapter and configuration, bench tests with propellers removed, a manual-override
(RC) path that does not depend on AEGIS, PX4 failsafes configured and tested on the airframe,
compliance with local aviation rules, and a written operator checklist.
