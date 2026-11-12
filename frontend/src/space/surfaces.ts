import type { Vec3, WorldObject, WorldState } from './world'

export type SurfaceKind =
  | 'floor'
  | 'seat'
  | 'bed'
  | 'cushion'
  | 'nest'
  | 'table'
  | 'other'

export interface SpaceSurface {
  key: string
  label: string
  kind: SurfaceKind
  center: Vec3
  top: number
  half: [number, number]
  rotation: number
  walkable: boolean
  sittable: boolean
  lieable: boolean
  source: 'room' | 'fixed' | 'world'
  objectId?: string
}

export interface SurfaceProps {
  cushion?: Vec3
}

export const FLOOR_SURFACE_KEY = 'floor'
export const SIT_ROOT_OFFSET = 0.75
export const LIE_ROOT_LIFT = 0.12
export const WALK_STEP_UP = 0.08
export const MAX_WALK_STEP = 0.28
// 角色胶囊半径约 0.28m。站在家具边缘外留 6cm 余量，
// 再远就会变成「离椅子还有一米就开始滑过去」。
export const SURFACE_STANDOFF = 0.34

interface AssetSurfaceSpec {
  kind: SurfaceKind
  top: number
  half: [number, number]
  walkable: boolean
  sittable: boolean
  lieable: boolean
}

const ASSET_SURFACES: Record<string, AssetSurfaceSpec> = {
  table: { kind: 'table', top: 0.77, half: [0.65, 0.375], walkable: false, sittable: false, lieable: false },
  chair: { kind: 'seat', top: 0.51, half: [0.23, 0.23], walkable: false, sittable: true, lieable: false },
  shelf: { kind: 'other', top: 1.8, half: [0.48, 0.18], walkable: false, sittable: false, lieable: false },
  bed: { kind: 'bed', top: 0.5, half: [0.62, 0.32], walkable: false, sittable: true, lieable: true },
  rug: { kind: 'floor', top: 0.024, half: [0.75, 0.75], walkable: true, sittable: true, lieable: true },
  plant: { kind: 'other', top: 0.78, half: [0.24, 0.24], walkable: false, sittable: false, lieable: false },
  lamp: { kind: 'other', top: 1.58, half: [0.18, 0.18], walkable: false, sittable: false, lieable: false },
  box: { kind: 'other', top: 0.56, half: [0.28, 0.28], walkable: false, sittable: false, lieable: false },
  cushion: { kind: 'cushion', top: 0.22, half: [0.3, 0.23], walkable: true, sittable: true, lieable: false },
  blanket: { kind: 'floor', top: 0.07, half: [0.42, 0.3], walkable: true, sittable: true, lieable: true },
  nest: { kind: 'nest', top: 0.32, half: [0.48, 0.48], walkable: false, sittable: true, lieable: true },
}

const FLOOR_SURFACE: SpaceSurface = {
  key: FLOOR_SURFACE_KEY,
  label: '地板',
  kind: 'floor',
  center: [0, 0, 0],
  top: 0,
  half: [4.95, 4.95],
  rotation: 0,
  walkable: true,
  sittable: true,
  lieable: true,
  source: 'room',
}

const FIXED_SURFACES: SpaceSurface[] = [
  {
    key: 'fixed-bed',
    label: '固定床面',
    kind: 'bed',
    center: [2.8, 0, -4.3],
    top: 0.34,
    half: [0.72, 0.33],
    rotation: 0,
    walkable: false,
    sittable: true,
    lieable: true,
    source: 'fixed',
  },
  {
    key: 'fixed-desk',
    label: '固定桌面',
    kind: 'table',
    center: [3.45, 0, 4.22],
    top: 0.75,
    half: [0.58, 0.24],
    rotation: 0,
    walkable: false,
    sittable: false,
    lieable: false,
    source: 'fixed',
  },
]

