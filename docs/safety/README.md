# Safety architecture

AEGIS ONE is a research tool for simulation. Nothing in this repository can fly a physical
aircraft, and nothing here is evidence that one would be safe to fly.

## Principles and how they are enforced

| Principle | Mechanism | Where |
| --- | --- | --- |
| The flight controller stays in charge | AEGIS only requests PX4's own modes (Mission, Hold, Return, Land). No offboard setpoints, no actuator commands. | `adapters/px4_sitl.py` |
| A human authorises every command | Commands need the operator token; the dashboard adds an explicit confirm step before arming | `api.py`, `MissionPanel.tsx` |
| The operator can always intervene | Hold, Return and Land are available whenever a vehicle is connected, independent of mission state | `api.py` |
| Know what you are connected to | Commands are refused until `SYS_AUTOSTART` matches a known simulation airframe (10040 SIH, 4001 gz_x500) | `PX4SITLAdapter._attest_simulator` |
| Simulation and reality are never confused | Every state, alert and recording carries provenance; replays are re-labelled | `contracts.py`, `recording.py` |
| Physical flight cannot be enabled by accident | `AEGIS_MODE=physical` is rejected at startup; no hardware executor exists | `config.py` |
| Missions are checked before they are sent | Waypoints, legs and the return leg must stay inside the geofence and altitude limits | `missions.validate_mission` |
| The geofence is enforced by the autopilot | The fence polygon is uploaded to PX4; AEGIS only monitors it | `px4_sitl.py`, `safety.py` |
| Do not trust acknowledgements | Mission start is confirmed by observing PX4's flight mode, with bounded retries | `px4_sitl.py` |
| Unreachable waypoints do not hang forever | Per-leg timeout, then Hold and a `stalled` status | `px4_sitl._supervise_mission` |
| Monitors advise, they do not act | Alerts for link loss, stale data, battery, GNSS, estimator and geofence | `safety.py` |

## Limits of these mechanisms

- **Attestation is a parameter check, not proof.** It stops an accidental connection to an
  ordinary real vehicle. It does not defend against a real autopilot deliberately configured
  with a simulation airframe id. Physical operation will need its own adapter, a validated
  configuration and explicit authorisation; it must not reuse this check.
- **The operator token is a shared secret over plain HTTP on localhost.** It is a guard against
  accidental commands, not an authentication system. Users, roles and audit logs are Phase 6.
- **PX4's geofence breach action is left at its default** and a breach has not been provoked in
  simulation yet.
- **Low-battery, GNSS-loss and estimator alerts are unit-tested with injected states only.**
  They have not been triggered by faults inside the simulator.
- **Watchdog scope**: link loss is detected from missing telemetry within 3 s. There is no
  watchdog on the backend process itself.

## Fault-injection status (brief section 9)

| Fault | Injected how | Result |
| --- | --- | --- |
| Communication timeout / link loss | Unit test (`connected=False`) | `link.lost` critical alert; other checks suppressed |
| Stale telemetry | Unit test | `telemetry.stale` critical alert |
| Battery degradation | Unit test, hub-level | warning at 25%, critical at 15%, clears on recovery |
| GPS outage / degradation | Unit test | `gps.no_fix` critical, `gps.degraded` warning |
| Invalid local position while airborne | Unit test | `estimator.position` critical |
| Mission not started by PX4 | Occurred for real during development | detected, `stalled`, Hold commanded; start logic then hardened |
| Camera disconnect, IMU anomalies, mapping failure, inference latency | Not applicable yet | Phases 2 to 6 |

## Before any hardware is connected (Phase 8, not started)

Simulation results do not transfer automatically. Hardware work requires, at minimum: a separate
hardware adapter and configuration, bench tests with propellers removed, a manual-override
(RC) path that does not depend on AEGIS, PX4 failsafes configured and tested on the airframe,
compliance with local aviation rules, and a written operator checklist.
