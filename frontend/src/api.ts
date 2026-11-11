import type { Mission, MissionStatus, PerceptionStatus, Scenario, SystemInfo } from "./types";

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
    post<{ valid: boolean; issues: string[] }>("/api/missions/validate", mission),
  submit: (mission: Mission, token: string) =>
    post<{ accepted: boolean; status: MissionStatus }>("/api/missions", mission, token),
  perception: () => request<PerceptionStatus>("/api/perception"),
  selectTrack: (trackId: number | null) =>
    post<{ selected_track_id: number | null }>("/api/perception/select", { track_id: trackId }),
  hold: (token: string) => post<unknown>("/api/vehicle/hold", undefined, token),
  returnHome: (token: string) => post<unknown>("/api/vehicle/return", undefined, token),
  land: (token: string) => post<unknown>("/api/vehicle/land", undefined, token),
};
