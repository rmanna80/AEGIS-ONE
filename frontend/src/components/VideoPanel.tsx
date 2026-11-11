import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { fixed } from "../lib/format";
import { useStore } from "../state/store";
import type { PerceptionResult, Provenance, Track } from "../types";

const TRACK_COLOURS = ["#4cc9f0", "#3ecf8e", "#f472b6", "#f5a524", "#a78bfa", "#fb7185", "#22d3ee", "#a3e635"];
const SELECTED = "#ffffff";
const PROVENANCE_TONE: Record<Provenance, string> = {
  simulated: "border-sim text-sim",
  replayed: "border-replay text-replay",
  physical: "border-crit text-crit",
};

function colourFor(trackId: number): string {
  return TRACK_COLOURS[trackId % TRACK_COLOURS.length]!;
}

/** Draw one frame and its overlays. Boxes arrive in source pixels; `scale` maps to the canvas. */
function draw(canvas: HTMLCanvasElement, image: ImageBitmap, result: PerceptionResult): void {
  if (canvas.width !== image.width || canvas.height !== image.height) {
    canvas.width = image.width;
    canvas.height = image.height;
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const scale = image.width / result.width_px;
  ctx.drawImage(image, 0, 0);
  // Keep labels readable when the canvas is displayed smaller than its pixel size.
  const zoom = canvas.clientWidth > 0 ? canvas.width / canvas.clientWidth : 1;
  const fontPx = Math.round(11 * Math.max(1, zoom));
  const labelHeight = fontPx + 5;
  ctx.font = `600 ${fontPx}px ui-monospace, Menlo, Consolas, monospace`;
  ctx.textBaseline = "top";

  // Raw detections that are not (yet) part of a confirmed track: thin and neutral.
  ctx.setLineDash([]);
  ctx.lineWidth = zoom;
  ctx.strokeStyle = "rgba(255,255,255,0.45)";
  for (const d of result.detections) {
    const [x1, y1, x2, y2] = d.bbox_xyxy_px;
    ctx.strokeRect(x1 * scale, y1 * scale, (x2 - x1) * scale, (y2 - y1) * scale);
  }

  for (const t of result.tracks) {
    const selected = t.track_id === result.selected_track_id;
    const colour = selected ? SELECTED : colourFor(t.track_id);
    const [x1, y1, x2, y2] = t.bbox_xyxy_px;

    if (t.history_px.length > 1) {
      ctx.beginPath();
      t.history_px.forEach(([hx, hy], i) =>
        i === 0 ? ctx.moveTo(hx * scale, hy * scale) : ctx.lineTo(hx * scale, hy * scale),
      );
      ctx.strokeStyle = colour;
      ctx.globalAlpha = 0.7;
      ctx.lineWidth = 1.5 * zoom;
      ctx.setLineDash([]);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Coasting = position is predicted, not observed (e.g. occluded): dashed box.
    ctx.setLineDash(t.state === "coasting" ? [6 * zoom, 4 * zoom] : []);
    ctx.lineWidth = (selected ? 3 : 2) * zoom;
    ctx.strokeStyle = colour;
    ctx.strokeRect(x1 * scale, y1 * scale, (x2 - x1) * scale, (y2 - y1) * scale);

    const text =
      t.state === "coasting"
        ? `#${t.track_id} ${t.label} · predicted`
        : `#${t.track_id} ${t.label} ${(t.confidence * 100).toFixed(0)}%`;
    const width = ctx.measureText(text).width + 8;
    const labelY = Math.max(0, y1 * scale - labelHeight);
    ctx.setLineDash([]);
    ctx.fillStyle = colour;
    ctx.fillRect(x1 * scale - 1, labelY, width, labelHeight);
    ctx.fillStyle = "#080b10";
    ctx.fillText(text, x1 * scale + 3, labelY + 3);
  }
}

export function VideoPanel() {
  const status = useStore((s) => s.perception);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const latest = useRef<PerceptionResult | null>(null);
  const [result, setResult] = useState<PerceptionResult | null>(null);
  const [streaming, setStreaming] = useState(false);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;
    let pending: PerceptionResult | null = null;
    let lastUi = 0;

    const connect = () => {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${location.host}/ws/video`);
      socket.binaryType = "blob";
      socket.onopen = () => setStreaming(true);
      socket.onmessage = async (event) => {
        if (typeof event.data === "string") {
          pending = JSON.parse(event.data) as PerceptionResult;
          return;
        }
        const meta = pending;
        pending = null;
        if (!meta || !canvasRef.current) return;
        try {
          const image = await createImageBitmap(event.data as Blob);
          draw(canvasRef.current, image, meta);
          image.close();
        } catch (error) {
          console.error("Could not draw video frame", error);
          return;
        }
        latest.current = meta;
        const now = performance.now();
        if (now - lastUi > 200) {
          lastUi = now; // the track table does not need to re-render at video rate
          setResult(meta);
        }
      };
      socket.onclose = () => {
        setStreaming(false);
        if (!closed) timer = window.setTimeout(connect, 1500);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  const select = (trackId: number | null) => {
    api.selectTrack(trackId).catch(() => undefined); // the track may have just ended
  };

  const onCanvasClick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    const meta = latest.current;
    if (!canvas || !meta) return;
    const rect = canvas.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * meta.width_px;
    const y = ((event.clientY - rect.top) / rect.height) * meta.height_px;
    const hits = meta.tracks
      .filter(({ bbox_xyxy_px: [x1, y1, x2, y2] }) => x >= x1 && x <= x2 && y >= y1 && y <= y2)
      .sort((a, b) => area(a) - area(b)); // smallest box under the cursor wins
    const hit = hits[0];
    select(hit && hit.track_id !== meta.selected_track_id ? hit.track_id : null);
  };

  if (!status?.enabled) return null;
  const provenance = result?.provenance ?? status.provenance;
  const noFrames = status.faults.includes("camera.no_frames");

  return (
    <div className="flex h-full flex-col bg-ink-950">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2">
        <h2 className="panel-title">Camera · AI detection</h2>
        {provenance && (
          <span className={`num rounded-sm border px-1.5 text-[10px] font-semibold uppercase ${PROVENANCE_TONE[provenance]}`}>
            {provenance}
          </span>
        )}
        <span className="num truncate text-[10px] text-dim">{result?.source ?? status.camera_source}</span>
        {status.recording && (
          <span className="num ml-auto flex shrink-0 items-center gap-1 text-[10px] text-crit" title={`Detections are being recorded to ${status.recording}`}>
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-crit" /> REC detections
          </span>
        )}
      </div>

      <div className="relative flex min-h-0 flex-1 items-center justify-center bg-black">
        <canvas
          ref={canvasRef}
          onClick={onCanvasClick}
          className="max-h-full max-w-full cursor-crosshair"
          aria-label="Camera view with detection and tracking overlays. Click an object to select its track."
        />
        {(noFrames || !streaming) && (
          <div className="absolute inset-0 flex items-center justify-center bg-ink-950/80 p-6 text-center">
            <div>
              <div className="panel-title mb-1">No camera frames</div>
              <div className="text-[12px] text-dim">
                Waiting for the camera source ({status.camera_source ?? "not connected"}).
                {status.last_error && <div className="mt-1 text-crit">{status.last_error}</div>}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="num grid grid-cols-4 gap-px border-y border-line bg-line text-[11px]">
        <Stat label="Pipeline" value={fixed(status.fps, 1)} unit="fps" />
        <Stat label="Inference p50" value={fixed(status.inference_latency_ms_p50, 0)} unit="ms" />
        <Stat label="Inference p95" value={fixed(status.inference_latency_ms_p95, 0)} unit="ms" warn={status.faults.includes("inference.slow")} />
        <Stat label="Frame" value={result ? `${result.width_px}×${result.height_px}` : "--"} unit="" />
      </div>

      <div className="h-32 shrink-0 overflow-y-auto px-3 py-2">
        <div className="mb-1 flex items-center justify-between">
          <h3 className="panel-title">
            Tracks <span className="num">({result?.tracks.length ?? 0})</span> · {status.detector}
          </h3>
          <span className="text-[10px] text-dim">Click an object to highlight it. Display only.</span>
        </div>
        {result && result.tracks.length > 0 ? (
          <table className="num w-full text-[11px]">
            <tbody>
              {result.tracks.map((t) => {
                const selected = t.track_id === result.selected_track_id;
                return (
                  <tr
                    key={t.track_id}
                    onClick={() => select(selected ? null : t.track_id)}
                    className={`cursor-pointer border-t border-line ${selected ? "bg-ink-700" : "hover:bg-ink-800"}`}
                  >
                    <td className="w-10 py-0.5 font-semibold" style={{ color: colourFor(t.track_id) }}>
                      #{t.track_id}
                    </td>
                    <td>{t.label}</td>
                    <td className="text-right">{t.state === "coasting" ? "predicted" : `${(t.confidence * 100).toFixed(0)}%`}</td>
                    <td className="text-right text-dim" title="Image-space motion of the box centre. Not a ground speed.">
                      {Math.hypot(...t.velocity_px_s).toFixed(0)} px/s
                    </td>
                    <td className="w-16 text-right text-dim">{t.age_frames} fr</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="text-[11px] text-dim">No confirmed tracks in view.</div>
        )}
      </div>
    </div>
  );
}

function area(t: Track): number {
  const [x1, y1, x2, y2] = t.bbox_xyxy_px;
  return (x2 - x1) * (y2 - y1);
}

function Stat(props: { label: string; value: string; unit: string; warn?: boolean }) {
  return (
    <div className="bg-ink-900 px-2.5 py-1.5">
      <div className="panel-title">{props.label}</div>
      <div className={props.warn ? "text-warn" : ""}>
        {props.value} <span className="text-dim">{props.unit}</span>
      </div>
    </div>
  );
}
