export function fixed(value: number | null | undefined, digits = 1, dash = "--"): string {
  return value == null || Number.isNaN(value) ? dash : value.toFixed(digits);
}

/** Seconds as H:MM:SS.s (tenths), for simulation / playback clocks. */
export function clock(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "--:--:--";
  const s = Math.max(0, seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = (s % 60).toFixed(1).padStart(4, "0");
  return `${h}:${String(m).padStart(2, "0")}:${rest}`;
}

export function wallClock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour12: false });
}
