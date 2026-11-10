import { Html, Line } from "@react-three/drei";
import { useMemo } from "react";
import * as THREE from "three";
import { enuToThree, type Triple } from "../lib/frames";
import { useStore } from "../state/store";
import type { Mission, Obstacle, Scenario } from "../types";

function ObstacleMesh({ obstacle }: { obstacle: Obstacle }) {
  if (obstacle.shape === "box") {
    const [sizeEast, sizeNorth, height] = obstacle.size_enu_m;
    return (
      <mesh
        position={enuToThree(obstacle.east_m, obstacle.north_m, height / 2)}
        // ENU yaw is counter-clockwise about Up, which is +Y here.
        rotation={[0, THREE.MathUtils.degToRad(obstacle.yaw_deg), 0]}
        castShadow
        receiveShadow
      >
        <boxGeometry args={[sizeEast, height, sizeNorth]} />
        <meshStandardMaterial color={obstacle.color} roughness={0.8} />
      </mesh>
    );
  }
  return (
    <mesh
      position={enuToThree(obstacle.east_m, obstacle.north_m, obstacle.height_m / 2)}
      castShadow
      receiveShadow
    >
      <cylinderGeometry args={[obstacle.radius_m, obstacle.radius_m, obstacle.height_m, 24]} />
      <meshStandardMaterial color={obstacle.color} roughness={0.8} />
    </mesh>
  );
}

function Geofence({ mission }: { mission: Mission }) {
  const { polygon_enu_m: polygon, max_altitude_rel_m: ceiling } = mission.constraints.geofence;
  const ring = (up: number): Triple[] => {
    const points = polygon.map(([e, n]) => enuToThree(e, n, up));
    return [...points, points[0]!];
  };
  return (
    <group>
      <Line points={ring(0.05)} color="#ff5d5d" lineWidth={1.5} dashed dashSize={3} gapSize={2} />
      <Line points={ring(ceiling)} color="#ff5d5d" lineWidth={1} transparent opacity={0.45} />
      {polygon.map(([e, n], i) => (
        <Line
          key={i}
          points={[enuToThree(e, n, 0), enuToThree(e, n, ceiling)]}
          color="#ff5d5d"
          lineWidth={1}
          transparent
          opacity={0.45}
        />
      ))}
    </group>
  );
}

function PlannedRoute({ mission }: { mission: Mission }) {
  const status = useStore((s) => s.snapshot?.mission);
  const reached = status?.mission_id === mission.mission_id ? status.current_waypoint : 0;
  const first = mission.waypoints[0];
  const path = useMemo(() => {
    if (!first) return [];
    const points: Triple[] = [enuToThree(0, 0, 0), enuToThree(0, 0, first.altitude_rel_m)];
    for (const wp of mission.waypoints) {
      points.push(enuToThree(wp.east_m, wp.north_m, wp.altitude_rel_m));
    }
    return points;
  }, [mission, first]);
  if (path.length < 2) return null;
  return (
    <group>
      <Line points={path} color="#ff8c1a" lineWidth={2} dashed dashSize={1.5} gapSize={1} />
      {mission.waypoints.map((wp, i) => {
        const done = i < reached;
        const colour = done ? "#3ecf8e" : "#ff8c1a";
        return (
          <group key={i}>
            <mesh position={enuToThree(wp.east_m, wp.north_m, wp.altitude_rel_m)}>
              <sphereGeometry args={[0.7, 16, 16]} />
              <meshBasicMaterial color={colour} />
            </mesh>
            {/* Drop line + ground marker make the waypoint's map position readable. */}
            <Line
              points={[
                enuToThree(wp.east_m, wp.north_m, 0),
                enuToThree(wp.east_m, wp.north_m, wp.altitude_rel_m),
              ]}
              color={colour}
              lineWidth={1}
              transparent
              opacity={0.5}
            />
            <mesh
              position={enuToThree(wp.east_m, wp.north_m, 0.04)}
              rotation={[-Math.PI / 2, 0, 0]}
            >
              <ringGeometry args={[0.5, 0.8, 24]} />
              <meshBasicMaterial color={colour} side={THREE.DoubleSide} />
            </mesh>
            <Html
              position={enuToThree(wp.east_m, wp.north_m, wp.altitude_rel_m + 1.6)}
              center
              zIndexRange={[5, 0]}
            >
              <div
                className="num pointer-events-none whitespace-nowrap rounded-sm border px-1 text-[10px]"
                style={{ borderColor: colour, color: colour, background: "#080b10cc" }}
              >
                WP{i + 1} · {wp.altitude_rel_m.toFixed(0)} m
              </div>
            </Html>
          </group>
        );
      })}
    </group>
  );
}

function FlownTrack() {
  const version = useStore((s) => s.trackVersion);
  const points = useMemo(() => {
    const flat = useStore.getState().track;
    const out: Triple[] = [];
    for (let i = 0; i < flat.length; i += 3) out.push([flat[i]!, flat[i + 1]!, flat[i + 2]!]);
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);
  if (points.length < 2) return null;
  return <Line points={points} color="#4cc9f0" lineWidth={2.5} />;
}

export function Environment({ scenario }: { scenario: Scenario }) {
  const draft = useStore((s) => s.draft);
  const size = scenario.ground.size_m;
  const pad = scenario.landing_pad;
  return (
    <group>
      {/* Ground sits slightly below z = 0 so the grid and markers never z-fight with it. */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.06, 0]} receiveShadow>
        <planeGeometry args={[size, size]} />
        <meshStandardMaterial color="#2a3d31" roughness={1} />
      </mesh>
      <gridHelper args={[size, size / 10, "#5f7689", "#3d5163"]} />

      {/* Landing pad */}
      <mesh position={enuToThree(pad.east_m, pad.north_m, 0.03)} rotation={[-Math.PI / 2, 0, 0]}>
        <circleGeometry args={[pad.radius_m, 40]} />
        <meshStandardMaterial color="#e8edf2" roughness={0.9} />
      </mesh>
      <mesh position={enuToThree(pad.east_m, pad.north_m, 0.04)} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[pad.radius_m * 0.72, pad.radius_m * 0.86, 40]} />
        <meshBasicMaterial color="#f5a524" />
      </mesh>

      {/* North reference */}
      <Line
        points={[enuToThree(0, pad.radius_m + 1, 0.05), enuToThree(0, pad.radius_m + 7, 0.05)]}
        color="#d7e0e8"
        lineWidth={2}
      />
      <Html position={enuToThree(0, pad.radius_m + 8.5, 0.5)} center zIndexRange={[5, 0]}>
        <div className="num pointer-events-none text-[11px] font-semibold text-[#d7e0e8]">N</div>
      </Html>

      {scenario.obstacles.map((o) => (
        <ObstacleMesh key={o.id} obstacle={o} />
      ))}
      {draft && <Geofence mission={draft} />}
      {draft && <PlannedRoute mission={draft} />}
      <FlownTrack />
    </group>
  );
}
