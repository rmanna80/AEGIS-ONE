import { describe, expect, it } from "vitest";
import {
  bodyNedQuatToThree,
  compassPoint,
  enuToThree,
  headingDeg,
  nedToThree,
  rotate,
  type Triple,
} from "./frames";

const NOSE: Triple = [0, 0, -1]; // model forward axis
const BELLY: Triple = [0, -1, 0]; // model down axis

function yawQuat(deg: number): [number, number, number, number] {
  const h = (deg * Math.PI) / 360;
  return [Math.cos(h), 0, 0, Math.sin(h)];
}

function close(actual: Triple, expected: Triple) {
  actual.forEach((value, i) => expect(value).toBeCloseTo(expected[i]!, 9));
}

describe("frame mapping", () => {
  it("draws east on +X, up on +Y and north on -Z", () => {
    expect(enuToThree(1, 0, 0)).toEqual([1, 0, 0]);
    expect(enuToThree(0, 1, 0)).toEqual([0, 0, -1]);
    expect(enuToThree(0, 0, 1)).toEqual([0, 1, 0]);
  });

  it("maps NED consistently with ENU", () => {
    // 3 m north, 4 m east, 5 m *down* is ENU (4, 3, -5).
    expect(nedToThree(3, 4, 5)).toEqual(enuToThree(4, 3, -5));
  });
});

describe("attitude quaternion conversion", () => {
  it("identity leaves the nose pointing north", () => {
    close(rotate(bodyNedQuatToThree([1, 0, 0, 0]), NOSE), [0, 0, -1]);
  });

  it("yaw +90 deg points the nose east and keeps the belly down", () => {
    const q = bodyNedQuatToThree(yawQuat(90));
    close(rotate(q, NOSE), [1, 0, 0]);
    close(rotate(q, BELLY), [0, -1, 0]);
  });

  it("yaw 180 deg points the nose south", () => {
    close(rotate(bodyNedQuatToThree(yawQuat(180)), NOSE), [0, 0, 1]);
  });

  it("positive pitch raises the nose", () => {
    const h = (30 * Math.PI) / 360;
    const q = bodyNedQuatToThree([Math.cos(h), 0, Math.sin(h), 0]);
    const nose = rotate(q, NOSE);
    expect(nose[1]).toBeCloseTo(Math.sin(Math.PI / 6), 9); // up component
    expect(nose[2]).toBeLessThan(0); // still heading north
  });

  it("positive roll drops the right wing", () => {
    const h = (20 * Math.PI) / 360;
    const q = bodyNedQuatToThree([Math.cos(h), Math.sin(h), 0, 0]);
    const rightWing = rotate(q, [1, 0, 0]);
    expect(rightWing[1]).toBeLessThan(0);
  });
});

describe("heading helpers", () => {
  it("wraps yaw into a compass heading", () => {
    expect(headingDeg(-90)).toBe(270);
    expect(headingDeg(360)).toBe(0);
    expect(headingDeg(725)).toBe(5);
  });

  it("names compass points", () => {
    expect(compassPoint(0)).toBe("N");
    expect(compassPoint(92)).toBe("E");
    expect(compassPoint(-45)).toBe("NW");
    expect(compassPoint(359)).toBe("N");
  });
});
