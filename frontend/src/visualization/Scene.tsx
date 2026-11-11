import { Canvas } from "@react-three/fiber";
import { useRef } from "react";
import type * as THREE from "three";
import { type CameraMode, useStore } from "../state/store";
import { CameraRig } from "./CameraRig";
import { Environment } from "./Environment";
import { OccupancyMap } from "./OccupancyMap";
import { MODEL_SCALE, Quadcopter } from "./Quadcopter";
import { SearchOverlay } from "./SearchOverlay";

const MODES: { id: CameraMode; label: string; hint: string }[] = [
  { id: "follow", label: "Follow", hint: "Third-person chase camera" },
  { id: "orbit", label: "Orbit", hint: "Drag to orbit the drone, scroll to zoom" },
  { id: "fpv", label: "FPV", hint: "First-person view from the nose" },
  { id: "top", label: "Top", hint: "Top-down map view, north up" },
  { id: "free", label: "Free", hint: "Free observer: drag, right-drag to pan, scroll to zoom" },
];

export function Scene() {
  const scenario = useStore((s) => s.scenario);
  const mode = useStore((s) => s.cameraMode);
  const setMode = useStore((s) => s.setCameraMode);
  const clearTrack = useStore((s) => s.clearTrack);
  const hasPose = useStore((s) => Boolean(s.snapshot?.state?.position_enu_m));
  const perception = useStore((s) => s.perception);
  const navigation = useStore((s) => s.navigation);
  const showMap = useStore((s) => s.showMap);
  const toggleMap = useStore((s) => s.toggleMap);
  const mapping = navigation?.enabled && navigation.depth_source !== null;
  const searching = useStore((s) => Boolean(s.sar?.enabled));
  const vehicleRef = useRef<THREE.Group | null>(null);

  return (
    <div className="relative h-full w-full overflow-hidden bg-ink-950">
      <Canvas shadows camera={{ position: [16, 10, 16], fov: 55, near: 0.1, far: 1200 }}>
        <color attach="background" args={["#0b1118"]} />
        <fog attach="fog" args={["#0b1118", 160, 420]} />
        <hemisphereLight args={["#b9d4ef", "#1c2a22", 0.9]} />
        <directionalLight
          position={[60, 120, 40]}
          intensity={1.6}
          castShadow
          shadow-mapSize={[2048, 2048]}
          shadow-camera-left={-100}
          shadow-camera-right={100}
          shadow-camera-top={100}
          shadow-camera-bottom={-100}
        />
        {scenario && <Environment scenario={scenario} />}
        <OccupancyMap />
        <SearchOverlay />
        <Quadcopter vehicleRef={vehicleRef} />
        <CameraRig vehicleRef={vehicleRef} />
      </Canvas>

      <div className="absolute left-3 top-3 flex gap-1" role="group" aria-label="Camera mode">
        {MODES.map((m) => (
          <button
            key={m.id}
            title={m.hint}
            aria-pressed={mode === m.id}
            onClick={() => setMode(m.id)}
            className={`rounded-sm border px-2.5 py-1 text-[11px] font-medium uppercase tracking-wider transition-colors ${
              mode === m.id
                ? "border-track bg-track/15 text-track"
                : "border-line bg-ink-900/80 text-dim hover:text-text"
            }`}
          >
            {m.label}
          </button>
        ))}
        <button
          onClick={clearTrack}
          title="Clear the drawn flight trajectory"
          className="ml-2 rounded-sm border border-line bg-ink-900/80 px-2.5 py-1 text-[11px] uppercase tracking-wider text-dim hover:text-text"
        >
          Clear track
        </button>
        {mapping && (
          <button
            onClick={toggleMap}
            aria-pressed={showMap}
            title="Show or hide the occupancy map reconstructed from the depth sensor"
            className={`rounded-sm border px-2.5 py-1 text-[11px] uppercase tracking-wider ${
              showMap ? "border-ok/60 bg-ok/10 text-ok" : "border-line bg-ink-900/80 text-dim hover:text-text"
            }`}
          >
            Map
          </button>
        )}
      </div>

      <div className="num absolute bottom-3 left-3 flex flex-col gap-1 rounded-sm border border-line bg-ink-900/85 px-2.5 py-2 text-[10px] text-dim">
        <Legend colour="#4cc9f0" label="Flown trajectory (telemetry)" />
        <Legend colour="#ff8c1a" label="Planned route / waypoint" dashed />
        <Legend colour="#3ecf8e" label="Waypoint reached" />
        <Legend colour="#ff5d5d" label="Geofence" dashed />
        {scenario?.props && scenario.props.length > 0 && (
          <Legend colour="#f472b6" label="Staged person / vehicle (proxy shape)" />
        )}
        {mapping && <Legend colour="#2dd4bf" label="Mapped obstacle voxel (depth sensor)" />}
        {searching && (
          <Legend colour="#facc15" label="Search area / heat source awaiting review" dashed />
        )}
        <span className="mt-1 text-[9px]">
          Vehicle drawn at {MODEL_SCALE}x scale · 10 m grid · ENU frame
        </span>
      </div>

      {navigation?.enabled && (
        <div className="num absolute bottom-3 right-3 rounded-sm border border-line bg-ink-900/85 px-2.5 py-2 text-[10px] text-dim">
          <div className="panel-title mb-1">Navigation</div>
          {navigation.depth_source ? (
            <>
              <div>
                depth: <span className="text-sim">{navigation.depth_provenance ?? "waiting"}</span>{" "}
                {navigation.depth_source}
              </div>
              <div>
                map: {navigation.occupied_voxels.toLocaleString()} voxels @ {navigation.resolution_m} m ·{" "}
                {navigation.frames_integrated.toLocaleString()} frames
              </div>
              <div>
                nearest mapped obstacle:{" "}
                <span className="text-text">
                  {navigation.nearest_obstacle_m == null
                    ? "none within 20 m"
                    : `${navigation.nearest_obstacle_m.toFixed(1)} m`}
                </span>
              </div>
            </>
          ) : (
            <div>no depth sensor (AEGIS_DEPTH)</div>
          )}
          <div>
            route checks: {navigation.uses_prior_map ? "known obstacles + map" : "map only"} ·{" "}
            {navigation.clearance_m} m clearance · auto-hold {navigation.auto_hold ? "on" : "off"}
          </div>
        </div>
      )}

      {perception && !perception.enabled && (
        <div
          className="absolute right-3 top-3 max-w-[340px] rounded-sm border border-line bg-ink-900/85 px-2.5 py-1.5 text-[10px] text-dim"
          title="Perception is optional. See README: Camera and object detection."
        >
          <span className="panel-title">Camera / AI off</span> · {perception.reason}
        </div>
      )}

      {!hasPose && (
        <div className="absolute inset-0 flex items-center justify-center bg-ink-950/70">
          <div className="rounded-sm border border-line bg-ink-900 px-5 py-4 text-center">
            <div className="panel-title mb-1">No vehicle pose</div>
            <div className="text-[12px] text-dim">
              Waiting for telemetry. Start PX4 SITL, or run the backend in replay mode.
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Legend({ colour, label, dashed }: { colour: string; label: string; dashed?: boolean }) {
  return (
    <span className="flex items-center gap-2">
      <span
        className="inline-block w-5"
        style={{ borderTop: `2px ${dashed ? "dashed" : "solid"} ${colour}` }}
      />
      {label}
    </span>
  );
}
