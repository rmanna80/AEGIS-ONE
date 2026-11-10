import { wallClock } from "../lib/format";
import { useStore } from "../state/store";
import type { Severity } from "../types";

const TONE: Record<Severity, string> = {
  info: "border-line text-text",
  warning: "border-warn text-warn",
  critical: "border-crit text-crit",
};

export function AlertsPanel() {
  const alerts = useStore((s) => s.snapshot?.alerts) ?? [];
  const messages = useStore((s) => s.snapshot?.autopilot_messages) ?? [];

  return (
    <section className="flex min-h-0 flex-1 flex-col p-3">
      <h2 className="panel-title mb-2">
        Active alerts <span className="num">({alerts.length})</span>
      </h2>
      {alerts.length === 0 ? (
        <div className="rounded-sm border border-line px-2.5 py-1.5 text-[11px] text-dim">
          No active alerts from the AEGIS monitors.
        </div>
      ) : (
        <ul className="flex flex-col gap-1">
          {alerts.map((a) => (
            <li key={a.alert_id} className={`rounded-sm border-l-2 bg-ink-900 px-2.5 py-1.5 ${TONE[a.severity]}`}>
              <div className="flex justify-between text-[11px] font-semibold uppercase tracking-wider">
                <span>
                  {a.severity} · {a.code}
                </span>
                <span className="num font-normal text-dim">
                  {a.provenance} · {wallClock(a.raised_at_unix_s)}
                </span>
              </div>
              <div className="text-[12px] text-text">{a.message}</div>
            </li>
          ))}
        </ul>
      )}

      <h2 className="panel-title mb-1 mt-3">Autopilot messages (PX4)</h2>
      <div className="num min-h-16 flex-1 overflow-y-auto rounded-sm border border-line bg-ink-900 p-2 text-[11px] leading-relaxed">
        {messages.length === 0 && <span className="text-dim">No messages yet.</span>}
        {[...messages].reverse().map((m, i) => (
          <div key={`${m.received_at_unix_s}-${i}`} className="flex gap-2">
            <span className="shrink-0 text-dim">{wallClock(m.received_at_unix_s)}</span>
            <span
              className={
                m.level === "INFO" || m.level === "DEBUG" || m.level === "NOTICE"
                  ? "text-text"
                  : "text-warn"
              }
            >
              {m.text}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
