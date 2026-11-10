# Sample data

`demo1_survey_sih.jsonl` is a real recording of Demo 1, not synthetic data: PX4 v1.17.0 SITL
with the SIH quadcopter simulator flying `simulation/scenarios/open_field.json`, captured by
`aegis.recording.FlightRecorder` on 2026-11-10 and downsampled from 10 Hz to 5 Hz.
Provenance in the file is `simulated`; when replayed it is shown as `replayed`.

Play it with `AEGIS_MODE=replay bash scripts/run_backend.sh`.