function objectSurface(object: WorldObject): SpaceSurface | null {
  const spec = ASSET_SURFACES[object.asset]
  if (!spec) return null
  const scale = Math.max(0.2, Number(object.scale) || 1)
  return {
    key: object.id,
    label: `${object.asset === 'bed' ? '床' : object.asset === 'chair' ? '椅子' : object.asset === 'cushion' ? '靠垫' : object.asset === 'nest' ? '小窝' : object.asset === 'rug' ? '地毯' : object.asset === 'blanket' ? '毯子' : '家具'}面`,
    kind: spec.kind,
    center: [object.position[0], object.position[1], object.position[2]],
    top: object.position[1] + spec.top * scale,
    half: [spec.half[0] * scale, spec.half[1] * scale],
    rotation: object.rotation,
    walkable: spec.walkable,
    sittable: spec.sittable,
    lieable: spec.lieable,
    source: 'world',
    objectId: object.id,
  }
}

export function buildSurfaces(
  world: WorldState | null | undefined,
  props?: SurfaceProps | null,
): SpaceSurface[] {
  const result = [FLOOR_SURFACE, ...FIXED_SURFACES]
  if (props?.cushion) {
    result.push({
      key: 'cushion',
      label: '靠垫面',
      kind: 'cushion',
      center: props.cushion,
      top: props.cushion[1] + 0.14,
      half: [0.32, 0.24],
      rotation: 0,
      walkable: true,
      sittable: true,
      lieable: false,
      source: 'room',
    })
  }
  for (const object of world?.objects ?? []) {
    const surface = objectSurface(object)
    if (surface) result.push(surface)
  }
  return result
}

export function pointInSurface(surface: SpaceSurface, x: number, z: number): boolean {
  const dx = x - surface.center[0]
  const dz = z - surface.center[2]
  const cos = Math.cos(surface.rotation)
  const sin = Math.sin(surface.rotation)
  const localX = dx * cos - dz * sin
  const localZ = dx * sin + dz * cos
  return Math.abs(localX) <= surface.half[0] && Math.abs(localZ) <= surface.half[1]
}

export function groundHeightAt(
  surfaces: SpaceSurface[],
  x: number,
  z: number,
  maxTop = MAX_WALK_STEP,
): number {
  let height = 0
  for (const surface of surfaces) {
    if (!surface.walkable || surface.top > maxTop) continue
    if (!pointInSurface(surface, x, z)) continue
    height = Math.max(height, surface.top)
  }
  return height
}

export function highestBlockingSurfaceAt(
  surfaces: SpaceSurface[],
  x: number,
  z: number,
  minTop = MAX_WALK_STEP,
): SpaceSurface | null {
  let best: SpaceSurface | null = null
  for (const surface of surfaces) {
    if (surface.kind === 'floor') continue
    if (surface.top < minTop) continue
    if (!pointInSurface(surface, x, z)) continue
    if (!best || surface.top > best.top) best = surface
  }
  return best
}

export function surfaceAtPoint(
  surfaces: SpaceSurface[],
  x: number,
  z: number,
  minTop = 0,
): SpaceSurface | null {
  let best: SpaceSurface | null = null
  for (const surface of surfaces) {
    if (surface.kind === 'floor' || surface.top < minTop) continue
    if (!pointInSurface(surface, x, z)) continue
    if (!best || surface.top > best.top) best = surface
  }
  return best
}

function findAliasSurface(
  surfaces: SpaceSurface[],
  target: string,
  from: Vec3,
): SpaceSurface | null {
  const aliasAssets: Record<string, string[]> = {
    bed: ['bed'],
    cushion: ['cushion'],
    chair: ['chair'],
    nest: ['nest'],
  }
  const assets = aliasAssets[target]
  if (!assets) return null
  const candidates = surfaces.filter(
    (surface) =>
      (surface.source === 'fixed' || surface.source === 'world') &&
      assets.includes(surface.kind === 'bed' ? 'bed' : surface.kind === 'seat' ? 'chair' : surface.kind) &&
      surface.sittable,
  )
  if (!candidates.length) return null
  candidates.sort(
    (a, b) =>
      Math.hypot(a.center[0] - from[0], a.center[2] - from[2]) -
      Math.hypot(b.center[0] - from[0], b.center[2] - from[2]),
  )
  return candidates[0]
}

