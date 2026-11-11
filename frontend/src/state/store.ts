import { create } from "zustand";
import type {
  Mission,
  NavigationStatus,
  PerceptionStatus,
  SarCandidate,
  SarStatus,
  Scenario,
  Snapshot,
  SystemInfo,
} from "../types";

export type LinkStatus = "connecting" | "open" | "closed";
export type CameraMode = "follow" | "orbit" | "fpv" | "top" | "free";

export interface HistoryPoint {
  t: number; // seconds since the dashboard started receiving data
  altitude: number | null;
  groundSpeed: number | null;
  verticalSpeed: number | null;
  battery: number | null; // percent
}

const HISTORY_INTERVAL_S = 0.5;
const HISTORY_MAX = 720; // 6 minutes at 2 Hz
const TRACK_MAX = 4000;
const TRACK_MIN_STEP_M = 0.15;

interface Store {
  link: LinkStatus;
  snapshot: Snapshot | null;
  system: SystemInfo | null;
  scenario: Scenario | null;
  perception: PerceptionStatus | null;
  navigation: NavigationStatus | null;
  sar: SarStatus | null;
  sarCandidates: SarCandidate[];
  /** Occupied voxel centres of the live map, flat [x, y, z, ...] in three.js coordinates. */
  mapVoxels: Float32Array;
  mapResolution: number;
  mapRevision: number;
  showMap: boolean;
  /** Mission being edited/previewed in the planner (starts as the scenario demo). */
  draft: Mission | null;
  history: HistoryPoint[];
  /** Flown path as flat [x, y, z, ...] in three.js coordinates. */
  track: number[];
  trackVersion: number;
  cameraMode: CameraMode;
  lastMessageAt: number;
  t0: number | null;

  setLink: (link: LinkStatus) => void;
  ingest: (snapshot: Snapshot) => void;
  setSystem: (system: SystemInfo) => void;
  setScenario: (scenario: Scenario) => void;
  setPerception: (perception: PerceptionStatus) => void;
  setNavigation: (navigation: NavigationStatus) => void;
  setSar: (sar: SarStatus, candidates: SarCandidate[]) => void;
  setMap: (voxelsEnu: number[], resolution: number, revision: number) => void;
  toggleMap: () => void;
  setDraft: (draft: Mission) => void;
  setCameraMode: (mode: CameraMode) => void;
  clearTrack: () => void;
}

export const useStore = create<Store>((set, get) => ({
  link: "connecting",
  snapshot: null,
  system: null,
  scenario: null,
  perception: null,
  navigation: null,
  sar: null,
  sarCandidates: [],
  mapVoxels: new Float32Array(0),
  mapResolution: 0.5,
  mapRevision: -1,
  showMap: true,
  draft: null,
  history: [],
  track: [],
  trackVersion: 0,
  cameraMode: "follow",
  lastMessageAt: 0,
  t0: null,

  setLink: (link) => set({ link }),
  setSystem: (system) => set({ system }),
  setPerception: (perception) => set({ perception }),
  setNavigation: (navigation) => set({ navigation }),
  setSar: (sar, sarCandidates) => set({ sar, sarCandidates }),
  toggleMap: () => set((s) => ({ showMap: !s.showMap })),
  setMap: (voxelsEnu, mapResolution, mapRevision) => {
    // ENU (east, north, up) -> three.js (x = east, y = up, z = -north)
    const mapVoxels = new Float32Array(voxelsEnu.length);
    for (let i = 0; i < voxelsEnu.length; i += 3) {
      mapVoxels[i] = voxelsEnu[i]!;
      mapVoxels[i + 1] = voxelsEnu[i + 2]!;
      mapVoxels[i + 2] = -voxelsEnu[i + 1]!;
    }
    set({ mapVoxels, mapResolution, mapRevision });
  },
  setScenario: (scenario) =>
    set((s) => ({ scenario, draft: s.draft ?? structuredClone(scenario.demo_mission) })),
  setDraft: (draft) => set({ draft }),
  setCameraMode: (cameraMode) => set({ cameraMode }),
  clearTrack: () => set((s) => ({ track: [], trackVersion: s.trackVersion + 1 })),

  ingest: (snapshot) => {
    const now = performance.now() / 1000;
    const prev = get();
    const t0 = prev.t0 ?? now;
    const update: Partial<Store> = { snapshot, lastMessageAt: now, t0 };
    const state = snapshot.state;

    if (state?.connected) {
      const last = prev.history[prev.history.length - 1];
      const t = now - t0;
      if (!last || t - last.t >= HISTORY_INTERVAL_S) {
        const battery = state.battery?.remaining_fraction;
        const point: HistoryPoint = {
          t,
          altitude: state.relative_altitude_m,
          groundSpeed: state.ground_speed_m_s,
          verticalSpeed: state.vertical_speed_up_m_s,
          battery: battery == null ? null : battery * 100,
        };
        update.history = [...prev.history.slice(-(HISTORY_MAX - 1)), point];
      }

      const p = state.position_enu_m;
      if (p) {
        const track = prev.track;
        const n = track.length;
        const x = p.x;
        const y = p.z;
        const z = -p.y;
        const moved =
          n === 0 ||
          Math.hypot(x - track[n - 3]!, y - track[n - 2]!, z - track[n - 1]!) > TRACK_MIN_STEP_M;
        // A replay looping back to its start would draw a line across the scene.
        const jumped =
          n > 0 && Math.hypot(x - track[n - 3]!, y - track[n - 2]!, z - track[n - 1]!) > 25;
        if (jumped) {
          update.track = [x, y, z];
          update.trackVersion = prev.trackVersion + 1;
        } else if (moved) {
          const next = n >= TRACK_MAX * 3 ? track.slice(3) : track.slice();
          next.push(x, y, z);
          update.track = next;
          update.trackVersion = prev.trackVersion + 1;
        }
      }
    }
    set(update);
  },
}));
