// Mirrors backend/aegis/contracts.py and backend/aegis/missions.py.
// Units and frames are part of the field names; see the Python docstrings.

export type Provenance = "simulated" | "replayed" | "physical";
export type Severity = "info" | "warning" | "critical";

export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

export interface GeodeticPosition {
  latitude_deg: number;
  longitude_deg: number;
  altitude_amsl_m: number;
}

export interface VehicleState {
  vehicle_id: string;
  provenance: Provenance;
  source: string;
  recorded_provenance: Provenance | null;
  received_at_unix_s: number;
  vehicle_time_s: number | null;
  connected: boolean;
  armed: boolean | null;
  in_air: boolean | null;
  flight_mode: string | null;
  landed_state: string | null;
  origin: { geodetic: GeodeticPosition } | null;
  geodetic: GeodeticPosition | null;
  relative_altitude_m: number | null;
  /** East, North, Up in metres relative to home. Derived by AEGIS. */
  position_enu_m: Vec3 | null;
  /** x = north, y = east, z = down. */
  velocity_ned_m_s: Vec3 | null;
  attitude: {
    /** Body (FRD) -> NED. */
    quaternion_wxyz: [number, number, number, number];
    roll_deg: number;
    pitch_deg: number;
    yaw_deg: number;
  } | null;
  ground_speed_m_s: number | null;
  vertical_speed_up_m_s: number | null;
  battery: { voltage_v: number | null; remaining_fraction: number | null } | null;
  gps: { fix_type: string; num_satellites: number } | null;
  health: Record<string, boolean> | null;
  derived_fields: string[];
}

export interface Alert {
  alert_id: string;
  code: string;
  severity: Severity;
  message: string;
  vehicle_id: string;
  provenance: Provenance;
  raised_at_unix_s: number;
  active: boolean;
}

export interface AutopilotMessage {
  level: string;
  text: string;
  received_at_unix_s: number;
}

export type MissionPhase =
  | "idle"
  | "uploading"
  | "active"
  | "returning"
  | "completed"
  | "cancelled"
  | "stalled"
  | "failed"
  | "rejected";

export interface MissionStatus {
  mission_id: string | null;
  name: string | null;
  phase: MissionPhase;
  current_waypoint: number;
  total_waypoints: number;
  message: string;
  issues: string[];
  updated_at_unix_s: number;
}

export interface Snapshot {
  type: "snapshot";
  server_time_unix_s: number;
  state: VehicleState | null;
  mission: MissionStatus;
  alerts: Alert[];
  playback: { time_s: number; duration_s: number | null; speed: number } | null;
  autopilot_messages: AutopilotMessage[];
}

export interface Waypoint {
  east_m: number;
  north_m: number;
  altitude_rel_m: number;
  acceptance_radius_m?: number;
  speed_m_s?: number | null;
  hold_s?: number;
}

export interface Mission {
  mission_id: string;
  name: string;
  waypoints: Waypoint[];
  constraints: {
    geofence: { polygon_enu_m: [number, number][]; max_altitude_rel_m: number };
    min_altitude_rel_m: number;
    cruise_speed_m_s: number;
    max_leg_length_m: number;
    leg_timeout_s: number;
    return_to_home_on_complete: boolean;
  };
}

export type Obstacle =
  | {
      id: string;
      shape: "box";
      east_m: number;
      north_m: number;
      size_enu_m: [number, number, number];
      yaw_deg: number;
      color: string;
    }
  | {
      id: string;
      shape: "cylinder";
      east_m: number;
      north_m: number;
      radius_m: number;
      height_m: number;
      color: string;
    };

/** Flat, visual-only ground surface (road, pavement). */
export interface Surface {
  id: string;
  east_m: number;
  north_m: number;
  size_enu_m: [number, number];
  yaw_deg: number;
  color: string;
}

/** A staged, static detection target. Gazebo renders the real model; the dashboard draws a proxy. */
export interface Prop {
  id: string;
  kind: "person" | "vehicle";
  model: string;
  expected_labels: string[];
  east_m: number;
  north_m: number;
  yaw_deg: number;
  /** length, width, height of the proxy in metres */
  size_m: [number, number, number];
}

export interface Scenario {
  scenario_id: string;
  name: string;
  ground: { size_m: number };
  landing_pad: { east_m: number; north_m: number; radius_m: number };
  surfaces?: Surface[];
  props?: Prop[];
  obstacles: Obstacle[];
  demo_mission: Mission;
}

export interface SystemInfo {
  version: string;
  mode: "simulation" | "monitor" | "replay";
  vehicle_id: string;
  provenance: Provenance;
  source: string | null;
  commands_enabled: boolean;
  simulator_attested?: boolean;
  attestation_error?: string | null;
  replay?: { file: string; duration_s: number; recorded_source: string };
}

// --- Perception (mirrors backend/aegis/perception/types.py). Image coordinates:
// pixels, origin top-left. Motion is image-space only.

export type BBox = [number, number, number, number];

export interface Detection {
  label: string;
  class_id: number;
  confidence: number;
  bbox_xyxy_px: BBox;
}

export interface Track {
  track_id: number;
  label: string;
  class_id: number;
  state: "tentative" | "confirmed" | "coasting";
  bbox_xyxy_px: BBox;
  confidence: number;
  velocity_px_s: [number, number];
  age_frames: number;
  frames_since_update: number;
  history_px: [number, number][];
}

export interface PerceptionResult {
  frame_id: number;
  sensor_id: string;
  provenance: Provenance;
  recorded_provenance?: Provenance | null;
  source: string;
  captured_at_unix_s: number;
  source_time_s: number | null;
  width_px: number;
  height_px: number;
  detector: string;
  inference_latency_ms: number;
  detections: Detection[];
  tracks: Track[];
  selected_track_id: number | null;
}

export type PerceptionStatus =
  | { enabled: false; reason: string }
  | {
      enabled: true;
      detector: string;
      camera_source: string | null;
      provenance: Provenance | null;
      frames_processed: number;
      fps: number | null;
      inference_latency_ms_p50: number | null;
      inference_latency_ms_p95: number | null;
      last_frame_age_s: number | null;
      tracks: number;
      selected_track_id: number | null;
      faults: string[];
      last_error: string | null;
      recording: string | null;
    };
