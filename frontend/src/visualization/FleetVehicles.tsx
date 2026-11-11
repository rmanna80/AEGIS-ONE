import { Html, Line } from "@react-three/drei";
import { useRef } from "react";
import { enuToThree, type Triple } from "../lib/frames";
import { useStore } from "../state/store";

const COLOURS = ["#c084fc", "#fb923c", "#34d399"];
const TRACK_MAX = 3000;
const TRACK_MIN_STEP_M = 0.3;

/** The other vehicles of a fleet: a marker, a label and the path each has flown. */
export function FleetVehicles() {
  const fleet = useStore((s) => s.snapshot?.fleet);
  const tracks = useRef(new Map<string, Triple[]>());
  if (!fleet || fleet.length === 0) return null;

  return (
    <group>
      {fleet.map((vehicle, index) => {
        const p = vehicle.state?.position_enu_m;
        if (!p || !vehicle.state?.connected) return null;
        const position = enuToThree(p.x, p.y, p.z);
        const colour = COLOURS[index % COLOURS.length]!;
        let track = tracks.current.get(vehicle.vehicle_id);
        if (!track) {
          track = [];
          tracks.current.set(vehicle.vehicle_id, track);
        }
        const last = track[track.length - 1];
        if (!last || Math.hypot(last[0] - position[0], last[1] - position[1], last[2] - position[2]) > TRACK_MIN_STEP_M) {
          track.push(position);
          if (track.length > TRACK_MAX) track.shift();
        }
        return (
          <group key={vehicle.vehicle_id}>
            {track.length >= 2 && <Line points={[...track]} color={colour} lineWidth={2} />}
            <group position={position}>
              <mesh castShadow>
                <boxGeometry args={[2.2, 0.25, 0.35]} />
                <meshStandardMaterial color={colour} />
              </mesh>
              <mesh castShadow>
                <boxGeometry args={[0.35, 0.25, 2.2]} />
                <meshStandardMaterial color={colour} />
              </mesh>
              <mesh>
                <sphereGeometry args={[0.45, 16, 12]} />
                <meshStandardMaterial color="#1b222b" />
              </mesh>
              <Html position={[0, 2.2, 0]} center zIndexRange={[5, 0]}>
                <span
                  className="num whitespace-nowrap rounded-sm bg-ink-950/80 px-1 text-[11px] font-semibold"
                  style={{ color: colour }}
                >
                  {vehicle.vehicle_id} · {p.z.toFixed(0)} m
                </span>
              </Html>
            </group>
          </group>
        );
      })}
    </group>
  );
}