export function nearestApproachPoint(
  surface: SpaceSurface,
  from: Vec3,
  surfaces: SpaceSurface[],
): Vec3 {
  if (surface.kind === 'floor') {
    return [from[0], groundHeightAt(surfaces, from[0], from[2]), from[2]]
  }
  const cos = Math.cos(surface.rotation)
  const sin = Math.sin(surface.rotation)
  const localCandidates: [number, number][] = [
    [0, surface.half[1] + SURFACE_STANDOFF],
    [surface.half[0] + SURFACE_STANDOFF, 0],
    [-surface.half[0] - SURFACE_STANDOFF, 0],
    [0, -surface.half[1] - SURFACE_STANDOFF],
  ]
  const candidates = localCandidates
    .map(([localX, localZ], index) => {
      const rawX = surface.center[0] + localX * cos + localZ * sin
      const rawZ = surface.center[2] - localX * sin + localZ * cos
      const x = Math.min(4.62, Math.max(-4.62, rawX))
      const z = Math.min(4.62, Math.max(-4.62, rawZ))
      // 椅子有靠背：优先从正面（local +Z）接近，不能从背后骑上去。
      const frontPenalty = surface.kind === 'seat' && index !== 0 ? 1.6 : 0
      return {
        point: [x, z] as [number, number],
        score: Math.hypot(x - from[0], z - from[2]) + frontPenalty,
      }
    })
    .filter(({ point: [x, z] }) => {
      const blocker = highestBlockingSurfaceAt(surfaces, x, z, MAX_WALK_STEP)
      return !blocker || blocker.key === surface.key
    })
  const pool = candidates.length
    ? candidates
    : localCandidates.map(([localX, localZ]) => {
        const x = surface.center[0] + localX * cos + localZ * sin
        const z = surface.center[2] - localX * sin + localZ * cos
        return {
          point: [
            Math.min(4.62, Math.max(-4.62, x)),
            Math.min(4.62, Math.max(-4.62, z)),
          ] as [number, number],
          score: Math.hypot(x - from[0], z - from[2]),
        }
      })
  pool.sort((a, b) => a.score - b.score)
  const [x, z] = pool[0].point
  return [x, groundHeightAt(surfaces, x, z), z]
}

export interface PostureTarget {
  ok: boolean
  kind: 'sit' | 'lie'
  surface: SpaceSurface | null
  standPoint: Vec3 | null
  /** 坐到支撑面上以后身体应朝的方向；地板沿用当前朝向。 */
  facingYaw: number | null
  reason: string
}

export function resolvePostureTarget(
  world: WorldState | null | undefined,
  kind: 'sit' | 'lie',
  target: string | null,
  from: Vec3,
  props?: SurfaceProps | null,
): PostureTarget {
  const surfaces = buildSurfaces(world, props)
  const wanted = (target || FLOOR_SURFACE_KEY).trim()
  let surface: SpaceSurface | null = null

  if (wanted === FLOOR_SURFACE_KEY || wanted === 'ground' || wanted === '地板') {
    surface = FLOOR_SURFACE
  } else {
    surface = surfaces.find((item) => item.key === wanted) ?? null
    if (!surface) surface = findAliasSurface(surfaces, wanted, from)
  }

  if (!surface) {
    return {
      ok: false,
      kind,
      surface: null,
      standPoint: null,
      facingYaw: null,
      reason: '那里没有可用的支撑面',
    }
  }
  if (kind === 'sit' && !surface.sittable) {
    return {
      ok: false,
      kind,
      surface,
      standPoint: null,
      facingYaw: null,
      reason: '这个位置不能坐',
    }
  }
  if (kind === 'lie' && !surface.lieable) {
    return {
      ok: false,
      kind,
      surface,
      standPoint: null,
      facingYaw: null,
      reason: '这个位置不能躺',
    }
  }
  if (surface.kind !== 'floor' && surface.top > 1.0) {
    return {
      ok: false,
      kind,
      surface,
      standPoint: null,
      facingYaw: null,
      reason: '这个表面太高了',
    }
  }
  return {
    ok: true,
    kind,
    surface,
    standPoint: nearestApproachPoint(surface, from, surfaces),
    facingYaw: surface.kind === 'floor' ? null : surface.rotation,
    reason: '',
  }
}

export function postureRootY(top: number, kind: 'sit' | 'lie'): number {
  return kind === 'sit' ? top - SIT_ROOT_OFFSET : top + LIE_ROOT_LIFT
}
