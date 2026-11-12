/** 可抓取动态物件的轻量注册表。
 *
 * Rapier 的 raycast 在运动学手代理旁边做细长射线时容易漏掉小物件；
 * 这里按“物件中心到视线的垂直距离”选最近的可抓物件，稳定且可解释。
 */

import type { RapierRigidBody } from '@react-three/rapier'

export interface Grabbable {
  body: RapierRigidBody
  label: string
  radius: number
}

const registry = new Map<string, Grabbable>()

export function registerGrabbable(
  key: string,
  body: RapierRigidBody,
  label: string,
  radius: number,
) {
  registry.set(key, { body, label, radius })
}

export function unregisterGrabbable(key: string) {
  registry.delete(key)
}

export function getGrabbable(key: string): Grabbable | null {
  return registry.get(key) ?? null
}

export function pickGrabbable(
  origin: [number, number, number],
  direction: [number, number, number],
  maxDistance: number,
): Grabbable | null {
  let best: Grabbable | null = null
  let bestScore = Number.POSITIVE_INFINITY
  for (const item of registry.values()) {
    if (!item.body.isValid()) continue
    const position = item.body.translation()
    const vx = position.x - origin[0]
    const vy = position.y - origin[1]
    const vz = position.z - origin[2]
    const along = vx * direction[0] + vy * direction[1] + vz * direction[2]
    if (along <= 0.08 || along > maxDistance) continue
    const px = vx - direction[0] * along
    const py = vy - direction[1] * along
    const pz = vz - direction[2] * along
    const perpendicular = Math.hypot(px, py, pz)
    if (perpendicular > item.radius) continue
    const score = perpendicular + along * 0.02
    if (score < bestScore) {
      best = item
      bestScore = score
    }
  }
  return best
}
