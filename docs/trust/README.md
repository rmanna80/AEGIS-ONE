# AEGIS TRUST

Runs a written scenario against the running system, injects a fault, measures what
happened, and gives each pass criterion a verdict with the evidence behind it.

It is a script and a small package (`backend/aegis/trust/`, `scripts/trust_run.py`) that
drive the public API, the same one the dashboard uses. It reuses the fault injector, the
anomaly monitor, the demo mission and REPLAY's incident packages.

## Run

```bash
bash scripts/run_px4.sh sih                                      # terminal 1
AEGIS_FAULT_INJECTION=true bash scripts/run_backend.sh           # terminal 2
python scripts/trust_run.py --token <token> trust/scenarios/link_loss_detection.json
python scripts/trust_run.py --token <token> trust/scenarios/stale_telemetry_detection.json
```

Each takes about 100 s: take off, fly the demo mission, 8 s of baseline, the fault,
measurement, evidence check, return and land. The result is printed and saved to
`data/generated/trust_results/`.

## Scenario file

`trust/scenarios/*.json`, schema `aegis.trust-scenario/1`. Unknown fields are rejected. A
scenario states its purpose, the environment it needs, initial conditions, the fault, the
expected behaviour, the pass criteria and its own limitations. The file's SHA-256 is stored
in the result, so a result always identifies the exact scenario text it was run from.

Scenarios have no randomised element yet (`random_seed` is `null`). The simulator is not
deterministic run to run, so timings vary slightly between runs.

## Verdicts

| Status | Meaning |
| --- | --- |
| PASS | Measured, and met the criterion |
| FAIL | Measured, and did not |
| INCONCLUSIVE | Ran, but the measurement needed for a verdict is missing |
| BLOCKED | Could not start: the environment does not match what the scenario requires |
| NOT RUN | Not attempted |
| SKIPPED | Deliberately left out |

Rules, each covered by a unit test:

- A scenario passes only if **every** criterion passes. One failure fails it however many
  others pass. There is no score and no average.
- A missing measurement is never a pass.
- "The alert cleared" cannot pass if the alert was never raised.
- The runner refuses (BLOCKED, nothing commanded, no fault injected) unless the vehicle has
  proved it is a simulator, the data is labelled `simulated`, the expected scenario is
  loaded, fault injection is on and recording is on.
- A successful start-up is not a test. Every criterion is a measurement taken during the run.

## Results on record

PX4 v1.17.0 SITL, built-in simulator, development machine (x86, 2 vCPU), 2026-11-14, commit
`c442125` plus the changes in this iteration. Saved in `data/sample/trust_results/`.

| Scenario | Criterion | Limit | Observed |
| --- | --- | --- | --- |
| Link loss | No alert before the fault | none | none |
| | `link.lost` raised (safety-critical) | 3 s | 0.20 s |
| | Alert clears after the link returns | 5 s | 0.26 s |
| | Mission not interrupted by the monitors | stays active | active |
| | Incident packaged | yes | yes |
| | Package verifies | yes | yes |
| | Record complete | 95% | 100% |
| Stale telemetry | No alert before the fault | none | none |
| | `telemetry.frozen` raised (safety-critical) | 4 s | 2.22 s |
| | Altered data labelled | yes | yes |
| | Mission not interrupted | stays active | active |
| | Incident packaged, verifies, complete | yes / yes / 95% | yes / yes / 100% |

Detection times are sampled every 0.2 s, so 0.20 s means "by the first sample".

The link-loss scenario was run five times and the stale-telemetry scenario four times
during this iteration. Not every run passed:

- The first link-loss run passed its six criteria of the time, but its incident package
  held only 79% of the expected records: nothing was recorded while the link was down. A
  completeness criterion (C7) was added and the recorder fixed.
- One link-loss run was INCONCLUSIVE: the new criterion had no measurement wired to it (a
  defect in the runner, fixed).
- One stale-telemetry run FAILED its baseline: a false battery alert appeared before the
  fault, on a second consecutive flight (a defect in the anomaly monitor, fixed).

Both scenarios pass on the code as committed, run back to back.

## What these results do not show

- The fault is injected into AEGIS's copy of the telemetry. The vehicle keeps receiving
  correct data and flies on. This tests AEGIS's detection and evidence handling, not the
  vehicle's behaviour under a real link loss or a real sensor fault.
- One simulator, one machine, one vehicle, a handful of runs. No statistics.
- The limits (3 s, 5 s, 95%) were written by the developers. They are not derived from a
  hazard analysis and are not a standard.
- Written and run by the same party. This is not independent validation and not a safety
  certification.

## Not built yet (Phase 10 onward)

Scenario suites and batch runs, repeated runs with statistics, more fault kinds (packet
loss, delay, sensor dropout, software crash and restart, storage full, resource
exhaustion), faults that reach the simulated vehicle rather than AEGIS's copy, crash
recovery tests, comparison between two software versions, and a results view on the
dashboard.
