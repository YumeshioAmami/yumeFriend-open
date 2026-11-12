export function normalizeAngle(value: number): number {
  if (!Number.isFinite(value)) return 0
  const twoPi = Math.PI * 2
  return ((value + Math.PI) % twoPi + twoPi) % twoPi - Math.PI
}
