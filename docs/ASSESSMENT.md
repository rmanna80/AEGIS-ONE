# Current-state assessment (2026-11-14)

What exists in this repository today, sorted by the strongest evidence behind it. A phase
being marked "done" earlier was not taken as evidence; each row names what was actually run.

Evidence levels, strongest first:

- **Hardware-tested**: ran on physical equipment.
- **Verified in simulation**: ran end to end against PX4 SITL, with a recorded result.
- **Unit-tested only**: the logic has tests; it has not run against the real thing.
- **Partial**: some of the capability exists.
- **Missing**: nothing exists.

"Independently validated" would mean someone other than the developers reproduced the result
with their own procedure. **Nothing here has been independently validated.** The owner
re-running the demos on a second machine is repetition, which is useful, but it is the same
procedure and the same pass criteria.

No physical aircraft, flight controller, camera, depth sensor, thermal sensor or radio has
ever been connected. The only physical device used is a Raspberry Pi 5 as the computer
running the backend, flying a simulated vehicle.

## Capabilities

| Capability | Level | Evidence | Main gap |
| --- | --- | --- | --- |
| Telemetry, 3D dashboard, provenance labels | Verified in simulation | Demo 1, 5 of 5 (sandbox, owner's PC, owner's Pi 5) | Never fed physical telemetry |
| Mission planning, validation, geofence, execution | Verified in simulation | Demo 1; 2 live SITL tests | Simulator only by design |
| Operator Hold / Return / Land | Verified in simulation | Hand checks; Demos 5 and 6 | Not an automated test |
| Simulator attestation (commands refused otherwise) | Unit-tested, plus every live run | `test_px4_attestation.py`, `test_hardware.py` | The refusal path never met a real autopilot |
| Flight recording and replay | Verified in simulation | Live replay of a packaged incident today | Recordings of simulated flights only |
| Object detection and tracking (RGB) | Verified in simulation | Demo 2 on the owner's Gazebo | Not run on the Pi in flight; no physical camera |
| Depth mapping and route checking | Verified in simulation | Demo 4, 5 of 5 on Gazebo after compass calibration | Recall passes by under 0.01; pose taken as given; no SLAM |
| Thermal search and operator review | Verified in simulation | Demo 3, 8 of 8 (sandbox, PC, Pi 5) | Thermal model is software, not rendered or measured |
| Two-vehicle fleet, shared frame, route conflicts | Verified in simulation | Demo 5, 8 of 8 | Automatic separation Hold never triggered live |
| Anomaly detection (8 rule checks) | Verified in simulation | Demo 6, 8 of 8, re-run today | First vehicle only; thresholds tuned on one simulator |
| Hash-chained audit log | Verified in simulation | Demo 6; `verify_audit.py` | Final hash has no external anchor |
| Fault injection (labelled, simulator only) | Verified in simulation | Demo 6; TRUST scenarios | Alters AEGIS's copy of the data, not the vehicle's |
| Raspberry Pi 5 as the backend computer | Hardware-tested (compute only) | Benchmark; Demo 1 and Demo 3 with the Pi in the loop | See `docs/hardware/README.md` for what was not measured |
| Device health monitor | Hardware-tested (readings) | Real temperature and power flags read on the Pi 5 | Alert thresholds only exercised with stand-in values |
| Read-only monitoring of a physical autopilot | Unit-tested only | `test_hardware.py` | Never attached to one |
| Session manifest, incident packages, timeline, playback controls (AEGIS REPLAY) | Verified in simulation | Today: incident captured live, verified, replayed; 11 unit tests | One vehicle; no camera or map data in packages |
| Scenario runner and results (AEGIS TRUST) | Verified in simulation | Today: 2 scenarios, PASS on all 7 criteria each; 15 unit tests | Two scenarios; one run each per configuration |
| Hardware interface inventory and operation classes | Implemented | `/api/hardware`; `test_hardware.py` | An inventory, not a validation |
| Wind, GNSS-denied flight, SLAM, map uncertainty | Missing | | |
| USB webcam (physical, bench mount) | Hardware-tested (on the owner's Pi 5) | Ran in flight with Demo 1 passing: 2.7 frames/s, 335 ms per detection | Format and delivered rate not yet recorded; bench only |
| Physical depth and thermal adapters, Pi camera module | Missing | | |
| Hardware-in-the-loop | Missing | | Named and refused |
| Physical flight control | Missing, by design | | Refused at start-up |
| Requirements, hazards, traceability (Phase 11) | Implemented | 23 requirements, 10 hazards, `scripts/trace.py`; link check runs in CI | Developer-written; no formal hazard analysis, no assurance cases, no independent review |
| Signed incident packages, device key (Phase 12) | Unit-tested, plus live packaging | `test_identity.py` (edit, re-sign, tamper cases) | Key protected only by file permissions; no trusted time |
| MAVSDK port firewall | Hardware-tested (owner's Pi) | Reachable from the PC before the rule, timed out after | Lost on reboot unless installed as a service |
| MAVLink signing | Not possible | PX4 v1.17 has none | |
| Regression comparison between suite runs (Phase 13) | Unit-tested only | `test_assurance.py` | Only one suite run exists so far, so no real comparison has been made |
| Customer discovery (Phase 15) | Missing | | No prospective user has been asked anything |

## Defects found and fixed during this assessment

Each was found by running something, not by reading code.

1. **All PX4 data was labelled `simulated` regardless of evidence.** The adapter carried the
   label as a constant. An autopilot that failed the simulator check would still have been
   shown as simulated. Now: `simulated` only after the autopilot proves it, `physical` once it
   answers without that proof, `unknown` before either.
2. **Nothing was recorded while the vehicle link was down.** A link loss looked the same in a
   recording as a recorder fault. The first TRUST run measured it: 79% of the expected
   records, 6.1 s missing. States are now recorded throughout, marked `connected=false`.
3. **False battery alarm on a second flight.** The simulator's battery recovers on the ground;
   the next take-off looked like a level that "rose in flight". Found by running two TRUST
   scenarios back to back.
4. **Replay reported the wrong original provenance.** A replayed state's own label was
   overwritten before it was read, so replay fell back to the file header (`unknown` if the
   file was opened before the simulator identified itself).
5. **A seek before playback started was discarded.**
6. **An incident replay showed no mission** until the mission next changed, because the
   mission status in force at the start of the window was outside the slice.
7. **The dashboard did not build** after `unknown` provenance was added (one component had no
   style for it). The Python tests could not see this.

## Defects found and fixed in the second iteration

Found by the new TRUST scenarios on their first live runs:

8. **Stale data could never be detected from the PX4 adapter.** It stamped each state with
   the time AEGIS sampled it, not when the data arrived, so the data's age was always about
   zero. When the MAVSDK bridge crashed, the 3 s outage was reported as `telemetry.frozen`
   (data arriving but unchanged), which is the wrong diagnosis, and no stale-data alert
   could fire. States now carry the time their data arrived.
9. **The frozen-data check counted a stall as frozen data.** It now only counts new data
   that says the same thing; no new data is the stale-data check's job.
10. **The delay fault was unrealistic** (a test tool defect, not AEGIS's): at onset it
    jumped the view back in time, which AEGIS correctly reported as a clock reversal. A
    slowing link goes quiet, then delivers late; it never goes back.

The first crash scenario also expected the wrong alert (`link.lost`); the outage was
shorter than the 3 s link timeout, so the right alert is stale data. The scenario now
accepts either, and it fails if the stall is misdiagnosed as frozen data.

## What this assessment does not establish

- That any threshold (anomaly limits, separation distances, detection times) is right for a
  real vehicle. They were chosen against one simulator.
- That the Pi 5 has enough capacity for perception and mapping together during a flight. The
  benchmark timed each alone, on synthetic input.
- That the system is safe. Passing tests show the listed behaviours under the listed
  conditions and nothing more. AEGIS does not provide regulatory certification.
