import { useEffect, useState } from "react";
import { clock } from "../lib/format";
import { useStore } from "../state/store";
import type { Provenance } from "../types";

const PROVENANCE_STYLE: Record<Provenance, string> = {
  simulated: "border-sim text-sim bg-sim/10",
  replayed: "border-replay text-replay bg-replay/10",
  physical: "border-crit text-crit bg-crit/10",
};

export function Header() {
  const system = useStore((s) => s.system);
  const link = useStore((s) => s.link);
  const state = useStore((s) => s.snapshot?.state);
  const playback = useStore((s) => s.snapshot?.playback);
  const lastMessageAt = useStore((s) => s.lastMessageAt);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 250);
    return () => window.clearInterval(timer);
  }, []);

  const provenance = state?.provenance ?? system?.provenance;
  const source = state?.source ?? system?.source;
  const dataAge = lastMessageAt ? performance.now() / 1000 - lastMessageAt : null;
  const streamLive = link === "open" && dataAge !== null && dataAge < 1.5;
  const vehicleLive = streamLive && Boolean(state?.connected);

  return (
    <header className="flex h-12 shrink-0 items-center gap-4 border-b border-line bg-ink-900 px-4">
      <div className="flex items-baseline gap-2">
        <span className="text-[15px] font-semibold tracking-[0.18em]">AEGIS ONE</span>
        <span className="panel-title">Mission Control</span>
      </div>

      {state?.injected_faults && state.injected_faults.length > 0 && (
        <span
          className="num rounded-sm border border-crit px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-crit"
          title="AEGIS's copy of the telemetry is being altered on purpose (fault injection)."
        >
          Fault injected: {state.injected_faults.join(", ")}
        </span>
      )}
      {provenance && (
        <div
          className={`num rounded-sm border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider ${PROVENANCE_STYLE[provenance]}`}
          title="Where the data on this screen comes from"
        >
          {provenance}
          {state?.recorded_provenance ? ` (of ${state.recorded_provenance} flight)` : ""}
          <span className="ml-2 font-normal normal-case tracking-normal opacity-80">{source}</span>
        </div>
      )}
      {system && (
        <div className="num rounded-sm border border-line px-2 py-0.5 text-[11px] uppercase tracking-wider text-dim">
          mode: {system.mode}
          {!system.commands_enabled && " · read-only"}
        </div>
      )}

      <div className="ml-auto flex items-center gap-5">
        <Clock
          label="Sim time"
          value={clock(state?.vehicle_time_s)}
          title="Autopilot clock (simulation time under SITL lockstep)"
        />
        {state?.sim_real_time_factor != null && (
          <div
            className="text-right leading-tight"
            title="Simulation speed relative to real time, measured from the autopilot clock. Below 1.0 the simulator cannot keep up and everything takes longer."
          >
            <div className="panel-title">Sim speed</div>
            <div className={`num text-[13px] ${state.sim_real_time_factor < 0.5 ? "text-warn" : ""}`}>
              {state.sim_real_time_factor.toFixed(2)}x
            </div>
          </div>
        )}
        {playback && (
          <Clock
            label={`Playback ${playback.speed}x`}
            value={`${clock(playback.time_s)} / ${clock(playback.duration_s)}`}
            title="Position in the recording being replayed"
          />
        )}
        <Clock
          label="Real time"
          value={now.toLocaleTimeString([], { hour12: false })}
          title="This computer's wall clock"
        />
        <div className="flex items-center gap-3">
          <Lamp on={streamLive} label="Backend" />
          <Lamp on={vehicleLive} label="Vehicle" />
        </div>
      </div>
    </header>
  );
}

function Clock({ label, value, title }: { label: string; value: string; title: string }) {
  return (
    <div className="text-right leading-tight" title={title}>
      <div className="panel-title">{label}</div>
      <div className="num text-[13px]">{value}</div>
    </div>
  );
}

function Lamp({ on, label }: { on: boolean; label: string }) {
  return (
    <span className="flex items-center gap-1.5 text-[11px] text-dim">
      <span
        className={`inline-block h-2 w-2 rounded-full ${on ? "bg-ok" : "bg-crit"}`}
        aria-hidden
      />
      {label} {on ? "link" : "down"}
    </span>
  );
}
