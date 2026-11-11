import { useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { useStore } from "../state/store";

const LOW = new THREE.Color("#2dd4bf");
const HIGH = new THREE.Color("#fde047");
const HEIGHT_FOR_FULL_COLOUR_M = 14;

/**
 * The live occupancy map: one instanced cube per occupied voxel, coloured by
 * height. This is what the depth sensor has reconstructed, not the scenario's
 * known geometry (which is drawn separately as solid shapes).
 */
export function OccupancyMap() {
  const voxels = useStore((s) => s.mapVoxels);
  const resolution = useStore((s) => s.mapResolution);
  const visible = useStore((s) => s.showMap);
  const mesh = useRef<THREE.InstancedMesh | null>(null);
  const count = voxels.length / 3;
  // Capacity grows in steps so the mesh is not rebuilt on every map update.
  const capacity = useMemo(() => Math.max(1024, 2 ** Math.ceil(Math.log2(count + 1))), [count]);

  useLayoutEffect(() => {
    const instanced = mesh.current;
    if (!instanced) return;
    const matrix = new THREE.Matrix4();
    const colour = new THREE.Color();
    for (let i = 0; i < count; i++) {
      const x = voxels[i * 3]!;
      const y = voxels[i * 3 + 1]!;
      const z = voxels[i * 3 + 2]!;
      matrix.makeTranslation(x, y, z);
      instanced.setMatrixAt(i, matrix);
      colour.copy(LOW).lerp(HIGH, Math.min(1, y / HEIGHT_FOR_FULL_COLOUR_M));
      instanced.setColorAt(i, colour);
    }
    instanced.count = count;
    instanced.instanceMatrix.needsUpdate = true;
    if (instanced.instanceColor) instanced.instanceColor.needsUpdate = true;
  }, [voxels, count, capacity]);

  if (!visible) return null;
  const size = resolution * 0.92;
  return (
    <instancedMesh
      key={capacity}
      ref={mesh}
      args={[undefined, undefined, capacity]}
      frustumCulled={false}
    >
      <boxGeometry args={[size, size, size]} />
      <meshStandardMaterial roughness={0.8} transparent opacity={0.85} />
    </instancedMesh>
  );
}
