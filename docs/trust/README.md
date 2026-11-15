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
python scripts/trust_suite.py --token <token> trust/suites/core.json   # five scenarios x 3
python scripts/compare_results.py <earlier suite result> <later suite result>
```

Each scenario takes about 100 s: take off, fly the demo mission, 8 s of baseline, the
fault, measurement, Return and landing, evidence check. The suite takes about 40 minutes.

| Scenario | Fault | What it checks |
| --- | --- | --- |
| `link_loss_detection` | AEGIS's view of the link goes down for 6 s | `link.lost` alert, clears, evidence |
| `stale_telemetry_detection` | The same snapshot keeps arriving for 6 s | `telemetry.frozen` alert, labelled, evidence |
| `gnss_loss_detection` | No GNSS fix for 6 s | `gps.no_fix` alert, clears, evidence |
| `telemetry_delay_detection` | Data delivered 3 s late for 8 s | `telemetry.stale` alert, no false clock alarm |
| `bridge_crash_recovery` | AEGIS's MAVSDK bridge process is killed | data-stopped alert, restart, PX4 keeps flying, commands work after |

Every scenario also checks for alerts the fault should *not* cause, and that the
operator's Return works after the test. The result is printed and saved to
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

Suite `core`, 5 scenarios × 3 runs, PX4 v1.17.0 SITL (built-in simulator), development
machine (x86, 2 vCPU), 2026-11-14, clean commit `aa95493`. **All 15 runs passed; every
criterion passed 3 of 3.** Saved in `data/sample/trust_results/` (the suite summary and
each run). The MAVSDK-port firewall rule was in place from the 2nd run on.

| Scenario | Safety-critical criterion | Limit | Observed (min / median / max) |
| --- | --- | --- | --- |
| Link loss | `link.lost` raised | 3 s | 0.20 / 0.20 / 0.20 s |
| Frozen data | `telemetry.frozen` raised | 4 s | 2.01 / 2.02 / 2.23 s |
| GNSS loss | `gps.no_fix` raised | 1 s | 0.20 / 0.20 / 0.20 s |
| Delayed data (3 s) | `telemetry.stale` raised | 2.5 s from onset | 2.22 / 2.22 / 2.22 s |
| Bridge crash | data-stopped alert raised | 3 s | 2.22 / 2.22 / 2.23 s |
| Bridge crash | operator's Return works after recovery | lands | 3 of 3 |

Every scenario also passed, in all three runs: no monitored alert before the fault; no
other alert caused by the fault; the mission not interrupted by the monitors (the crash
scenario instead checks that AEGIS reports it lost track of the mission, and that PX4
kept flying it); an incident package produced, verifying, with 100% of the expected state
records; the operator's Return accepted and the vehicle landed. Alerts cleared within
0.27 s of the fault ending (link, GNSS, delay) and within 4.04 s for the bridge crash,
whose restart takes about 2 s.

Detection times are sampled every 0.2 s, so 0.20 s means "by the first sample". The
2.2 s figures for stale data follow from the design: data is flagged once it is older than
2 s.

## Runs that did not pass, and what they found

Before the suite, each new scenario was run once. Two did not pass, and both found real
problems that were then fixed:

- **Bridge crash, first run: FAIL.** No data-stopped alert appeared; `telemetry.frozen`
  did. The PX4 adapter stamped every state with the time AEGIS *sampled* it, so the age of
  the data was always zero and stale data could never be detected; the stall looked like
  frozen data. Fixed in the adapter, and the frozen check no longer counts a stall. The
  scenario also expected the wrong alert (`link.lost`): the outage was shorter than the
  3 s link timeout. It now accepts either data-stopped alert, and fails on the
  misdiagnosis.
- **Delayed data, first run: FAIL** on "no other alert": a clock-reversal alert. That was
  a defect in the fault, not in AEGIS: it jumped the view back 3 s at onset, and AEGIS was
  right to flag it. The fault now goes quiet, then delivers late, never backwards. The
  detection limit was rewritten from the requirement (data older than 2 s flagged within
  0.5 s), since data under a 3 s delay only becomes 2 s old at the 2 s mark.

Earlier, the first link-loss run found that nothing was recorded while the link was down
(79% of expected records); one run was INCONCLUSIVE because a new criterion had no
measurement wired to it; and one frozen-data run failed its baseline on a false battery
alert. All fixed.

## What these results do not show

- The fault is injected into AEGIS's copy of the telemetry. The vehicle keeps receiving
  correct data and flies on. This tests AEGIS's detection and evidence handling, not the
  vehicle's behaviour under a real link loss or a real sensor fault.
- The bridge crash is real (the process is killed) but the other four faults alter only
  AEGIS's copy of the data.
- One simulator, one machine, one vehicle, three runs each. The spread is of these runs
  only; it is not a statistical guarantee.
- The limits (3 s, 5 s, 95%) were written by the developers. They are not derived from a
  hazard analysis and are not a standard.
- Written and run by the same party. This is not independent validation and not a safety
  certification.

## Not built yet

More fault kinds (packet loss, sensor dropout, storage full, CPU and memory pressure),
faults that reach the simulated vehicle rather than AEGIS's copy, crashes of the backend
itself and of the camera bridge, a results view on the dashboard, and running the suite
automatically (it needs PX4 SITL, which CI does not build). Comparison between suite runs
exists but has only been exercised by unit tests: one suite run exists so far.
