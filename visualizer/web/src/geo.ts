const R = 6371000;
const RAD = Math.PI / 180;
export const NM = 1852;

export type XY = [number, number];

/** Local flat projection around a centre: metres east, metres south (y grows down, like the screen). */
export function makeProjection(center: number[]) {
  const k = Math.cos(center[1] * RAD);
  const toM = (lonLat: number[]): XY => [
    (lonLat[0] - center[0]) * RAD * R * k,
    -(lonLat[1] - center[1]) * RAD * R,
  ];
  const toLonLat = (m: XY): [number, number] => [
    center[0] + m[0] / (RAD * R * k),
    center[1] - m[1] / (RAD * R),
  ];
  return { toM, toLonLat };
}

export function distM(a: number[], b: number[]): number {
  const k = Math.cos(((a[1] + b[1]) / 2) * RAD);
  const dx = (b[0] - a[0]) * RAD * R * k;
  const dy = (b[1] - a[1]) * RAD * R;
  return Math.hypot(dx, dy);
}

/** Compass bearing from a to b, degrees. */
export function bearing(a: number[], b: number[]): number {
  const k = Math.cos(((a[1] + b[1]) / 2) * RAD);
  const dx = (b[0] - a[0]) * k;
  const dy = b[1] - a[1];
  return (Math.atan2(dx, dy) / RAD + 360) % 360;
}

export function lerp(a: number, b: number, f: number) {
  return a + (b - a) * f;
}

export function lerpAngle(a: number, b: number, f: number) {
  const d = ((b - a + 540) % 360) - 180;
  return (a + d * f + 360) % 360;
}
