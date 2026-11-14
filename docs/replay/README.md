# AEGIS REPLAY

Records what AEGIS saw, preserves the record around an incident so it can be checked later,
and plays it back on the dashboard with a timeline.

It is part of the existing backend (`backend/aegis/replay/`, `recording.py`), not a separate
application. It reuses the flight recorder, the hub, the audit log and the dashboard.

## What it does today

| Piece | What it is |
| --- | --- |
| Session manifest | `session.json` written at start: software version and git commit, device, configuration (never the operator token), scenario and model hashes, clock domains |
| Flight log | `flight.jsonl`: every state at 10 Hz, including while the link is down; every alert, mission change and injected fault |
| Incident capture | Opens on any critical alert or when the operator marks one; closes 8 s after the alerts clear (or at 180 s) |
| Incident package | A folder with the slice of the flight log (15 s before the trigger to the close), the audit records for the period, the session manifest, an incident summary, and a SHA-256 manifest |
| Anchor | The manifest's hash is written into the hash-chained audit log when the package is made |
| Export / import | Zip download from the dashboard or API; `scripts/incident_tool.py import` |
| Playback | Any flight log or incident slice, with pause, seek and speed, and event markers |

## Try it without flying

A package captured from a live PX4 SITL flight is in `data/sample/incident_link_loss/`.

```bash
python scripts/incident_tool.py verify data/sample/incident_link_loss
AEGIS_MODE=replay AEGIS_REPLAY_FILE=data/sample/incident_link_loss/events.jsonl bash scripts/run_backend.sh
```

Open http://localhost:8000. The bar under the 3D view has play/pause, a scrub bar, speed,
and markers for the injected fault, the link loss, the alert and the recovery. Click a
marker or a row in the list to jump there.

## Capture one yourself

```bash
bash scripts/run_px4.sh sih                                      # terminal 1
AEGIS_FAULT_INJECTION=true bash scripts/run_backend.sh           # terminal 2
python scripts/trust_run.py --token <token> trust/scenarios/link_loss_detection.json   # terminal 3
python scripts/incident_tool.py list
```

## Time

Three clocks are recorded side by side on every record and are never converted into one
another:

| Field | Clock |
| --- | --- |
| `t` | This computer's monotonic clock, seconds since the recording opened. Playback and the timeline use this one |
| `utc` | This computer's wall clock. Only as good as its synchronisation; the manifest records whether the OS reported it as synchronised, or `null` if that could not be read |
| `vehicle_time_s` | The autopilot's clock: simulation time in a simulator, time since boot on hardware |

In an incident slice `t` restarts at 0 and `t_session` keeps the original value. No offset
between this computer and any other device is estimated, so events from two computers
cannot yet be put on one timeline.

## What verification does and does not show

`incident_tool.py verify` recomputes every file's hash and compares it with the manifest.

- It shows the files are the ones that were packaged.
- It does **not** show that the measurements in them were accurate, or that their source was
  trustworthy. A hash proves nothing about the truth of what was hashed.
- Someone who edits a file and rewrites the manifest produces a package that verifies on its
  own. That is caught only by comparing the manifest hash with the `incident_packaged`
  record in the session's audit log. A unit test demonstrates both halves.
- The audit log's own last hash has no external anchor, and nothing is signed. A person with
  write access to the whole session folder can forge all of it consistently. Signing and
  device identity are Phase 12.

## Limits

- **Playback is not reproduction.** It shows what was recorded. It does not re-run the
  software, and a recording of a physical event would not be a deterministic reproduction
  of that event.
- **No root cause.** Packages say "Not determined". The timeline shows order and timing,
  which establishes correlation at most.
- A record made while the link is down holds the last values received, marked
  `connected=false`. It is not a measurement of the vehicle.
- One vehicle. Fleet sessions record the first vehicle only.
- Not in a package: camera video, detections, maps, raw MAVLink, application logs, resource
  use over time. Video and detections stay in the session folder; the package lists what it
  omits.
- Alerts shown during playback are re-derived from the replayed states by the current
  software, and are labelled `replayed`. The alerts raised at the time are the `alert`
  records on the timeline. The two can differ if detection logic has changed since.
- Playback controls need no token. They change what every viewer of that backend sees and
  touch no vehicle.
- No report generator yet; `incident.json` is the report.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/replay/session` | The running session's manifest |
| `GET /api/replay/timeline` | Position, speed and event markers (replay mode) |
| `POST /api/replay/control` | `{"action": "play" \| "pause" \| "seek" \| "speed", "value": n}` |
| `GET /api/incidents` | Incidents this session |
| `POST /api/incidents` | Operator marks an incident (operator token) |
| `GET /api/incidents/{id}` | Summary, incident document, verification result |
| `GET /api/incidents/{id}/export` | Zip of the package |
