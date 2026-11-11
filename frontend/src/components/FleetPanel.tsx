import { useState } from "react";
import { api } from "../api";
import { useStore } from "../state/store";
import type { FleetSearchPlan, MissionStatus, VehicleState } from "../types";

const TOKEN_KEY = "aegis.operatorToken";

function operatorToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

interface Row {
  id: string;
  state: VehicleState | null;
  mission: MissionStatus;
}

/**
 * Every vehicle of the fleet with its own Hold / Return / Land, a Hold for all of
 * them, and (when search support is on) a shared search that gives each vehicle its
 * own strip. Vehicles fly only missions submitted here or in the Mission panel.
 */
export function FleetPanel() {
  const snapshot = useStore((s) => s.snapshot);
  const sarEnabled = useStore((s) => Boolean(s.sar?.enabled));
  const [plans, setPlans] = useState<FleetSearchPlan[] | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!snapshot?.fleet || snapshot.fleet.length === 0) return null;
  const rows: Row[] = [
    { id: snapshot.state?.vehicle_id ?? "sim-1", state: snapshot.state, mission: snapshot.mission },
    ...snapshot.fleet.map((v) => ({ id: v.vehicle_id, state: v.state, mission: v.mission })),
  ];

  // Closest pair of airborne vehicles, from the same positions the 3D view draws.
  let closest: { a: string; b: string; horizontal: number; vertical: number } | null = null;
  for (let i = 0; i < rows.length; i++) {
    for (let j = i + 1; j < rows.length; j++) {
      const a = rows[i]!.state;
      const b = rows[j]!.state;
      if (!a?.in_air || !b?.in_air || !a.position_enu_m || !b.position_enu_m) continue;
      const horizontal = Math.hypot(
        a.position_enu_m.x - b.position_enu_m.x,
        a.position_enu_m.y - b.position_enu_m.y,
      );
      const vertical = Math.abs(a.position_enu_m.z - b.position_enu_m.z);
      if (!closest || Math.hypot(horizontal, vertical) < Math.hypot(closest.horizontal, closest.vertical)) {
        closest = { a: rows[i]!.id, b: rows[j]!.id, horizontal, vertical };
      }
    }
  }

  const run = async (action: (token: string) => Promise<unknown>, done?: string) => {
    setError(null);
    setNote(null);
    const token = operatorToken();
    if (!token) {
      setError("Enter the operator token in the Mission panel first.");
      return;
    }
    try {
      await action(token);
      if (done) setNote(done);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setConfirming(false);
    }
  };

  const planSearch = async () => {
    setError(null);
    setNote(null);
    try {
      setPlans((await api.sarPlanFleet()).plans);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const startSearch = () =>
    run(async (token) => {
      for (const plan of plans ?? []) {
        const result = await api.fleetSubmit(plan.vehicle_id, plan.mission, token);
        if (!result.accepted) {
          throw new Error(
            `${plan.vehicle_id}: ${[result.status.message, ...result.status.issues].join(" ")}`,
          );
        }
      }
      setPlans(null);
    }, "Search started on every vehicle.");

  const plansOk = plans !== null && plans.every((p) => p.issues.length === 0);

  return (
    <section className="border-b border-line p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="panel-title">Fleet</h2>
        <span className="num text-[11px] text-dim">
          {closest
            ? `closest: ${closest.a}–${closest.b} ${closest.horizontal.toFixed(0)} m apart, ${closest.vertical.toFixed(0)} m in height`
            : `${rows.length} vehicles`}
        </span>
      </div>

      <ul className="mb-2 flex flex-col gap-1">
        {rows.map((row) => (
          <li key={row.id} className="rounded-sm border border-line bg-ink-900 px-2.5 py-1.5">
            <div className="flex justify-between text-[12px]">
              <span className="num font-semibold">{row.id}</span>
              <span className="num text-[11px] uppercase text-dim">
                {row.state?.connected ? (row.state.flight_mode ?? "—") : "not connected"} ·{" "}
                {row.mission.phase}
                {row.mission.total_waypoints > 0 &&
                  ` ${row.mission.current_waypoint}/${row.mission.total_waypoints}`}
              </span>
            </div>
            <div className="num text-[11px] text-dim">
              alt {row.state?.relative_altitude_m?.toFixed(0) ?? "—"} m · E{" "}
              {row.state?.position_enu_m?.x.toFixed(0) ?? "—"} · N{" "}
              {row.state?.position_enu_m?.y.toFixed(0) ?? "—"} · battery{" "}
              {row.state?.battery?.remaining_fraction != null
                ? `${(row.state.battery.remaining_fraction * 100).toFixed(0)}%`
                : "—"}
            </div>
            <div className="mt-1 flex gap-1">
              {(["hold", "return", "land"] as const).map((command) => (
                <button
                  key={command}
                  onClick={() => run((token) => api.fleetCommand(row.id, command, token), `${row.id}: ${command} sent.`)}
                  disabled={!row.state?.connected}
                  className="flex-1 rounded-sm border border-line px-2 py-0.5 text-[11px] uppercase hover:border-warn disabled:opacity-40"
                >
                  {command}
                </button>
              ))}
            </div>
          </li>
        ))}
      </ul>

      <button
        onClick={() => run((token) => api.fleetHoldAll(token), "Hold sent to every connected vehicle.")}
        className="mb-2 w-full rounded-sm border border-warn px-2 py-1 text-[12px] font-semibold uppercase text-warn hover:bg-ink-800"
      >
        Hold all
      </button>

      {sarEnabled && (
        <>
          <button
            onClick={planSearch}
            className="mb-2 w-full rounded-sm border border-line bg-ink-800 px-2 py-1 text-[12px] hover:border-track"
          >
            Plan a shared search
          </button>
          {plans && (
            <div className="mb-2 rounded-sm border border-line px-2.5 py-1.5 text-[11px]">
              {plans.map((plan) => (
                <div key={plan.vehicle_id} className={plan.issues.length > 0 ? "text-crit" : "text-dim"}>
                  <span className="num font-semibold text-text">{plan.vehicle_id}</span>:{" "}
                  {plan.summary.passes} passes at {plan.summary.altitude_rel_m} m,{" "}
                  {Math.round(plan.summary.path_length_m)} m
                  {plan.issues.length > 0 && ` — ${plan.issues.join(" ")}`}
                </div>
              ))}
              <button
                onClick={() => (confirming ? startSearch() : setConfirming(true))}
                disabled={!plansOk}
                className="mt-1.5 w-full rounded-sm border border-track px-2 py-1 text-[12px] font-semibold uppercase text-track disabled:opacity-40"
              >
                {confirming ? `Confirm: arm and launch ${plans.length} vehicles` : "Start the shared search"}
              </button>
            </div>
          )}
        </>
      )}
      {note && <p className="text-[11px] text-ok">{note}</p>}
      {error && <p className="text-[11px] text-crit">{error}</p>}
      <p className="mt-1 text-[11px] text-dim">
        The Mission panel below commands {rows[0]!.id}. AEGIS holds two vehicles that come too
        close; it never steers them.
      </p>
    </section>
  );
}
