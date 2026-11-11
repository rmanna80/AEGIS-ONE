import { useEffect, useState } from "react";
import { api } from "../api";
import { useStore } from "../state/store";
import type { ReviewStatus, SarCandidate } from "../types";

const TOKEN_KEY = "aegis.operatorToken";
const STATUS_TONE: Record<ReviewStatus, string> = {
  unreviewed: "text-warn",
  confirmed: "text-ok",
  dismissed: "text-dim",
};

function operatorToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

/**
 * Search support: thermal view, coverage, and the list of heat sources awaiting the
 * operator's decision. Everything here is advisory. Confirming or dismissing a
 * candidate records a decision; it never sends a command to the vehicle.
 */
export function SearchPanel() {
  const sar = useStore((s) => s.sar);
  const candidates = useStore((s) => s.sarCandidates);
  const setSar = useStore((s) => s.setSar);
  const setDraft = useStore((s) => s.setDraft);
  const [tick, setTick] = useState(0);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);

  if (!sar?.enabled) return null;

  const plan = async () => {
    setError(null);
    try {
      const result = await api.sarPlan();
      setDraft(result.mission);
      const s = result.summary;
      setNote(
        `Search pattern loaded into the mission planner: ${s.passes} passes at ${s.altitude_rel_m} m, ` +
          `${Math.round(s.path_length_m)} m, about ${Math.round(s.estimated_search_time_s / 60)} min. ` +
          (result.issues.length > 0
            ? `Problems: ${result.issues.join(" ")}`
            : "Review it above, then start it."),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const review = async (candidate: SarCandidate, decision: ReviewStatus) => {
    setError(null);
    const token = operatorToken();
    if (!token) {
      setError("Enter the operator token in the Mission panel to record a decision.");
      return;
    }
    try {
      const result = await api.sarReview(candidate.candidate_id, decision, "", token);
      setSar(
        sar,
        candidates.map((c) => (c.candidate_id === candidate.candidate_id ? result.candidate : c)),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section className="border-b border-line p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="panel-title">Search</h2>
        <span className="num text-[11px] uppercase text-dim">
          {sar.thermal_provenance ?? "no data"} · {sar.thermal_source ?? "thermal"}
        </span>
      </div>

      {sar.frames_processed > 0 ? (
        <img
          src={`/api/sar/thermal.jpg?t=${tick}`}
          alt="Latest thermal frame in false colour; detected warm objects are outlined"
          className="mb-2 w-full rounded-sm border border-line"
        />
      ) : (
        <div className="mb-2 rounded-sm border border-line px-2.5 py-6 text-center text-[11px] text-dim">
          Waiting for thermal frames (the vehicle must be connected).
        </div>
      )}

      <div className="mb-1 flex justify-between text-[11px] text-dim">
        <span>Search area seen by the thermal camera</span>
        <span className="num text-text">{(sar.coverage_fraction * 100).toFixed(0)}%</span>
      </div>
      <div className="mb-2 h-1 overflow-hidden rounded-sm bg-ink-700" aria-hidden>
        <div className="h-full bg-ok" style={{ width: `${sar.coverage_fraction * 100}%` }} />
      </div>

      <button
        onClick={plan}
        className="mb-2 w-full rounded-sm border border-line bg-ink-800 px-2 py-1 text-[12px] hover:border-track"
      >
        Plan search pattern
      </button>
      {note && <p className="mb-2 text-[11px] text-dim">{note}</p>}

      <h3 className="panel-title mb-1">
        Heat sources <span className="num">({candidates.length})</span>
        {sar.awaiting_review > 0 && (
          <span className="ml-2 normal-case text-warn">{sar.awaiting_review} need your review</span>
        )}
      </h3>
      {candidates.length === 0 ? (
        <p className="text-[11px] text-dim">None found yet.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {candidates.map((c) => (
            <li key={c.candidate_id} className="rounded-sm border border-line bg-ink-900 px-2.5 py-1.5">
              <div className="flex justify-between text-[12px]">
                <span>
                  <span className="num font-semibold">{c.candidate_id}</span> · {c.category}
                </span>
                <span className={`num text-[11px] uppercase ${STATUS_TONE[c.status]}`}>{c.status}</span>
              </div>
              <div className="num text-[11px] text-dim">
                E {c.east_m.toFixed(0)} m · N {c.north_m.toFixed(0)} m · peak {c.peak_c.toFixed(0)} °C ·{" "}
                {c.area_m2.toFixed(1)} m² · seen {c.observations}×
              </div>
              <div className="mt-1 flex gap-1">
                <button
                  onClick={() => review(c, "confirmed")}
                  disabled={c.status === "confirmed"}
                  className="flex-1 rounded-sm border border-line px-2 py-0.5 text-[11px] hover:border-ok disabled:opacity-40"
                >
                  Confirm
                </button>
                <button
                  onClick={() => review(c, "dismissed")}
                  disabled={c.status === "dismissed"}
                  className="flex-1 rounded-sm border border-line px-2 py-0.5 text-[11px] hover:border-warn disabled:opacity-40"
                >
                  Dismiss
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="mt-2 text-[11px] text-crit">{error}</p>}
      <p className="mt-2 text-[11px] text-dim">
        Advisory only. Descriptions give size and temperature, not identity. Your decision is
        recorded in the search report; it does not move the vehicle.
      </p>
    </section>
  );
}
