import type {
  FleetSearchPlan,
  MapPayload,
  Mission,
  MissionStatus,
  NavigationStatus,
  PerceptionStatus,
  ReviewStatus,
  SarCandidate,
  SarPlan,
  SarStatus,
  Scenario,
  SystemInfo,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep the status text */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

function post<T>(path: string, body?: unknown, token?: string): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["X-Aegis-Operator-Token"] = token;
  return request<T>(path, {
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  system: () => request<SystemInfo>("/api/system"),
  scenario: () => request<Scenario>("/api/scenario"),
  validate: (mission: Mission) =>
    post<{ valid: boolean; issues: string[]; obstacles_checked: boolean }>(
      "/api/missions/validate",
      mission,
    ),
  plan: (mission: Mission, fromWaypoint: number) =>
    post<{ mission: Mission; detour_waypoints_added: number }>("/api/missions/plan", {
      mission,
      from_waypoint: fromWaypoint,
    }),
  activeMission: () => request<{ mission: Mission | null }>("/api/missions/active"),
  navigation: () => request<NavigationStatus>("/api/navigation"),
  map: (since: number) => request<MapPayload>(`/api/map?since=${since}`),
  submit: (mission: Mission, token: string) =>
    post<{ accepted: boolean; status: MissionStatus }>("/api/missions", mission, token),
  perception: () => request<PerceptionStatus>("/api/perception"),
  selectTrack: (trackId: number | null) =>
    post<{ selected_track_id: number | null }>("/api/perception/select", { track_id: trackId }),
  sar: () => request<SarStatus>("/api/sar"),
  sarCandidates: () =>
    request<{ candidates: SarCandidate[]; awaiting_review: number }>("/api/sar/candidates"),
  sarPlan: () => post<SarPlan>("/api/sar/plan", {}),
  sarReview: (candidateId: string, decision: ReviewStatus, note: string, token: string) =>
    post<{ candidate: SarCandidate }>(
      `/api/sar/candidates/${encodeURIComponent(candidateId)}/review`,
      { decision, note },
      token,
    ),
  sarPlanFleet: () => post<{ plans: FleetSearchPlan[] }>("/api/sar/plan/fleet", {}),
  fleetSubmit: (vehicleId: string, mission: Mission, token: string) =>
    post<{ accepted: boolean; status: MissionStatus }>(
      `/api/fleet/${encodeURIComponent(vehicleId)}/missions`,
      mission,
      token,
    ),
  fleetCommand: (vehicleId: string, command: "hold" | "return" | "land", token: string) =>
    post<unknown>(`/api/fleet/${encodeURIComponent(vehicleId)}/${command}`, undefined, token),
  fleetHoldAll: (token: string) =>
    post<{ results: Record<string, string> }>("/api/fleet/hold-all", undefined, token),
  hold: (token: string) => post<unknown>("/api/vehicle/hold", undefined, token),
  returnHome: (token: string) => post<unknown>("/api/vehicle/return", undefined, token),
  land: (token: string) => post<unknown>("/api/vehicle/land", undefined, token),
};
