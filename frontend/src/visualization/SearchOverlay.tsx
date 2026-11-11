import { Html, Line } from "@react-three/drei";
import { enuToThree } from "../lib/frames";
import { useStore } from "../state/store";
import type { ReviewStatus } from "../types";

const COLOUR: Record<ReviewStatus, string> = {
  unreviewed: "#facc15",
  confirmed: "#3ecf8e",
  dismissed: "#6b7280",
};

/** Search area outline and heat-source candidates (positions are estimates). */
export function SearchOverlay() {
  const sar = useStore((s) => s.sar);
  const candidates = useStore((s) => s.sarCandidates);
  if (!sar?.enabled) return null;
  const area = sar.search_area_enu_m;
  const outline = [...area, area[0]!].map(([east, north]) => enuToThree(east, north, 0.08));
  return (
    <group>
      <Line points={outline} color="#facc15" lineWidth={1.5} dashed dashSize={2} gapSize={1.5} />
      {candidates.map((c) => {
        const [x, y, z] = enuToThree(c.east_m, c.north_m, 0.1);
        return (
          <group key={c.candidate_id} position={[x, y, z]}>
            <mesh rotation={[-Math.PI / 2, 0, 0]}>
              <ringGeometry args={[1.4, 1.9, 32]} />
              <meshBasicMaterial color={COLOUR[c.status]} transparent opacity={0.9} />
            </mesh>
            <Line points={[[0, 0, 0], [0, 5, 0]]} color={COLOUR[c.status]} lineWidth={1.5} />
            <Html position={[0, 5.5, 0]} center zIndexRange={[5, 0]}>
              <span
                className="num rounded-sm bg-ink-950/80 px-1 text-[11px] font-semibold"
                style={{ color: COLOUR[c.status] }}
              >
                {c.candidate_id}
              </span>
            </Html>
          </group>
        );
      })}
    </group>
  );
}
