// Frame conversions between the backend's aerospace frames and three.js.
//
// three.js scene convention used throughout the viewer (right-handed, Y up):
//   +X = East, +Y = Up, -Z = North
// so an ENU point (e, n, u) is drawn at (e, u, -n), and a NED vector
// (n, e, d) becomes (e, -d, -n).

export type Triple = [number, number, number];
export type QuatXYZW = [number, number, number, number];

export function enuToThree(east: number, north: number, up: number): Triple {
  // `+ 0` normalises -0 to 0 so equality checks and serialisation stay clean.
  return [east, up, -north + 0];
}

export function nedToThree(north: number, east: number, down: number): Triple {
  return [east, -down + 0, -north + 0];
}

/**
 * Convert a body(FRD)->NED quaternion (w, x, y, z) into a three.js quaternion
 * (x, y, z, w) for a model whose nose points along -Z, right wing along +X and
 * belly along -Y (i.e. the body FRD axes pushed through `nedToThree`).
 *
 * The NED->three change of basis is a proper rotation, so the quaternion's
 * vector part transforms like any other vector and the scalar part is unchanged.
 */
export function bodyNedQuatToThree(q: [number, number, number, number]): QuatXYZW {
  const [w, x, y, z] = q;
  const [tx, ty, tz] = nedToThree(x, y, z);
  return [tx, ty, tz, w];
}

/** Rotate a vector by a three.js-order (x, y, z, w) unit quaternion. */
export function rotate(q: QuatXYZW, v: Triple): Triple {
  const [qx, qy, qz, qw] = q;
  const [vx, vy, vz] = v;
  const cx = qy * vz - qz * vy;
  const cy = qz * vx - qx * vz;
  const cz = qx * vy - qy * vx;
  const ccx = qy * cz - qz * cy;
  const ccy = qz * cx - qx * cz;
  const ccz = qx * cy - qy * cx;
  return [vx + 2 * (qw * cx + ccx), vy + 2 * (qw * cy + ccy), vz + 2 * (qw * cz + ccz)];
}

/** Compass heading in [0, 360) from a yaw angle (0 = north, clockwise positive). */
export function headingDeg(yawDeg: number): number {
  return ((yawDeg % 360) + 360) % 360;
}

const POINTS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"] as const;

export function compassPoint(heading: number): string {
  return POINTS[Math.round(headingDeg(heading) / 45) % 8]!;
}
