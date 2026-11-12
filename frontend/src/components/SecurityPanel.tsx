import { useEffect, useState } from "react";
import { api } from "../api";
import type { SecurityStatus } from "../types";

const LEVEL_TONE: Record<SecurityStatus["posture"][number]["level"], string> = {
  ok: "text-ok",
  info: "text-dim",
  warning: "text-warn",
};

/**
 * Read-only view of the security posture, the anomaly detectors and the command
 * audit log. Alerts themselves appear in the Active alerts list above.
 */
export function SecurityPanel() {
  const [status, setStatus] = useState<SecurityStatus | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .security()
        .then((s) => !cancelled && setStatus(s))
        .catch(() => undefined);
    load();
    const timer = window.setInterval(load, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  if (!status) return null;
  const warnings = status.posture.filter((c) => c.level === "warning").length;
  const anomaly = status.anomaly;
  const detections = anomaly.enabled
    ? Object.entries(anomaly.detections).filter(([, count]) => count > 0)
    : [];

  return (
    <section className="shrink-0 border-t border-line p-3">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center justify-between text-left"
      >
        <h2 className="panel-title">Security</h2>
        <span className="num text-[11px] text-dim">
          {warnings} posture warning{warnings === 1 ? "" : "s"} · {status.auth_failures_total} failed
          logins · {status.audit.records} audited {open ? "▾" : "▸"}
        </span>
      </button>

      {status.faults.active.length > 0 && (
        <p className="mt-2 rounded-sm border border-crit px-2.5 py-1.5 text-[11px] text-crit">
          Fault injection active ({status.faults.active.map((f) => f.kind).join(", ")}): the
          telemetry shown is deliberately altered for testing.
        </p>
      )}

      {open && (
        <div className="mt-2 flex flex-col gap-2 text-[11px]">
          <ul className="flex flex-col gap-1">
            {status.posture.map((check) => (
              <li key={check.id} className="rounded-sm border border-line bg-ink-900 px-2.5 py-1.5">
                <span className={`num mr-1.5 font-semibold uppercase ${LEVEL_TONE[check.level]}`}>
                  {check.level}
                </span>
                <span className="text-text">{check.text}</span>
              </li>
            ))}
          </ul>

          {status.device && (
            <div>
              <h3 className="panel-title mb-1">This computer</h3>
              <p className="num text-dim">
                {status.device.model ?? status.device.machine} · {status.device.cpu_count ?? "?"} cores ·{" "}
                {status.device.temperature_c != null
                  ? `${status.device.temperature_c.toFixed(0)} °C`
                  : "temperature n/a"}{" "}
                · load {status.device.load_1min ?? "?"} ·{" "}
                {status.device.disk_free_mb != null
                  ? `${(status.device.disk_free_mb / 1024).toFixed(0)} GB free`
                  : "disk n/a"}
                {status.device.throttled && status.device.throttled.length > 0 && (
                  <span className="text-warn"> · {status.device.throttled.join(", ")}</span>
                )}
              </p>
            </div>
          )}

          <div>
            <h3 className="panel-title mb-1">Anomaly detectors</h3>
            {anomaly.enabled ? (
              <p className="text-dim">
                {Object.keys(anomaly.detectors).length} checks running, advisory only.{" "}
                {detections.length === 0
                  ? "Nothing detected this session."
                  : `Detected this session: ${detections.map(([code, n]) => `${code} ×${n}`).join(", ")}.`}
              </p>
            ) : (
              <p className="text-dim">Off (AEGIS_ANOMALY=false).</p>
            )}
          </div>

          <div>
            <h3 className="panel-title mb-1">Recent commands (audit log)</h3>
            {status.audit.recent.length === 0 ? (
              <p className="text-dim">No command requests yet.</p>
            ) : (
              <ul className="num text-dim">
                {status.audit.recent.slice(-8).map((r) => (
                  <li key={r.seq}>
                    #{r.seq} {r.event} {r.path ?? ""} {r.result ?? ""}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-1 text-dim">
              Check the file for edits with <span className="num">scripts/verify_audit.py</span>.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
