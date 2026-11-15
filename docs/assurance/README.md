# Assurance: requirements, hazards, evidence

Three files and two scripts. No separate tool, no database.

| File | What it holds |
| --- | --- |
| `assurance/requirements.json` | What AEGIS must do, each with an ID, whether it is safety-critical, and the checks that verify it (unit tests and scenario criteria) |
| `assurance/hazards.json` | What could go wrong, which requirements address it, and what is left over (residual) |
| `docs/assurance/TRACEABILITY.md` | A generated snapshot: each requirement's status from the latest results |

```bash
python scripts/trace.py                 # run the unit tests, read scenario results, report
python scripts/trace.py --write-docs    # also refresh docs/assurance/TRACEABILITY.md
python scripts/compare_results.py <earlier suite result> <later suite result>
```

## Statuses

The weakest evidence decides, and there is no overall score.

| Status | Meaning |
| --- | --- |
| FAILED | A linked check failed in its latest results |
| NOT VERIFIED | A linked check has no result, was skipped, or gave no verdict |
| VERIFIED IN SIMULATION | Every linked check passed, at least one of them a scenario flown on PX4 SITL |
| VERIFIED BY UNIT TESTS | Every linked check passed, all of them unit tests |

Scenario evidence uses every run made with the newest software commit for that scenario,
so a criterion that passed two runs of three is FAILED, not "67%". Safety-critical
requirements that are not verified are listed at the top of the report.

Hazards are marked open when a requirement that addresses them is not verified, or when
their residual risk is itself an open item. H-5 (someone other than the operator commands
the vehicle) stays open whatever the tests say: the MAVSDK control port cannot be
restricted by the software, and the firewall that closes it has not been checked from
another computer.

## Keeping it honest

- `backend/tests/test_assurance.py` runs in CI and fails when a requirement points to a test
  or criterion that no longer exists, when a hazard names a missing requirement, or when a
  requirement addresses no hazard. Its first run found one (R-HONEST-1 was in no hazard).
- Verified means the linked checks passed. It does not mean the requirement is the right
  one, that the checks are enough, or that the system is safe.
- The requirements and hazards were written by the developers from the brief and the code.
  There has been no formal hazard analysis (no FMEA, no STPA), no likelihood or severity
  estimate, and no independent review.
- All scenario evidence is from simulation. Nothing is verified on physical hardware.

## Comparing two suite runs

`compare_results.py` reports a verdict change as REGRESSION or FIXED, and reads timings
conservatively: a timing has moved only when the later median lies outside the whole
range of the earlier runs, with at least two runs on each side. Anything else is "no
difference shown by these runs". With three runs a side, a lower median inside the old
range is not an improvement, and the script does not call it one.
