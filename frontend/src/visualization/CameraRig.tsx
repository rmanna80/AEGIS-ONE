import { OrbitControls } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import * as THREE from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import { useStore } from "../state/store";

const desired = new THREE.Vector3();
const forward = new THREE.Vector3();
const NORTH_UP = new THREE.Vector3(0, 0, -1);
const WORLD_UP = new THREE.Vector3(0, 1, 0);

/**
 * Camera modes:
 *  follow - chase camera behind the vehicle's heading
 *  orbit  - mouse orbit / zoom around the vehicle (target tracks it)
 *  fpv    - first-person view along the vehicle's nose, rolling/pitching with it
 *  top    - map view from above, north up
 *  free   - free observer: orbit, pan and zoom anywhere, no tracking
 */
export function CameraRig({ vehicleRef }: { vehicleRef: React.RefObject<THREE.Group | null> }) {
  const mode = useStore((s) => s.cameraMode);
  const controls = useRef<OrbitControlsImpl | null>(null);
  const { camera } = useThree();
  const manual = mode === "orbit" || mode === "free";

  useEffect(() => {
    camera.up.copy(mode === "top" ? NORTH_UP : WORLD_UP);
    const vehicle = vehicleRef.current;
    if (manual && vehicle && controls.current) {
      controls.current.target.copy(vehicle.position);
      camera.position.copy(vehicle.position).add(new THREE.Vector3(14, 10, 14));
      controls.current.update();
    }
  }, [mode, manual, camera, vehicleRef]);

  useFrame((_, dt) => {
    const vehicle = vehicleRef.current;
    if (!vehicle) return;
    const k = 1 - Math.exp(-dt * 4);

    if (mode === "follow") {
      forward.set(0, 0, -1).applyQuaternion(vehicle.quaternion).setY(0);
      if (forward.lengthSq() < 1e-6) forward.set(0, 0, -1);
      forward.normalize();
      desired.copy(vehicle.position).addScaledVector(forward, -13).add({ x: 0, y: 6, z: 0 });
      camera.position.lerp(desired, k);
      camera.lookAt(vehicle.position);
    } else if (mode === "fpv") {
      forward.set(0, 0, -0.6).applyQuaternion(vehicle.quaternion);
      camera.position.copy(vehicle.position).add(forward);
      camera.quaternion.copy(vehicle.quaternion);
    } else if (mode === "top") {
      desired.set(vehicle.position.x, 110, vehicle.position.z);
      camera.position.lerp(desired, k);
      camera.lookAt(camera.position.x, 0, camera.position.z);
    } else if (mode === "orbit" && controls.current) {
      // Move camera and target together so the user's orbit offset is preserved.
      desired.copy(vehicle.position).sub(controls.current.target);
      controls.current.target.add(desired);
      camera.position.add(desired);
      controls.current.update();
    }
  });

  return (
    <OrbitControls
      ref={controls}
      enabled={manual}
      enablePan={mode === "free"}
      maxPolarAngle={Math.PI / 2 - 0.02}
      minDistance={3}
      maxDistance={260}
    />
  );
}
