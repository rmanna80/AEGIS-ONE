import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { ReplayTimeline, TimelineEvent } from "../types";

const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];

const MARK_TONE: Record<TimelineEvent["kind"], string> = {
  alert: "bg-crit",
  fault: "bg-warn",
  link: "bg-crit",
  gap: "bg-dim",
  mission: "bg-track",
  search: "bg-ok",
};

const RANK: TimelineEvent["kind"][] = ["alert", "link", "fault", "gap", "search", "mission"];

function worst(events: TimelineEvent[]): TimelineEvent["kind"] {
  return RANK.find((kind) => events.some((e) => e.kind === kind)) ?? "mission";
}

function mmss(seconds: number): string {
  const s = Math.max(0, seconds);
  return `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}

/**
 * Playback controls and event timeline for replay mode. Everything on the page
 * follows the one playback clock this bar moves; nothing here reaches a vehicle.
 */
export function ReplayBar() {
  const [timeline, setTimeline] = useState<ReplayTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragValue, setDragValue] = useState<number | null>(null);

  const load = useCallback(
    () =>
      api
        .replayTimeline()
        .then((t) => {
          setTimeline(t);
          setError(null);
        })
        .catch((e: Error) => setError(e.message)),
    [],
  );

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 500);
    return () => window.clearInterval(timer);
  }, [load]);

  if (!timeline) {
    return error ? <p className="px-3 py-2 text-[11px] text-dim">{error}</p> : null;
  }

  const { start_s: start, duration_s: end } = timeline;
  const span = Math.max(0.001, end - start);
  const position = dragValue ?? timeline.position_s;
  const control = (action: "play" | "pause" | "seek" | "speed", value?: number) =>
    api
      .replayControl(action, value)
      .then(load)
      .catch((e: Error) => setError(e.message));
  const clusters: { t: number; events: TimelineEvent[] }[] = [];
  for (const event of timeline.events) {
    const last = clusters[clusters.length - 1];
    if (last && event.t - last.t < span / 120) last.events.push(event);
    else clusters.push({ t: event.t, events: [event] });
  }
  // The event in force at the playback position: the last one at or before it.
  const current = [...timeline.events].reverse().find((e) => e.t <= position);

  return (
    <section
      className="shrink-0 border-t border-line bg-ink-900 px-3 py-2"
      aria-label="Replay controls"
    >
      <div className="flex items-center gap-3">
        <button
          onClick={() => control(timeline.paused ? "play" : "pause")}
          className="num w-16 rounded-sm border border-replay px-2 py-1 text-[11px] font-semibold uppercase text-replay hover:bg-replay/10"
        >
          {timeline.paused ? "Play" : "Pause"}
        </button>
        <span className="num w-28 text-[11px] text-text">
          {mmss(position - start)} / {mmss(span)}
        </span>
        <div className="relative flex-1">
          <input
            type="range"
            aria-label="Position in the recording"
            min={start}
            max={end}
            step={0.05}
            value={position}
            onChange={(e) => setDragValue(Number(e.target.value))}
            onPointerUp={() => {
              if (dragValue !== null) control("seek", dragValue);
              setDragValue(null);
            }}
            onKeyUp={() => {
              if (dragValue !== null) control("seek", dragValue);
              setDragValue(null);
            }}
            className="w-full accent-[var(--color-replay)]"
          />
          {/* Event markers. Events too close to tell apart share one marker. */}
          <div className="relative h-3">
            {clusters.map((cluster) => (
              <button
                key={cluster.t}
                onClick={() => control("seek", Math.max(start, cluster.t - 0.5))}
                title={cluster.events.map((e) => `${mmss(e.t - start)}  ${e.label}`).join("\n")}
                aria-label={`Jump to ${cluster.events.map((e) => e.label).join("; ")}`}
                style={{ left: `${(100 * (cluster.t - start)) / span}%` }}
                className={`absolute top-0 h-3 -translate-x-1/2 rounded-sm ${
                  cluster.events.length > 1 ? "w-[5px]" : "w-[3px]"
                } ${MARK_TONE[worst(cluster.events)]}`}
              />
            ))}
          </div>
        </div>
        <label className="flex items-center gap-1.5 text-[11px] text-dim">
          Speed
          <select
            value={timeline.speed}
            onChange={(e) => control("speed", Number(e.target.value))}
            className="num rounded-sm border border-line bg-ink-800 px-1 py-0.5 text-text"
          >
            {SPEEDS.map((s) => (
              <option key={s} value={s}>
                {s}×
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="mt-1 flex items-baseline justify-between gap-4 text-[11px]">
        <span className="text-text">
          {timeline.incident_id && <span className="num mr-2 text-replay">{timeline.incident_id}</span>}
          {current ? (
            <>
              <span className="num text-dim">{mmss(current.t - start)}</span> {current.label}
            </>
          ) : (
            <span className="text-dim">No event yet at this position.</span>
          )}
        </span>
        <span className="text-dim">
          {timeline.note}
          {timeline.recorded_software?.git_commit &&
            ` Recorded by commit ${timeline.recorded_software.git_commit.slice(0, 7)}.`}
        </span>
      </div>
      <ol className="mt-1 flex max-h-16 flex-wrap gap-x-4 gap-y-0.5 overflow-y-auto text-[11px]">
        {timeline.events.map((event, i) => (
          <li key={`${event.t}-${i}`}>
            <button
              onClick={() => control("seek", Math.max(start, event.t - 0.5))}
              className={`text-left hover:underline ${event === current ? "text-text" : "text-dim"}`}
            >
              <span
                className={`mr-1 inline-block h-2 w-2 rounded-sm align-middle ${MARK_TONE[event.kind]}`}
              />
              <span className="num">{mmss(event.t - start)}</span> {event.label}
            </button>
          </li>
        ))}
      </ol>
      {error && <p className="mt-1 text-[11px] text-crit">{error}</p>}
    </section>
  );
}
