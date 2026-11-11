import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useStore } from "../state/store";
import type { Mission, MissionPhase, Waypoint } from "../types";

const TOKEN_KEY = "aegis.operatorToken";
const BUSY_PHASES: MissionPhase[] = ["uploading", "active", "returning"];
const PHASE_TONE: Partial<Record<MissionPhase, string>> = {
  active: "text-track",
  returning: "text-track",
  completed: "text-ok",
  cancelled: "text-warn",
  stalled: "text-crit",
  failed: "text-crit",
  rejected: "text-crit",
};

function readToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function MissionPanel() {
  const system = useStore((s) => s.system);
  const scenario = useStore((s) => s.scenario);
  const draft = useStore((s) => s.draft);
  const setDraft = useStore((s) => s.setDraft);
  const status = useStore((s) => s.snapshot?.mission);
  const vehicleConnected = useStore((s) => Boolean(s.snapshot?.state?.connected));

  const [token, setToken] = useState(readToken);
  const [issues, setIssues] = useState<string[] | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Live validation of the draft against the backend's rules (debounced).
  useEffect(() => {
    if (!draft) return;
    setConfirming(false);
    const timer = window.setTimeout(() => {
      api
        .validate(draft)
        .then((result) => setIssues(result.issues))
        .catch((e: unknown) =>
          setIssues([e instanceof ApiError ? `Invalid mission: ${e.message}` : "Backend unreachable"]),
        );
    }, 300);
    return () => window.clearTimeout(timer);
  }, [draft]);

  if (!draft || !scenario) {
    return (
      <section className="border-b border-line p-3 text-[12px] text-dim">Loading scenario…</section>
    );
  }

  const phase = status?.phase ?? "idle";
  const busy = BUSY_PHASES.includes(phase);
  const commandsEnabled = Boolean(system?.commands_enabled);
  const attested = system?.simulator_attested !== false;
  const canCommand = commandsEnabled && attested && vehicleConnected && token.length > 0;
  const valid = issues !== null && issues.length === 0;
  // A disabled button must always say why; otherwise a click looks like "nothing happens".
  const startBlockedBy = !commandsEnabled
    ? null // explained by the read-only note below
    : !vehicleConnected
      ? "No vehicle connected. Start PX4 (scripts/run_px4.sh) and wait for the Vehicle lamp to turn green."
      : !attested
        ? null // explained by the "Commands locked" note below
        : token.length === 0
          ? "Enter the operator token above (the AEGIS_OPERATOR_TOKEN value from your .env file)."
          : busy
            ? "A mission is in progress. Use Hold, Return or Land to intervene."
            : !valid
              ? "The mission has validation problems (see above)."
              : null;

  const saveToken = (value: string) => {
    setToken(value);
    try {
      sessionStorage.setItem(TOKEN_KEY, value);
    } catch {
      /* storage unavailable: keep the token in memory only */
    }
  };

  const run = async (action: () => Promise<unknown>) => {
    setPending(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending(false);
      setConfirming(false);
    }
  };

  const start = () =>
    run(async () => {
      const result = await api.submit(draft, token);
      if (!result.accepted) {
        throw new Error([result.status.message, ...result.status.issues].join(" "));
      }
    });

  const updateWaypoint = (index: number, patch: Partial<Waypoint>) => {
    const waypoints = draft.waypoints.map((wp, i) => (i === index ? { ...wp, ...patch } : wp));
    setDraft({ ...draft, waypoints });
  };
  const addWaypoint = () => {
    const last = draft.waypoints[draft.waypoints.length - 1];
    const next: Waypoint = last
      ? { ...last, east_m: last.east_m + 10 }
      : { east_m: 0, north_m: 10, altitude_rel_m: 10 };
    setDraft({ ...draft, waypoints: [...draft.waypoints, next] });
  };
  const removeWaypoint = (index: number) =>
    setDraft({ ...draft, waypoints: draft.waypoints.filter((_, i) => i !== index) });
  const resetDraft = () => setDraft(structuredClone(scenario.demo_mission) as Mission);

  const reached = status?.mission_id === draft.mission_id ? status.current_waypoint : 0;
  const progress = status && status.total_waypoints > 0 ? status.current_waypoint / status.total_waypoints : 0;
  const fence = draft.constraints.geofence;

  return (
    <section className="border-b border-line p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="panel-title">Mission</h2>
        <span className={`num text-[11px] font-semibold uppercase ${PHASE_TONE[phase] ?? "text-dim"}`}>
          {phase}
          {status && status.total_waypoints > 0 && ` · WP ${status.current_waypoint}/${status.total_waypoints}`}
        </span>
      </div>

      <div className="mb-1 h-1 overflow-hidden rounded-sm bg-ink-700" aria-hidden>
        <div className="h-full bg-track transition-all" style={{ width: `${progress * 100}%` }} />
      </div>
      <p className="mb-2 min-h-4 text-[11px] text-dim">{status?.message || draft.name}</p>

      <table className="num w-full border-collapse text-[12px]">
        <thead>
          <tr className="panel-title text-left">
            <th className="w-9 py-1 font-normal">WP</th>
            <th className="font-normal">East m</th>
            <th className="font-normal">North m</th>
            <th className="font-normal">Alt m</th>
            <th className="w-6" />
          </tr>
        </thead>
        <tbody>
          {draft.waypoints.map((wp, i) => (
            <tr key={i} className="border-t border-line">
              <td className={i < reached ? "text-ok" : "text-route"}>{i + 1}</td>
              {(["east_m", "north_m", "altitude_rel_m"] as const).map((field) => (
                <td key={field} className="py-0.5 pr-1">
                  <input
                    type="number"
                    step={1}
                    value={wp[field]}
                    disabled={busy}
                    aria-label={`Waypoint ${i + 1} ${field}`}
                    onChange={(e) => {
                      const value = e.target.valueAsNumber;
                      if (!Number.isNaN(value)) updateWaypoint(i, { [field]: value });
                    }}
                    className="w-full rounded-sm border border-line bg-ink-800 px-1.5 py-0.5 text-right outline-none focus:border-track disabled:opacity-60"
                  />
                </td>
              ))}
              <td>
                <button
                  onClick={() => removeWaypoint(i)}
                  disabled={busy || draft.waypoints.length <= 1}
                  title="Remove waypoint"
                  aria-label={`Remove waypoint ${i + 1}`}
                  className="px-1 text-dim hover:text-crit disabled:opacity-30"
                >
                  ×
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-1.5 flex items-center gap-4 text-[11px]">
        <button onClick={addWaypoint} disabled={busy} className="text-track hover:underline disabled:opacity-40">
          + Add waypoint
        </button>
        <button onClick={resetDraft} disabled={busy} className="text-dim hover:text-text disabled:opacity-40">
          Reset to demo
        </button>
      </div>
      <div className="num mt-1 text-[10px] text-dim">
        Geofence ceiling {fence.max_altitude_rel_m} m · cruise {draft.constraints.cruise_speed_m_s} m/s ·
        return-to-home {draft.constraints.return_to_home_on_complete ? "on" : "off"}
      </div>

      <div
        className={`mt-2 rounded-sm border px-2.5 py-1.5 text-[11px] ${
          issues === null ? "border-line text-dim" : valid ? "border-ok/50 text-ok" : "border-crit/60 text-crit"
        }`}
        role="status"
      >
        {issues === null && "Validating…"}
        {valid && "Pre-flight validation passed: all waypoints and legs are inside the geofence."}
        {issues !== null && !valid && (
          <ul className="list-inside list-disc">
            {issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-3 border-t border-line pt-3">
        <div className="mb-2 flex items-center gap-2">
          <label htmlFor="operator-token" className="panel-title shrink-0">
            Operator token
          </label>
          <input
            id="operator-token"
            type="password"
            value={token}
            autoComplete="off"
            placeholder={commandsEnabled ? "AEGIS_OPERATOR_TOKEN" : "commands disabled"}
            disabled={!commandsEnabled}
            onChange={(e) => saveToken(e.target.value)}
            className="num w-full rounded-sm border border-line bg-ink-800 px-2 py-1 text-[12px] outline-none focus:border-track disabled:opacity-50"
          />
        </div>

        {!commandsEnabled && (
          <p className="mb-2 text-[11px] text-dim">
            Read-only: the backend is in {system?.mode ?? "…"} mode, so no commands can be sent.
          </p>
        )}
        {commandsEnabled && !attested && (
          <p className="mb-2 text-[11px] text-crit">
            Commands locked: {system?.attestation_error ?? "the autopilot is not an attested simulator."}
          </p>
        )}

        {confirming ? (
          <div className="mb-2 rounded-sm border border-sim/60 bg-sim/10 p-2 text-[12px]">
            <p className="mb-2">
              Arm the <strong>simulated</strong> vehicle and fly {draft.waypoints.length} waypoints?
            </p>
            <div className="flex gap-2">
              <button
                onClick={start}
                disabled={pending}
                className="rounded-sm bg-sim px-3 py-1 font-semibold text-ink-950 disabled:opacity-60"
              >
                {pending ? "Uploading…" : "Confirm: arm & start"}
              </button>
              <button onClick={() => setConfirming(false)} className="rounded-sm border border-line px-3 py-1 text-dim">
                Back
              </button>
            </div>
          </div>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            disabled={!canCommand || !valid || busy || pending}
            className="mb-2 w-full rounded-sm border border-track bg-track/15 py-1.5 text-[12px] font-semibold uppercase tracking-wider text-track hover:bg-track/25 disabled:cursor-not-allowed disabled:border-line disabled:bg-transparent disabled:text-dim"
          >
            Upload &amp; start mission
          </button>
        )}

        {startBlockedBy && !confirming && (
          <p className="mb-2 rounded-sm border border-warn/50 px-2 py-1 text-[11px] text-warn" role="status">
            {startBlockedBy}
          </p>
        )}

        <div className="grid grid-cols-3 gap-1.5">
          <Intervene label="Hold" hint="Cancel: stop and hover" disabled={!canCommand || pending} onClick={() => run(() => api.hold(token))} />
          <Intervene label="Return" hint="Return to home and land" disabled={!canCommand || pending} onClick={() => run(() => api.returnHome(token))} />
          <Intervene label="Land" hint="Land at the current position" disabled={!canCommand || pending} onClick={() => run(() => api.land(token))} />
        </div>
        {error && (
          <p className="mt-2 rounded-sm border border-crit/60 px-2 py-1 text-[11px] text-crit" role="alert">
            {error}
          </p>
        )}
      </div>
    </section>
  );
}

function Intervene(props: { label: string; hint: string; disabled: boolean; onClick: () => void }) {
  return (
    <button
      onClick={props.onClick}
      disabled={props.disabled}
      title={props.hint}
      className="rounded-sm border border-warn/70 py-1.5 text-[12px] font-semibold uppercase tracking-wider text-warn hover:bg-warn/15 disabled:cursor-not-allowed disabled:border-line disabled:text-dim"
    >
      {props.label}
    </button>
  );
}
