import { useEffect, useState } from "react";
import { api } from "../api";
import type { IncidentSummary } from "../types";

const TOKEN_KEY = "aegis.operatorToken";

const STATUS_TONE: Record<IncidentSummary["status"], string> = {
  open: "text-crit",
  packaging: "text-warn",
  packaged: "text-ok",
  failed: "text-crit",
};

/**
 * Incidents preserved this session. One opens by itself on a critical alert; the
 * operator can also mark one. A package is evidence of what was recorded, not a
 * diagnosis.
 */
export function IncidentsPanel() {
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .incidents()
        .then((r) => {
          if (cancelled) return;
          setEnabled(r.enabled);
          setIncidents(r.incidents);
        })
        .catch(() => undefined);
    load();
    const timer = window.setInterval(load, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  if (!enabled) return null;

  const mark = () => {
    let token = "";
    try {
      token = sessionStorage.getItem(TOKEN_KEY) ?? "";
    } catch {
      /* no storage: the request will be refused and say why */
    }
    api
      .markIncident("Marked by the operator", "", token)
      .then((r) => setMessage(`${r.incident.incident_id} opened.`))
      .catch((e: Error) => setMessage(e.message));
  };

  return (
    <section className="shrink-0 border-t border-line p-3">
      <div className="flex items-center justify-between">
        <h2 className="panel-title">Incidents</h2>
        <button
          onClick={mark}
          title="Preserve the record around this moment (needs the operator token)"
          className="rounded-sm border border-line px-2 py-0.5 text-[11px] text-text hover:bg-ink-800"
        >
          Mark incident
        </button>
      </div>
      {message && <p className="mt-1 text-[11px] text-dim">{message}</p>}
      {incidents.length === 0 ? (
        <p className="mt-2 text-[11px] text-dim">
          None this session. One is preserved automatically on a critical alert.
        </p>
      ) : (
        <ul className="mt-2 flex flex-col gap-1 text-[11px]">
          {[...incidents].reverse().map((incident) => (
            <li
              key={incident.incident_id}
              className="rounded-sm border border-line bg-ink-900 px-2.5 py-1.5"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="num text-text">{incident.incident_id}</span>
                <span className={`num uppercase ${STATUS_TONE[incident.status]}`}>
                  {incident.status}
                </span>
              </div>
              <p className="text-dim">
                {incident.title}
                {incident.seconds != null && ` · ${incident.seconds.toFixed(0)} s preserved`}
              </p>
              {incident.status === "packaged" && (
                <p className="num mt-0.5 text-dim">
                  sha256 {incident.manifest_sha256?.slice(0, 12)}… ·{" "}
                  {incident.signed_by ? (
                    <span title={`Signed with this device's key ${incident.signed_by}`}>
                      signed {incident.signed_by.slice(0, 9)}… ·{" "}
                    </span>
                  ) : (
                    <span className="text-warn">unsigned · </span>
                  )}
                  <a
                    className="text-track underline"
                    href={`/api/incidents/${encodeURIComponent(incident.incident_id)}/export`}
                  >
                    export
                  </a>
                </p>
              )}
              {incident.status === "failed" && <p className="text-crit">{incident.error}</p>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
