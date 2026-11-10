import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";
import { bodyNedQuatToThree, enuToThree } from "../lib/frames";
import { useStore } from "../state/store";

/** The airframe is drawn larger than life so it stays visible at field scale. */
export const MODEL_SCALE = 4;

const ARM = 0.25; // metres from centre to motor (500 mm class X quad)
// PX4 "quad X" motor layout in body FRD (x forward, y right): 1 front-right,
// 2 rear-left, 3 front-left, 4 rear-right. Model axes: nose -Z, right +X.
const MOTORS: { x: number; z: number; ccw: boolean; front: boolean }[] = [
  { x: ARM * Math.SQRT1_2, z: -ARM * Math.SQRT1_2, ccw: true, front: true },
  { x: -ARM * Math.SQRT1_2, z: ARM * Math.SQRT1_2, ccw: true, front: false },
  { x: -ARM * Math.SQRT1_2, z: -ARM * Math.SQRT1_2, ccw: false, front: true },
  { x: ARM * Math.SQRT1_2, z: ARM * Math.SQRT1_2, ccw: false, front: false },
];

const targetPosition = new THREE.Vector3();
const targetQuaternion = new THREE.Quaternion();

/**
 * Quadcopter whose pose is driven only by telemetry: position from the ENU
 * estimate and attitude from the autopilot's body->NED quaternion. Between
 * 10 Hz snapshots the pose is eased, never extrapolated.
 */
export function Quadcopter({ vehicleRef }: { vehicleRef: React.RefObject<THREE.Group | null> }) {
  const props = useRef<(THREE.Group | null)[]>([]);
  const initialised = useRef(false);

  useFrame((_, dt) => {
    const group = vehicleRef.current;
    const state = useStore.getState().snapshot?.state;
    if (!group || !state?.position_enu_m) return;

    const p = state.position_enu_m;
    targetPosition.set(...enuToThree(p.x, p.y, Math.max(p.z, 0)));
    if (state.attitude) {
      targetQuaternion.set(...bodyNedQuatToThree(state.attitude.quaternion_wxyz));
    }
    const jump = group.position.distanceTo(targetPosition) > 25; // e.g. replay loop
    if (!initialised.current || jump) {
      group.position.copy(targetPosition);
      group.quaternion.copy(targetQuaternion);
      initialised.current = true;
    } else {
      const k = 1 - Math.exp(-dt * 12);
      group.position.lerp(targetPosition, k);
      group.quaternion.slerp(targetQuaternion, k);
    }

    // Rotor animation indicates the armed state only; rotor speed is not telemetered.
    if (state.armed) {
      props.current.forEach((prop, i) => {
        if (prop) prop.rotation.y += (MOTORS[i]!.ccw ? 1 : -1) * dt * 55;
      });
    }
  });

  return (
    <group ref={vehicleRef}>
      <group scale={MODEL_SCALE}>
        {/* Fuselage */}
        <mesh castShadow>
          <boxGeometry args={[0.13, 0.05, 0.2]} />
          <meshStandardMaterial color="#1b222b" metalness={0.4} roughness={0.5} />
        </mesh>
        {/* Top plate + GNSS mast */}
        <mesh position={[0, 0.032, 0]}>
          <boxGeometry args={[0.1, 0.012, 0.15]} />
          <meshStandardMaterial color="#e8edf2" roughness={0.6} />
        </mesh>
        <mesh position={[0, 0.075, 0.04]}>
          <cylinderGeometry args={[0.022, 0.022, 0.012, 20]} />
          <meshStandardMaterial color="#10151b" />
        </mesh>
        {/* Nose camera pod marks the front */}
        <mesh position={[0, -0.01, -0.115]}>
          <boxGeometry args={[0.05, 0.04, 0.035]} />
          <meshStandardMaterial color="#0b0e12" metalness={0.6} roughness={0.3} />
        </mesh>
        {MOTORS.map((m, i) => {
          const angle = Math.atan2(m.x, m.z);
          const colour = m.front ? "#ff5d5d" : "#d7e0e8";
          return (
            <group key={i}>
              {/* Arm */}
              <mesh position={[m.x / 2, 0, m.z / 2]} rotation={[0, angle, 0]} castShadow>
                <boxGeometry args={[0.022, 0.014, ARM]} />
                <meshStandardMaterial color={colour} roughness={0.5} />
              </mesh>
              {/* Motor */}
              <mesh position={[m.x, 0.02, m.z]} castShadow>
                <cylinderGeometry args={[0.02, 0.022, 0.035, 16]} />
                <meshStandardMaterial color="#2c3947" metalness={0.7} roughness={0.35} />
              </mesh>
              {/* Landing leg */}
              <mesh position={[m.x * 0.6, -0.06, m.z * 0.6]}>
                <cylinderGeometry args={[0.005, 0.005, 0.09, 8]} />
                <meshStandardMaterial color="#2c3947" />
              </mesh>
              {/* Propeller */}
              <group
                position={[m.x, 0.042, m.z]}
                ref={(node) => {
                  props.current[i] = node;
                }}
              >
                <mesh>
                  <boxGeometry args={[0.24, 0.003, 0.022]} />
                  <meshStandardMaterial color="#0d1218" />
                </mesh>
                <mesh>
                  <cylinderGeometry args={[0.12, 0.12, 0.001, 28]} />
                  <meshBasicMaterial color={colour} transparent opacity={0.12} />
                </mesh>
              </group>
            </group>
          );
        })}
      </group>
    </group>
  );
}
