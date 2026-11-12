import {
  Component,
  Suspense,
  useCallback,
  useEffect,
  memo,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
} from 'react'
import { Canvas, useFrame, useLoader, useThree, type ThreeEvent } from '@react-three/fiber'
import { Html, RoundedBox } from '@react-three/drei'
import {
  BallCollider,
  CapsuleCollider,
  CuboidCollider,
  CylinderCollider,
  Physics,
  RigidBody,
  useRapier,
  type RapierCollider,
  type RapierRigidBody,
} from '@react-three/rapier'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import {
  VRMLoaderPlugin,
  VRMUtils,
  type VRM,
  type VRMHumanBoneName,
} from '@pixiv/three-vrm'
import * as THREE from 'three'

import {
  api,
  type Friend,
  type SpaceAction,
  type SpaceDiscovery,
  type SpaceIdleKind,
  type SpaceKnowledge,
  type SpaceObjectKey,
  type SpaceOutfit,
  type SpacePerceptionObject,
  type SpaceProfile,
  type SpaceRuntime,
  type SpaceWorldModel,
  type SpaceSelfState,
  type SpaceState,
} from '../shared/api'
import {
  applyRetargetedPose,
  buildRetargetedClips,
  sampleRetargetedClip,
  type RetargetedClip,
} from './animation'
import { stepSpring, type SpringState } from './springMath'
import {
  buildSurfaces,
  groundHeightAt,
  pointInSurface,
  postureRootY,
  resolvePostureTarget,
  WALK_STEP_UP,
  MAX_WALK_STEP,
} from './surfaces'
import {
  DEFAULT_WORLD,
  WORLD_ASSETS,
  WhiteRoom,
  WorldObjectView,
  type WorldObject,
  type WorldState,
} from './world'
import { FreeFlyControls, createViewerSnapshot, type ViewerSnapshot } from './freeFly'
import { UserHand } from './userHand'
import { RoomColliders } from './physicsWorld'
import { updateSpaceDebug } from './debug'
import { getGrabbable, registerGrabbable, unregisterGrabbable } from './grabRegistry'

type Attention = 'busy' | 'notice' | 'engaged'
type Reaction =
  | 'head'
  | 'ear'
  | 'tail'
  | 'hand'
  | 'body'
  | 'neck'
  | 'chest'
  | 'waist'
  | 'thigh'
  | 'butt'
type Vec3 = [number, number, number]
type SpaceMode = 'free' | 'together'
type AvatarContactKind = 'hold' | 'drag' | 'push' | 'pull'

interface AvatarGrabState {
  zone: Reaction
  startedAt: number
  offset: THREE.Vector3
  offsetY: number
  lastPosition: THREE.Vector3
  lastDistanceToCamera: number
  lastAt: number
  kind: AvatarContactKind
  moved: boolean
  /** 抓取开始时的动画；抓取期间继续播放，不让姿势突然变成站立/待机。 */
  preserveClip: string
  /** 抓取开始时是否在走路；抓取期间只锁物理位移，原地继续走路动作。 */
  preserveMoving: boolean
}

const BODY_ZONE_LABELS: Record<Reaction, string> = {
  head: '头',
  ear: '耳朵',
  tail: '尾巴',
  hand: '手',
  body: '身体',
  neck: '脖子',
  chest: '胸口',
  waist: '腰',
  thigh: '腿',
  butt: '臀部',
}

/** 专用触区优先于大范围身体区。 */
const TOUCH_ZONE_PRIORITY: Record<string, number> = {
  ear: 60,
  hand: 55,
  tail: 55,
  head: 45,
  // 脖子只在大球/胸口没命中时才该赢；它以前高于胸口，会把靠近领口的触摸抢成脖子。
  neck: 15,
  chest: 20,
  waist: 18,
  butt: 18,
  thigh: 10,
}
const MAX_TOUCH_DISTANCE = 4.2
const TOUCH_ANCHOR_OFFSET: Record<string, number> = {
  chest: 0.05,
  waist: 0.04,
}

const BODY_DYNAMIC = 0
const BODY_KINEMATIC_POSITION = 2
const SPACE_ARRIVAL_DISTANCE = 0.16
const SPACE_CAMERA = {
  position: [0, 1.5, 3.6] as [number, number, number],
  fov: 58,
  near: 0.03,
}
const SPACE_GL = {
  antialias: true,
  alpha: false,
  powerPreference: 'high-performance' as const,
}
interface Locomotion {
  position: Vec3
  yaw: number
  posture: 'stand' | 'sit' | 'lie'
  sitting: boolean
  lying: boolean
  seatHeight: number
  rootY: number
  supportKey: string | null
  supportTop: number
  seatPoint: Vec3 | null
  pendingPosture: 'sit' | 'lie' | null
  mount?: MountState | null
  targetKey: string | null
}

interface MountState {
  kind: 'sit' | 'lie'
  startedAt: number
  duration: number
  from: Vec3
  to: Vec3
  rootStartY: number
  rootEndY: number
  faceYaw: number
}

interface WalkMotion {
  key: string
  decisionUntil: number
  topSpeed: number
  speed: number
  acceleration: number
  deceleration: number
  seed: number
}

function smootherStep(value: number): number {
  const t = THREE.MathUtils.clamp(value, 0, 1)
  return t * t * t * (t * (t * 6 - 15) + 10)
}

function mountProgress(mount: MountState, now: number): number {
  return THREE.MathUtils.clamp((now - mount.startedAt) / mount.duration, 0, 1)
}

function mountRootY(mount: MountState, progress: number): number {
  return THREE.MathUtils.lerp(
    mount.rootStartY,
    mount.rootEndY,
    smootherStep(progress),
  )
}

function mountPosition(mount: MountState, progress: number): [number, number] {
  const t = smootherStep(progress)
  return [
    THREE.MathUtils.lerp(mount.from[0], mount.to[0], t),
    THREE.MathUtils.lerp(mount.from[2], mount.to[2], t),
  ]
}

interface SpaceViewProps {
  friend: Friend
  onClose: () => void
  /** 手机端：不锁鼠标，右侧拖动转视角，左侧虚拟摇杆移动。 */
  variant?: 'desktop' | 'mobile'
}

function TouchJoystick({
  move,
}: {
  move: MutableRefObject<{ x: number; y: number }>
}) {
  const [knob, setKnob] = useState({ x: 0, y: 0 })
  const active = useRef<number | null>(null)
  const center = useRef({ x: 0, y: 0 })

  const update = (clientX: number, clientY: number) => {
    const radius = 44
    const dx = THREE.MathUtils.clamp(clientX - center.current.x, -radius, radius)
    const dy = THREE.MathUtils.clamp(clientY - center.current.y, -radius, radius)
    const length = Math.hypot(dx, dy)
    const scale = length > radius ? radius / length : 1
    const x = (dx * scale) / radius
    const y = (-dy * scale) / radius
    setKnob({ x: dx * scale, y: dy * scale })
    move.current = { x, y }
  }

  const reset = () => {
    active.current = null
    move.current = { x: 0, y: 0 }
    setKnob({ x: 0, y: 0 })
  }

  return (
    <div
      className="pointer-events-auto absolute bottom-5 left-4 z-30 h-32 w-32 touch-none select-none rounded-full border border-white/25 bg-slate-950/35 backdrop-blur active:bg-slate-950/50"
      onPointerDown={(event) => {
        if (active.current !== null) return
        active.current = event.pointerId
        const rect = event.currentTarget.getBoundingClientRect()
        center.current = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
        event.currentTarget.setPointerCapture(event.pointerId)
        update(event.clientX, event.clientY)
        event.preventDefault()
      }}
      onPointerMove={(event) => {
        if (active.current !== event.pointerId) return
        update(event.clientX, event.clientY)
        event.preventDefault()
      }}
      onPointerUp={(event) => {
        if (active.current === event.pointerId) reset()
      }}
      onPointerCancel={reset}
      aria-label="移动摇杆"
    >
      <div className="absolute inset-3 rounded-full border border-white/10" />
      <div
        className="absolute left-1/2 top-1/2 h-12 w-12 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/35 bg-white/20 shadow-lg"
        style={{ transform: `translate(calc(-50% + ${knob.x}px), calc(-50% + ${knob.y}px))` }}
      />
      <span className="absolute inset-x-0 bottom-2 text-center text-[9px] tracking-[0.18em] text-white/35">
        移动
      </span>
    </div>
  )
}

interface PropPosition {
  book: Vec3
  mug: Vec3
  cushion: Vec3
  notebook: Vec3
  plush: Vec3
}

const DEFAULT_PROPS: PropPosition = {
  book: [0.42, 0.05, -0.55],
  mug: [0.95, 0.11, -0.9],
  cushion: [-0.95, 0.16, 0.15],
  notebook: [-1.35, 0.06, -0.5],
  plush: [0.5, 0.18, 0.32],
}

const ANCHORS: Record<string, Vec3> = {
  center: [0, 0, 0],
  window: [0.2, 0, -4.05],
  bed: [2.8, 0, -3.55],
  desk: [3.45, 0, 3.25],
  shelf: [-3.95, 0, -3.6],
  cushion: [-1.8, 0, 1.0],
}

const DEFAULT_PROP_STATES: Record<string, string> = {
  book: 'closed',
  mug: 'full',
  cushion: 'normal',
  notebook: 'closed',
  plush: 'normal',
  room_light: 'on',
  curtain: 'open',
  window: 'closed',
  door: 'closed',
}
const DEFAULT_OUTFIT: SpaceOutfit = {
  top: 'wear',
  bottom: 'wear',
  shoes: 'wear',
  accessory: 'wear',
}
const OUTFIT_OPTIONS: Record<keyof SpaceOutfit, { id: string; label: string }[]> = {
  top: [{ id: 'wear', label: '穿上衣' }],
  bottom: [{ id: 'wear', label: '穿下装' }],
  shoes: [
    { id: 'wear', label: '穿鞋' },
    { id: 'none', label: '不穿鞋' },
  ],
  accessory: [
    { id: 'wear', label: '戴配饰' },
    { id: 'none', label: '不戴配饰' },
  ],
}
const EMOTION_STYLES: Record<
  string,
  { expression: string; clip: string; holdClip: string; duration: number }
> = {
  happy: {
    expression: 'happy',
    clip: 'Dance_Loop',
    holdClip: 'Idle_Talking_Loop',
    duration: 2600,
  },
  excited: {
    expression: 'excited',
    clip: 'Jump_Loop',
    holdClip: 'Idle_Talking_Loop',
    duration: 1800,
  },
  shy: {
    expression: 'shy',
    clip: 'Interact',
    holdClip: 'Idle_Talking_Loop',
    duration: 2800,
  },
  curious: {
    expression: 'curious',
    clip: 'Interact',
    holdClip: 'Idle_Talking_Loop',
    duration: 2600,
  },
  surprised: {
    expression: 'surprised',
    clip: 'Jump_Start',
    holdClip: 'Idle_Talking_Loop',
    duration: 1400,
  },
  sad: {
    expression: 'sad',
    clip: 'Crouch_Fwd_Loop',
    holdClip: 'Crouch_Idle_Loop',
    duration: 3600,
  },
  angry: {
    expression: 'angry',
    clip: 'Punch_Cross',
    holdClip: 'Idle_Talking_Loop',
    duration: 1600,
  },
  tired: {
    expression: 'tired',
    clip: 'Crouch_Idle_Loop',
    holdClip: 'Crouch_Idle_Loop',
    duration: 4200,
  },
  calm: {
    expression: 'calm',
    clip: 'Idle_Loop',
    holdClip: 'Idle_Loop',
    duration: 3000,
  },
}

const EXPRESSION_FALLBACK: Record<string, string> = {
  excited: 'happy',
  shy: 'relaxed',
  curious: 'relaxed',
  tired: 'relaxed',
  calm: 'relaxed',
}

const EMOTION_POSES: Record<
  string,
  {
    head?: Vec3
    spine?: Vec3
    leftArm?: Vec3
    rightArm?: Vec3
  }
> = {
  happy: { head: [-0.04, 0, 0.04], leftArm: [0, 0, -0.18], rightArm: [0, 0, 0.18] },
  excited: { head: [-0.08, 0, 0], leftArm: [0, 0, -0.25], rightArm: [0, 0, 0.25] },
  shy: { head: [0.18, -0.12, 0.06], spine: [0.03, 0, 0], leftArm: [0.1, 0.05, 0.42], rightArm: [0.1, -0.05, -0.42] },
  curious: { head: [-0.08, 0.18, 0.08], spine: [-0.04, 0, 0], leftArm: [0, 0, -0.06], rightArm: [0.12, 0, 0.1] },
  surprised: { head: [-0.14, 0, 0], spine: [0.07, 0, 0], leftArm: [0, 0, -0.28], rightArm: [0, 0, 0.28] },
  sad: { head: [0.22, 0, -0.04], spine: [0.08, 0, 0], leftArm: [0.08, 0, 0.1], rightArm: [0.08, 0, -0.1] },
  angry: { head: [-0.08, 0, 0], spine: [-0.05, 0, 0], leftArm: [0.24, 0, 0.2], rightArm: [0.24, 0, -0.2] },
  tired: { head: [0.20, 0, 0.08], spine: [0.09, 0, 0], leftArm: [0, 0, 0.04], rightArm: [0, 0, -0.04] },
  calm: { head: [0.02, 0, 0], spine: [0, 0, 0], leftArm: [0, 0, -0.02], rightArm: [0, 0, 0.02] },
}

type OutfitMesh = {
  mesh: THREE.Mesh
  material: THREE.Material
  slot: 'top' | 'bottom' | 'shoes' | 'outfit'
}

function collectOutfitMeshes(vrm: VRM): OutfitMesh[] {
  const found: OutfitMesh[] = []
  vrm.scene.traverse((object) => {
    const mesh = object as THREE.Mesh
    if (!mesh.isMesh) return
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
    for (const material of materials) {
      if (!material) continue
      const name = `${mesh.name} ${mesh.parent?.name || ''} ${material.name || ''}`.toLowerCase()
      // 2026-11-07：不再靠改材质颜色冒充换装；按模型里真实衣服网格做显隐。
      // D469：小世界只按正常衣物槽位显隐。
      const slot = /shoe|boot|sneaker|socks/.test(name)
        ? 'shoes'
        : /_bottoms_|bottoms|skirt|pants|shorts|trousers|garter|under_wear|himohodoki/.test(name)
          ? 'bottom'
          : /_tops_|tops|shirt|blouse|hoodie|jacket|tee|coat|breast|chest_ribon|arm/.test(name)
            ? 'top'
            : /fish2_outfit|onepiece|dress|outfit|maid|frill|plane|cube|kemomimi|head_dress/.test(name)
              ? 'outfit'
              : null
      if (slot) found.push({ mesh, material, slot })
    }
  })
  return found
}

const FIXTURE_KEYS = new Set(['room_light', 'curtain', 'window', 'door'])
const FIXTURE_LABELS: Record<string, string> = {
  room_light: '房间灯',
  curtain: '窗帘',
  window: '窗户',
  door: '门',
}

const ACTION_IDLE: Record<string, SpaceIdleKind> = {
  book: 'read',
  notebook: 'read',
  phone: 'phone',
  plush: 'tail',
  mug: 'stretch',
  window: 'window',
}

const ANCHOR_LABELS: Record<string, string> = {
  center: '房间中间',
  window: '窗边',
  bed: '床边',
  desk: '桌边',
  shelf: '书架旁',
  cushion: '靠垫旁',
}

const OBJECT_LABELS: Record<string, string> = {
  book: '书',
  mug: '杯子',
  cushion: '靠垫',
  notebook: '笔记本',
  plush: '鲸鱼玩偶',
}

const OBJECT_GRAB_RADII: Record<SpaceObjectKey, number> = {
  book: 0.34,
  mug: 0.28,
  cushion: 0.48,
  notebook: 0.3,
  plush: 0.38,
}

/**
 * 统一的 VRM 基础姿态修正：模型静止/T-pose 时，上臂通常横着。
 * 这里不依赖任何具体模型，只按“上臂世界方向”判断：如果接近水平，
 * 就把上臂瞄准身体侧下方，避免默认模型经常摆出莫名其妙的 T-pose。
 */
function aimArmToward(
  bone: THREE.Object3D | null,
  child: THREE.Object3D | null,
  root: THREE.Object3D | null,
  targetLocal: THREE.Vector3,
  onlyIfHorizontal = false,
): void {
  if (!bone || !child || !root) return
  bone.updateWorldMatrix(true, false)
  child.updateWorldMatrix(true, false)
  const bonePos = new THREE.Vector3()
  const childPos = new THREE.Vector3()
  bone.getWorldPosition(bonePos)
  child.getWorldPosition(childPos)
  const current = childPos.sub(bonePos)
  if (current.lengthSq() < 1e-6) return
  current.normalize()
  if (onlyIfHorizontal && Math.abs(current.y) > 0.35) return
  const rootQ = new THREE.Quaternion()
  root.getWorldQuaternion(rootQ)
  const target = targetLocal.clone().normalize().applyQuaternion(rootQ)
  const delta = new THREE.Quaternion().setFromUnitVectors(current, target)
  const parentQ = new THREE.Quaternion()
  bone.parent?.getWorldQuaternion(parentQ)
  const worldQ = new THREE.Quaternion()
  bone.getWorldQuaternion(worldQ)
  worldQ.premultiply(delta)
  bone.quaternion.copy(parentQ.invert().multiply(worldQ).normalize())
  bone.updateWorldMatrix(false, true)
}

const WORLD_SPAWNS: Vec3[] = [
  [-2.6, 0, -1.4],
  [2.6, 0, -1.6],
  [-2.1, 0, 2.1],
  [2.4, 0, 2.3],
  [0, 0, -3.2],
  [-3.8, 0, 0.4],
  [3.8, 0, 0.4],
  [0, 0, 3.7],
]

function idleLabel(kind: SpaceIdleKind): string {
  return {
    window: '看窗外',
    read: '看书',
    tail: '看尾巴',
    phone: '刷手机',
    stretch: '伸懒腰',
  }[kind]
}

function describeAction(action: SpaceAction): string {
  const target = action.target ?? ''
  if (action.kind === 'go_to') return `她走到了${ANCHOR_LABELS[target] ?? '房间另一边'}`
  if (action.kind === 'sit') return `她在${ANCHOR_LABELS[target] ?? '旁边'}坐下了`
  if (action.kind === 'lie') return `她在${ANCHOR_LABELS[target] ?? '那里'}躺下了`
  if (action.kind === 'approach') return `她凑近了${OBJECT_LABELS[target] ?? '那样东西'}`
  if (action.kind === 'use') return `她在用${OBJECT_LABELS[target] ?? '房间里的东西'}`
  if (action.kind === 'arrange') return `她在整理${OBJECT_LABELS[target] ?? '房间'}`
  if (action.kind === 'set_state') {
    return `她在摆弄${OBJECT_LABELS[target] ?? FIXTURE_LABELS[target] ?? '房间里的东西'}`
  }
  if (action.kind === 'pick_up') return `她拿起了${OBJECT_LABELS[target] ?? '房间里的东西'}`
  if (action.kind === 'put_down') return `她放下了${OBJECT_LABELS[target] ?? '手里的东西'}`
  if (action.kind === 'push') return `她推了推${OBJECT_LABELS[target] ?? '房间里的东西'}`
  if (action.kind === 'idle') return `她在${ACTION_IDLE[target] ? idleLabel(ACTION_IDLE[target]) : '发呆'}`
  return '她在发呆'
}

const IDLE_TARGETS: Record<SpaceIdleKind, Vec3> = {
  window: [1.45, 1.55, -1.4],
  read: [0.08, 0.5, 0.48],
  tail: [0.3, 0.55, -0.12],
  phone: [0.14, 0.5, 0.58],
  stretch: [0.0, 1.92, 0.12],
}

const IDLE_BODY_POSES: Record<SpaceIdleKind, { position: Vec3; yaw: number }> = {
  window: { position: [0.14, 0, -0.12], yaw: -0.28 },
  read: { position: [0.02, 0, 0.02], yaw: 0.04 },
  tail: { position: [0.06, 0, 0], yaw: 0.16 },
  phone: { position: [0.02, 0, 0.04], yaw: 0.07 },
  stretch: { position: [0, 0, 0], yaw: 0 },
}

const FALLBACK_PROFILE: SpaceProfile = {
  version: 3,
  attention: { curiosity: 0.55, patience: 0.55, initiative: 0.45 },
  idle: [
    { kind: 'window', text: '趴在窗边看外面的光' },
    { kind: 'read', text: '翻一本旧书，看到一半又走神了' },
    { kind: 'tail', text: '低头看自己的尾巴' },
    { kind: 'phone', text: '拿着手机慢慢刷，偶尔笑一下' },
    { kind: 'stretch', text: '伸了个懒腰，活动了一下肩膀' },
  ],
  notice: {
    lines: ['嗯？你什么时候来的。', '……我感觉到你在看我了。'],
    expression: 'relaxed',
  },
  call: {
    lines: ['我在。怎么啦？', '嗯，我听着呢。'],
    expression: 'happy',
  },
  zones: {
    head: { lines: ['唔……头发要乱了。', '别揉啦，我又不是小孩子。'], expression: 'happy' },
    ear: { lines: ['耳朵不能乱碰啦。', '你手怎么这么欠呀。'], expression: 'surprised' },
    tail: { lines: ['尾巴不是给你拽的！', '别、别碰那里……'], expression: 'surprised' },
    hand: { lines: ['手先给我，别乱摸。', '你抓这么紧干嘛。'], expression: 'relaxed' },
    body: { lines: ['……你站这么近干嘛。', '嗯？我在呢。'], expression: 'relaxed' },
  },
  objects: {
    book: { lines: ['这本我还没看完呢。', '书签别给我弄掉了。'], expression: 'relaxed' },
    mug: { lines: ['杯子里还有水，小心一点。', '你要喝一口吗？'], expression: 'relaxed' },
    cushion: { lines: ['靠垫要放回原位啦。', '你要坐这边？'], expression: 'happy' },
    notebook: { lines: ['那本是我记东西的。', '别乱翻我的笔记。'], expression: 'surprised' },
    plush: { lines: ['鲸鱼玩偶不能抢。', '它比你软多了。'], expression: 'happy' },
  },
}

const STORAGE_PREFIX = 'yf.space.layout.v5.'
const PROFILE_STORAGE_PREFIX = 'yf.space.profile.v3.'

function readLayout(friendId: string): PropPosition {
  try {
    const raw = localStorage.getItem(`${STORAGE_PREFIX}${friendId}`)
    if (!raw) return DEFAULT_PROPS
    const parsed = JSON.parse(raw) as Partial<PropPosition>
    return {
      book: parsed.book ?? DEFAULT_PROPS.book,
      mug: parsed.mug ?? DEFAULT_PROPS.mug,
      cushion: parsed.cushion ?? DEFAULT_PROPS.cushion,
      notebook: parsed.notebook ?? DEFAULT_PROPS.notebook,
      plush: parsed.plush ?? DEFAULT_PROPS.plush,
    }
  } catch {
    return DEFAULT_PROPS
  }
}

function mergeServerLayout(layout: SpaceState['layout']): PropPosition {
  const result = { ...DEFAULT_PROPS }
  for (const key of Object.keys(DEFAULT_PROPS) as (keyof PropPosition)[]) {
    const value = layout?.[key]
    if (
      Array.isArray(value) &&
      value.length === 3 &&
      value.every((item) => typeof item === 'number' && Number.isFinite(item))
    ) {
      result[key] = [value[0], value[1], value[2]]
    }
  }
  return result
}

function samePosition(a: Vec3, b: Vec3): boolean {
  return Math.abs(a[0] - b[0]) < 0.0001 && Math.abs(a[1] - b[1]) < 0.0001 && Math.abs(a[2] - b[2]) < 0.0001
}

function makeSkyTexture(): THREE.CanvasTexture {
  const canvas = document.createElement('canvas')
  canvas.width = 512
  canvas.height = 320
  const ctx = canvas.getContext('2d')
  if (ctx) {
    const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height)
    gradient.addColorStop(0, '#8bbbd2')
    gradient.addColorStop(0.55, '#c5dce5')
    gradient.addColorStop(1, '#f1d7b6')
    ctx.fillStyle = gradient
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = 'rgba(255,255,255,0.45)'
    ctx.beginPath()
    ctx.ellipse(120, 92, 72, 22, 0, 0, Math.PI * 2)
    ctx.ellipse(188, 82, 52, 20, 0, 0, Math.PI * 2)
    ctx.fill()
  }
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  return texture
}

/**
 * 阴影贴图不需要每帧重画：房间和灯都不动，只有角色在动。
 * 每 3 帧更新一次（60fps 下约 20fps 阴影），既保留投影又明显降低
 * 低配 GPU / WebView2 上“看向地面就掉帧”的概率。
 */
function ShadowBudget() {
  const gl = useThree((state) => state.gl)
  const frames = useRef(0)

  useEffect(() => {
    gl.shadowMap.autoUpdate = false
    gl.shadowMap.needsUpdate = true
    return () => {
      gl.shadowMap.autoUpdate = true
    }
  }, [gl])

  useFrame((_, delta) => {
    frames.current += 1
    // 已经掉帧时反而每帧更新，避免阴影肉眼可见地跳。
    const interval = delta > 0.034 ? 1 : 3
    if (frames.current % interval === 0) gl.shadowMap.needsUpdate = true
  })

  return null
}

/** 给无头验证 / 用户反馈留一个真实帧率探针。 */
function FrameBudgetProbe() {
  const frames = useRef(0)
  const since = useRef(performance.now())

  useFrame(() => {
    frames.current += 1
    const now = performance.now()
    if (now - since.current < 1000) return
    const fps = Math.round((frames.current * 1000) / (now - since.current))
    updateSpaceDebug({
      fps,
      frameMs: Math.round((1000 / Math.max(1, fps)) * 10) / 10,
    })
    frames.current = 0
    since.current = now
  })

  return null
}

/**
 * 通用动作平滑度检查：统计相机 / 手 / 角色每秒的方向反转次数。
 * 正常抚摸的来回是低频的（一两秒一次）；如果一分钟内数字突然飙高，
 * 基本就是输入噪声、两个位置源打架或物理回弹在抖。
 */
function MotionSmoothnessProbe({ viewer }: { viewer: MutableRefObject<ViewerSnapshot> }) {
  const previous = useRef<Record<string, THREE.Vector3>>({})
  const previousDelta = useRef<Record<string, THREE.Vector3>>({})
  const reversals = useRef({ camera: 0, hand: 0, avatar: 0 })
  const windowStarted = useRef(performance.now())

  useFrame(() => {
    const debugAvatar = window.__YF_SPACE_DEBUG__?.avatar
    const points: Record<string, Vec3 | null> = {
      camera: viewer.current.camera,
      hand: viewer.current.hand,
      avatar:
        Array.isArray(debugAvatar) && debugAvatar.length === 3
          ? (debugAvatar as Vec3)
          : null,
    }
    for (const [key, value] of Object.entries(points)) {
      if (!value) continue
      const current = new THREE.Vector3(value[0], value[1], value[2])
      const last = previous.current[key]
      if (last) {
        const delta = current.clone().sub(last)
        if (delta.lengthSq() > 1e-10) {
          const lastDelta = previousDelta.current[key]
          if (
            lastDelta &&
            lastDelta.dot(delta) < 0 &&
            delta.length() > 0.00035
          ) {
            reversals.current[key as keyof typeof reversals.current] += 1
          }
          previousDelta.current[key] = delta
        }
      }
      previous.current[key] = current
    }
    const now = performance.now()
    if (now - windowStarted.current < 1000) return
    const snapshot = { ...reversals.current }
    updateSpaceDebug({
      motionReversals: snapshot,
      motionJitter:
        snapshot.camera > 6 || snapshot.hand > 10 || snapshot.avatar > 10,
    })
    reversals.current = { camera: 0, hand: 0, avatar: 0 }
    windowStarted.current = now
  })

  return null
}

const Room = memo(function Room({ fixtures }: { fixtures: Record<string, string> }) {
  const sky = useMemo(makeSkyTexture, [])
  useEffect(() => () => sky.dispose(), [sky])
  const lightFactor = fixtures.room_light === 'off' ? 0.18 : 1
  const curtainClosed = fixtures.curtain === 'closed'

  return (
    <>
      <color attach="background" args={['#111923']} />
      <fog attach="fog" args={['#18232d', 8, 18]} />
      <ambientLight intensity={0.75 * lightFactor} color="#ffe1bd" />
      <directionalLight
        position={[4.2, 6.0, 3.8]}
        intensity={2.1 * lightFactor}
        color="#ffe4c2"
        castShadow
        shadow-mapSize-width={512}
        shadow-mapSize-height={512}
        shadow-camera-left={-5.4}
        shadow-camera-right={5.4}
        shadow-camera-top={5.2}
        shadow-camera-bottom={-5.4}
        shadow-camera-near={0.5}
        shadow-camera-far={18}
        shadow-bias={-0.00035}
        shadow-normalBias={0.025}
      />
      <pointLight
        position={[0.2, 1.5, -4.0]}
        intensity={2.4 * lightFactor}
        color="#9edbff"
        distance={8}
      />
      <pointLight
        position={[-3.4, 2.1, 2.9]}
        intensity={1.0 * lightFactor}
        color="#ffb98f"
        distance={7}
      />
      <pointLight
        position={[0, 3.2, 0.35]}
        intensity={1.25 * lightFactor}
        color="#ffe3bd"
        distance={9}
      />
      <pointLight
        position={[3.5, 2.2, 3.7]}
        intensity={1.55 * lightFactor}
        color="#ffd2a0"
        distance={7}
      />

      <mesh receiveShadow position={[0, -0.06, 0]}>
        <boxGeometry args={[10.2, 0.12, 10.2]} />
        <meshStandardMaterial color="#9a7458" roughness={0.85} />
      </mesh>
      <mesh position={[0, 1.8, -5.05]} receiveShadow>
        <boxGeometry args={[10.2, 3.6, 0.12]} />
        <meshStandardMaterial color="#445760" roughness={0.92} />
      </mesh>
      <mesh position={[-5.05, 1.8, 0]}>
        <boxGeometry args={[0.12, 3.6, 10.2]} />
        <meshStandardMaterial color="#374b55" roughness={0.92} />
      </mesh>
      <mesh position={[5.05, 1.8, 0]}>
        <boxGeometry args={[0.12, 3.6, 10.2]} />
        <meshStandardMaterial color="#374b55" roughness={0.92} />
      </mesh>
      <mesh position={[0, 1.8, 5.05]} receiveShadow>
        <boxGeometry args={[10.2, 3.6, 0.12]} />
        <meshStandardMaterial color="#526872" roughness={0.92} />
      </mesh>
      <mesh position={[0, 0.15, 4.975]}>
        <boxGeometry args={[9.9, 0.3, 0.045]} />
        <meshStandardMaterial color="#d0ab7a" roughness={0.7} />
      </mesh>
      <mesh position={[0, 3.6, 0]} receiveShadow>
        <boxGeometry args={[10.2, 0.12, 10.2]} />
        <meshStandardMaterial color="#2e3d45" roughness={0.96} />
      </mesh>
      <group position={[0, 3.51, 0.2]}>
        <mesh position={[0, 0.055, 0]}>
          <cylinderGeometry args={[0.22, 0.28, 0.08, 24]} />
          <meshStandardMaterial color="#d4b88d" roughness={0.55} />
        </mesh>
        <mesh position={[0, -0.01, 0]}>
          <sphereGeometry args={[0.14, 18, 12]} />
          <meshStandardMaterial
            color="#ffe1ae"
            emissive="#ffd28a"
            emissiveIntensity={1.4}
            roughness={0.3}
          />
        </mesh>
      </group>

      {/* 地毯：刻意做成暖色长方形，而不是深色圆盘。
          旧圆盘在低角度下很像一团贴地阴影，用户会误以为是投影或渲染 bug。 */}
      <mesh position={[0, 0.006, 0.35]} receiveShadow>
        <boxGeometry args={[2.75, 0.012, 1.95]} />
        <meshStandardMaterial color="#b8845d" roughness={0.98} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.0145, 0.35]} receiveShadow>
        <planeGeometry args={[2.48, 1.68]} />
        <meshStandardMaterial color="#c9a077" roughness={1} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.016, 0.35]}>
        <planeGeometry args={[2.12, 1.32]} />
        <meshStandardMaterial color="#b07b55" roughness={1} />
      </mesh>

      {/* 前墙：相机绕到角色背后时仍然看到房间，而不是墙外虚空。 */}
      <group position={[-1.4, 0, 4.97]}>
        <mesh position={[0, 1.15, 0]} castShadow receiveShadow>
          <boxGeometry args={[0.94, 2.3, 0.06]} />
          <meshStandardMaterial color="#6a4937" roughness={0.74} />
        </mesh>
        <mesh position={[-0.5, 1.15, 0.01]}>
          <boxGeometry args={[0.08, 2.42, 0.08]} />
          <meshStandardMaterial color="#d0ab7a" roughness={0.66} />
        </mesh>
        <mesh position={[0.5, 1.15, 0.01]}>
          <boxGeometry args={[0.08, 2.42, 0.08]} />
          <meshStandardMaterial color="#d0ab7a" roughness={0.66} />
        </mesh>
        <mesh position={[0, 2.34, 0.01]}>
          <boxGeometry args={[1.08, 0.08, 0.08]} />
          <meshStandardMaterial color="#d0ab7a" roughness={0.66} />
        </mesh>
        {[0.38, -0.18].map((y) => (
          <mesh key={y} position={[0.25, y, 0.05]}>
            <boxGeometry args={[0.48, 0.035, 0.025]} />
            <meshStandardMaterial color="#8e654c" roughness={0.8} />
          </mesh>
        ))}
        <mesh position={[0.31, 0.08, 0.08]}>
          <sphereGeometry args={[0.045, 12, 10]} />
          <meshStandardMaterial color="#d8b26e" metalness={0.3} roughness={0.35} />
        </mesh>
      </group>

      <group position={[1.6, 0, 4.96]}>
        <mesh position={[0, 1.76, 0]} castShadow>
          <boxGeometry args={[1.04, 0.74, 0.06]} />
          <meshStandardMaterial color="#caa879" roughness={0.7} />
        </mesh>
        <mesh position={[0, 1.76, -0.041]} rotation={[0, Math.PI, 0]}>
          <planeGeometry args={[0.86, 0.56]} />
          <meshBasicMaterial color="#7f9aa5" toneMapped={false} />
        </mesh>
        <mesh position={[0.02, 1.73, -0.052]} rotation={[0, Math.PI, 0]}>
          <circleGeometry args={[0.16, 24]} />
          <meshBasicMaterial color="#e7c98e" toneMapped={false} />
        </mesh>
      </group>

      <group position={[3.45, 0, 4.22]}>
        <mesh position={[0, 0.7, 0]} castShadow receiveShadow>
          <boxGeometry args={[1.15, 0.09, 0.42]} />
          <meshStandardMaterial color="#815e46" roughness={0.72} />
        </mesh>
        {[-0.45, 0.45].map((x) => (
          <mesh key={x} position={[x, 0.35, 0]} castShadow>
            <boxGeometry args={[0.07, 0.7, 0.07]} />
            <meshStandardMaterial color="#644834" roughness={0.8} />
          </mesh>
        ))}
        <mesh position={[-0.25, 0.89, 0]} castShadow>
          <cylinderGeometry args={[0.11, 0.14, 0.28, 16]} />
          <meshStandardMaterial color="#b7c3ac" roughness={0.72} />
        </mesh>
        {[0, 1, 2].map((i) => (
          <mesh
            key={i}
            position={[-0.25 + (i - 1) * 0.06, 1.14 + (i % 2) * 0.08, 0]}
            rotation={[0, 0, (i - 1) * 0.3]}
            castShadow
          >
            <coneGeometry args={[0.045, 0.24, 7]} />
            <meshStandardMaterial color="#547565" roughness={0.9} />
          </mesh>
        ))}
      </group>

      <group position={[0.2, 1.9, -4.94]}>
        <mesh>
          <planeGeometry args={[1.76, 1.3]} />
          <meshBasicMaterial map={sky} toneMapped={false} />
        </mesh>
        <mesh position={[0, 0.7, 0.02]}>
          <boxGeometry args={[1.9, 0.08, 0.1]} />
          <meshStandardMaterial color="#d2b58f" />
        </mesh>
        <mesh position={[0, -0.7, 0.02]}>
          <boxGeometry args={[1.9, 0.08, 0.1]} />
          <meshStandardMaterial color="#d2b58f" />
        </mesh>
        <mesh position={[-0.91, 0, 0.02]}>
          <boxGeometry args={[0.08, 1.45, 0.1]} />
          <meshStandardMaterial color="#d2b58f" />
        </mesh>
        <mesh position={[0.91, 0, 0.02]}>
          <boxGeometry args={[0.08, 1.45, 0.1]} />
          <meshStandardMaterial color="#d2b58f" />
        </mesh>
        <mesh position={[0, 0, 0.015]}>
          <boxGeometry args={[0.05, 1.3, 0.05]} />
          <meshStandardMaterial color="#d2b58f" />
        </mesh>
        <mesh position={[0, 0, 0.015]}>
          <boxGeometry args={[1.76, 0.045, 0.05]} />
          <meshStandardMaterial color="#d2b58f" />
        </mesh>
      </group>

      <mesh position={[2.8, 0.28, -4.3]} castShadow receiveShadow>
        <boxGeometry args={[1.55, 0.12, 0.72]} />
        <meshStandardMaterial color="#73533e" roughness={0.7} />
      </mesh>
      <mesh position={[2.18, 0.16, -4.3]} castShadow>
        <boxGeometry args={[0.12, 0.5, 0.12]} />
        <meshStandardMaterial color="#5b4032" />
      </mesh>
      <mesh position={[3.42, 0.16, -4.3]} castShadow>
        <boxGeometry args={[0.12, 0.5, 0.12]} />
        <meshStandardMaterial color="#5b4032" />
      </mesh>

      <group position={[-4.0, 1.25, -4.1]}>
        <mesh castShadow receiveShadow>
          <boxGeometry args={[0.58, 2.1, 0.38]} />
          <meshStandardMaterial color="#6d4d3c" roughness={0.7} />
        </mesh>
        {[0.46, 0.06, -0.34, -0.72].map((y) => (
          <mesh key={y} position={[0, y, 0.21]}>
            <boxGeometry args={[0.5, 0.035, 0.03]} />
            <meshStandardMaterial color="#d1a77a" />
          </mesh>
        ))}
        {[-0.18, 0.04, 0.2].map((x, i) => (
          <mesh key={x} position={[x, 0.3 + i * 0.18, 0.18]} rotation={[0, 0, i * 0.12]}>
            <boxGeometry args={[0.07, 0.24, 0.04]} />
            <meshStandardMaterial color={['#b56d57', '#7d9a91', '#d3a45f'][i]} />
          </mesh>
        ))}
      </group>

      <group position={[4.2, 0.45, -3.8]}>
        <mesh castShadow position={[0, 0.08, 0]}>
          <cylinderGeometry args={[0.2, 0.26, 0.16, 16]} />
          <meshStandardMaterial color="#8d6a4c" />
        </mesh>
        {[0, 1, 2, 3, 4].map((i) => (
          <mesh
            key={i}
            castShadow
            position={[Math.cos(i * 1.9) * 0.13, 0.3 + (i % 2) * 0.12, Math.sin(i * 1.9) * 0.1]}
            rotation={[0, 0, (i - 2) * 0.35]}
          >
            <coneGeometry args={[0.12, 0.42, 8]} />
            <meshStandardMaterial color="#4f806e" roughness={0.9} />
          </mesh>
        ))}
      </group>

      <mesh position={[1.4, 1.22, -4.9]}>
        <boxGeometry args={[0.16, 0.3, 0.12]} />
        <meshStandardMaterial color="#e7bd74" emissive="#f6c578" emissiveIntensity={0.6} />
      </mesh>
      <mesh position={[1.4, 1.38, -4.9]}>
        <boxGeometry args={[0.04, 0.04, 0.04]} />
        <meshStandardMaterial color="#7c5b42" />
      </mesh>

      {/* 墙裙、顶角线和天花横梁：大房间不能只剩一个空盒子。 */}
      {[
        { position: [0, 0.2, -4.97] as Vec3, size: [10.0, 0.4, 0.06] as Vec3 },
        { position: [0, 0.2, 4.97] as Vec3, size: [10.0, 0.4, 0.06] as Vec3 },
        { position: [-4.97, 0.2, 0] as Vec3, size: [0.06, 0.4, 10.0] as Vec3 },
        { position: [4.97, 0.2, 0] as Vec3, size: [0.06, 0.4, 10.0] as Vec3 },
      ].map((item, index) => (
        <mesh key={`baseboard-${index}`} position={item.position}>
          <boxGeometry args={item.size} />
          <meshStandardMaterial color="#c9a777" roughness={0.72} />
        </mesh>
      ))}
      {[
        { position: [0, 3.4, -4.96] as Vec3, size: [10.0, 0.14, 0.08] as Vec3 },
        { position: [0, 3.4, 4.96] as Vec3, size: [10.0, 0.14, 0.08] as Vec3 },
        { position: [-4.96, 3.4, 0] as Vec3, size: [0.08, 0.14, 10.0] as Vec3 },
        { position: [4.96, 3.4, 0] as Vec3, size: [0.08, 0.14, 10.0] as Vec3 },
      ].map((item, index) => (
        <mesh key={`crown-${index}`} position={item.position}>
          <boxGeometry args={item.size} />
          <meshStandardMaterial color="#6b5141" roughness={0.75} />
        </mesh>
      ))}
      {[-3.35, 0, 3.35].map((z) => (
        <mesh key={`beam-${z}`} position={[0, 3.47, z]} castShadow>
          <boxGeometry args={[10.1, 0.16, 0.2]} />
          <meshStandardMaterial color="#4d3b31" roughness={0.82} />
        </mesh>
      ))}
      {[-4.93, 4.93].map((x) =>
        [-3.4, -1.2, 1.2, 3.4].map((z) => (
          <mesh key={`panel-${x}-${z}`} position={[x, 1.82, z]}>
            <boxGeometry args={[0.035, 2.55, 0.1]} />
            <meshStandardMaterial color="#526b73" roughness={0.9} />
          </mesh>
        )),
      )}

      {/* 窗边的帘子和帘头，让大窗不是一张贴图浮在墙上。 */}
      <mesh position={[-0.92, 1.9, -4.87]} castShadow>
        <boxGeometry args={[0.34, 2.3, 0.07]} />
        <meshStandardMaterial color="#b49a82" roughness={0.98} />
      </mesh>
      <mesh position={[1.32, 1.9, -4.87]} castShadow>
        <boxGeometry args={[0.34, 2.3, 0.07]} />
        <meshStandardMaterial color="#b49a82" roughness={0.98} />
      </mesh>
      <mesh position={[0.2, 2.91, -4.84]} castShadow>
        <boxGeometry args={[2.62, 0.16, 0.12]} />
        <meshStandardMaterial color="#8f7057" roughness={0.85} />
      </mesh>
      {curtainClosed && (
        <mesh position={[0.2, 1.9, -4.85]} castShadow>
          <boxGeometry args={[2.45, 2.28, 0.055]} />
          <meshStandardMaterial color="#9e8169" roughness={0.98} />
        </mesh>
      )}
    </>
  )
})

function WhaleTail({
  reaction,
  idleKind,
  onClick,
}: {
  reaction: Reaction | null
  idleKind: SpaceIdleKind
  onClick: () => void
}) {
  const roots = useRef<(THREE.Group | null)[]>([])
  const pitch = useRef<SpringState[]>([])
  const roll = useRef<SpringState[]>([])
  const yaw = useRef<SpringState[]>([])
  useFrame((state, delta) => {
    const t = state.clock.elapsedTime
    const excited = reaction === 'tail'
    const inspecting = idleKind === 'tail' && !reaction
    roots.current.forEach((node, index) => {
      if (!node) return
      if (!pitch.current[index]) {
        pitch.current[index] = { value: 0.13, velocity: 0 }
        roll.current[index] = { value: 0, velocity: 0 }
        yaw.current[index] = { value: 0, velocity: 0 }
      }
      const targetX =
        0.13 +
        Math.sin(t * 1.35 + index * 0.48) * 0.045 +
        (inspecting ? Math.sin(t * 0.8 + index * 0.45) * 0.075 : 0) +
        (excited ? Math.sin(t * 11 + index * 0.7) * 0.12 / (index * 0.4 + 1) : 0)
      const targetZ =
        Math.sin(t * 1.1 + index * 0.42) * 0.035 +
        (excited ? 0.16 / (index * 0.4 + 1) : 0) +
        (inspecting ? 0.08 / (index * 0.35 + 1) : 0)
      const targetY = Math.sin(t * 0.72 + index * 0.3) * 0.018
      pitch.current[index] = stepSpring(
        pitch.current[index],
        targetX,
        delta,
        32 + index * 3,
        6.4,
      )
      roll.current[index] = stepSpring(
        roll.current[index],
        targetZ,
        delta,
        28 + index * 2.5,
        5.8,
      )
      yaw.current[index] = stepSpring(
        yaw.current[index],
        targetY,
        delta,
        22 + index * 2,
        6.2,
      )
      node.rotation.x = pitch.current[index].value
      node.rotation.z = roll.current[index].value
      node.rotation.y = yaw.current[index].value
    })
  })

  return (
    <group position={[0.2, 0.88, -0.24]} rotation={[-0.54, 0, 0.42]} onClick={(event) => {
      event.stopPropagation()
      onClick()
    }}>
      <TailSegment index={0} refs={roots} onClick={onClick} />
    </group>
  )
}

function TailSegment({
  index,
  refs,
  onClick,
}: {
  index: number
  refs: MutableRefObject<(THREE.Group | null)[]>
  onClick: () => void
}) {
  return (
    <group
      ref={(node) => {
        refs.current[index] = node
      }}
      position={index === 0 ? [0, 0, 0] : [0, -0.088, -0.018]}
      rotation={[0.14, 0, 0]}
    >
      <mesh
        castShadow
        position={[0, -0.045, 0]}
        scale={[1.12, 1.08, 1.12]}
        onClick={(event) => {
          event.stopPropagation()
          onClick()
        }}
      >
        <sphereGeometry args={[0.125 - index * 0.009, 16, 12]} />
        <meshStandardMaterial
          color={index > 5 ? '#b7e4ec' : '#4c9eb8'}
          roughness={0.5}
          metalness={0.03}
          emissive={index > 5 ? '#345d68' : '#173a46'}
          emissiveIntensity={0.18}
        />
      </mesh>
      {index < 6 ? (
        <TailSegment index={index + 1} refs={refs} onClick={onClick} />
      ) : (
        <group position={[0, -0.11, 0]}>
          <mesh
            castShadow
            position={[-0.13, 0, 0]}
            rotation={[0, 0, Math.PI / 2]}
            scale={[0.2, 0.075, 0.05]}
            onClick={(event) => {
              event.stopPropagation()
              onClick()
            }}
          >
            <sphereGeometry args={[1, 16, 10]} />
            <meshStandardMaterial color="#9bd7e2" roughness={0.42} />
          </mesh>
          <mesh
            castShadow
            position={[0.13, 0, 0]}
            rotation={[0, 0, Math.PI / 2]}
            scale={[0.2, 0.075, 0.05]}
            onClick={(event) => {
              event.stopPropagation()
              onClick()
            }}
          >
            <sphereGeometry args={[1, 16, 10]} />
            <meshStandardMaterial color="#9bd7e2" roughness={0.42} />
          </mesh>
        </group>
      )}
    </group>
  )
}

function ActivityProp({
  kind,
  rightHand,
}: {
  kind: SpaceIdleKind
  rightHand: MutableRefObject<THREE.Object3D | null>
}) {
  const group = useRef<THREE.Group | null>(null)
  const baseY = kind === 'read' ? 0.77 : 0.79
  const handLocal = useMemo(() => new THREE.Vector3(), [])

  useFrame((state) => {
    if (!group.current) return
    const t = state.clock.elapsedTime
    if (rightHand.current && group.current.parent) {
      rightHand.current.getWorldPosition(handLocal)
      group.current.parent.worldToLocal(handLocal)
      group.current.position.set(
        handLocal.x + (kind === 'read' ? 0.12 : 0.04),
        handLocal.y + 0.1,
        handLocal.z + 0.13,
      )
    } else {
      group.current.position.y = baseY
    }
    group.current.position.y += Math.sin(t * 1.7) * 0.012
    group.current.rotation.z = Math.sin(t * 1.25) * 0.025
  })

  if (kind === 'read') {
    return (
      <group
        name="space-activity-read"
        ref={group}
        position={[0.12, 0.77, 0.34]}
        rotation={[-0.66, 0.06, -0.03]}
      >
        <RoundedBox args={[0.38, 0.035, 0.28]} radius={0.012} smoothness={3} castShadow>
          <meshStandardMaterial color="#8f454a" roughness={0.72} />
        </RoundedBox>
        {[-0.11, 0.11].map((x) => (
          <mesh key={x} position={[x * 0.78, 0.024, 0]}>
            <boxGeometry args={[0.16, 0.008, 0.24]} />
            <meshStandardMaterial color="#efe3cb" roughness={0.9} />
          </mesh>
        ))}
        <mesh position={[0, 0.03, 0]}>
          <boxGeometry args={[0.018, 0.012, 0.24]} />
          <meshStandardMaterial color="#b78e66" roughness={0.8} />
        </mesh>
      </group>
    )
  }

  if (kind === 'phone') {
    return (
      <group
        name="space-activity-phone"
        ref={group}
        position={[0.34, 0.79, 0.34]}
        rotation={[-0.42, -0.18, 0.04]}
      >
        <RoundedBox args={[0.15, 0.26, 0.025]} radius={0.018} smoothness={3} castShadow>
          <meshStandardMaterial color="#1f262b" roughness={0.38} metalness={0.22} />
        </RoundedBox>
        <mesh position={[0, 0.006, 0.016]}>
          <planeGeometry args={[0.125, 0.215]} />
          <meshBasicMaterial color="#a8d8de" toneMapped={false} />
        </mesh>
        <mesh position={[0, 0.085, 0.018]}>
          <planeGeometry args={[0.09, 0.024]} />
          <meshBasicMaterial color="#e8f5ec" toneMapped={false} />
        </mesh>
        <mesh position={[0, 0.02, 0.018]}>
          <planeGeometry args={[0.075, 0.018]} />
          <meshBasicMaterial color="#5f8790" toneMapped={false} />
        </mesh>
      </group>
    )
  }

  return null
}

const Avatar = memo(function Avatar({
  attention,
  idleTarget,
  idleKind,
  reaction,
  moodExpression,
  emotionName,
  locomotion,
  animationClip,
  outfit,
  modelUrl,
  worldState,
  propPositions,
  onReact,
  onAvatarGrabStart,
  onAvatarGrabEnd,
  onPostureReady,
  onMountComplete,
  viewer,
  selfState,
}: {
  attention: Attention
  idleTarget: Vec3
  idleKind: SpaceIdleKind
  reaction: Reaction | null
  moodExpression: string | null
  emotionName: string | null
  locomotion: Locomotion
  animationClip: string | null
  outfit: SpaceOutfit
  modelUrl?: string | null
  worldState: WorldState
  propPositions: PropPosition
  onReact: (reaction: Reaction) => void
  onAvatarGrabStart: (zone: Reaction) => void
  onAvatarGrabEnd: () => void
  onPostureReady: (kind: 'sit' | 'lie') => void
  onMountComplete: (kind: 'sit' | 'lie') => void
  viewer: MutableRefObject<ViewerSnapshot>
  selfState: MutableRefObject<SpaceSelfState>
}) {
  const { camera, gl } = useThree()
  const { world, rapier } = useRapier()
  const avatarBody = useRef<RapierRigidBody | null>(null)
  const avatarCollider = useRef<RapierCollider | null>(null)
  const avatarGrab = useRef<AvatarGrabState | null>(null)
  const releaseFall = useRef<{ vy: number } | null>(null)
  const colliderSensor = useRef(false)
  const previousPosture = useRef<Locomotion['posture']>('stand')
  const sensorGraceUntil = useRef(0)
  const character = useMemo(() => world.createCharacterController(0.025), [world])
  const steering = useRef(new THREE.Vector3())
  const lastBodyPosition = useRef(new THREE.Vector3())
  const stillSince = useRef(performance.now())
  // D424：所有朋友默认用 CC0 的 Vivi（AvatarSample_E）；个人上传的 VRM 可覆盖。
  const modelPath = modelUrl || '/models/Vivi.vrm'
  const gltf = useLoader(GLTFLoader, modelPath, (loader) => {
    loader.register((parser) => new VRMLoaderPlugin(parser))
  })
  const vrm = gltf.userData.vrm as VRM | undefined
  useEffect(() => {
    if (!vrm) return
    ;(window as any).__YF_SPACE_VRM__ = vrm
    // Tanorika/大肥鱼这套模型的 Side2 有一缕长发放到胸前时会穿出衣服，
    // 看起来像“胸口有个蓝色小东西”；Side/Back/Bang 仍保留，发型不缺。
    vrm.scene.traverse((object) => {
      const mesh = object as THREE.Mesh
      if (mesh.isMesh && mesh.name === 'Side2') mesh.visible = false
    })
    return () => {
      delete (window as any).__YF_SPACE_VRM__
    }
  }, [vrm])
  const animationGltf = useLoader(
    GLTFLoader,
    '/models/animations/AnimationLibrary_Godot_Standard.gltf',
  )
  // VRM0 的场景旋转必须在烘焙动画之前完成；否则烘焙时看到的静止骨架
  // 和运行时实际骨架相差 180°，旋转差量会把手臂翻到头顶。
  const preparedVrm = useMemo(() => {
    if (!vrm) return vrm
    if (!vrm.scene.userData.__yfVrm0Rotated) {
      if (vrm.meta?.metaVersion === '0') VRMUtils.rotateVRM0(vrm)
      vrm.scene.userData.__yfVrm0Rotated = true
    }
    // 模型朝向应在资产本身修正（大肥鱼 VRM 的根节点已转到 +Z），运行时不再
    // 按 hips 猜朝向 —— 那套猜测会把正常模型也转坏。
    // 这里只做一次“骨架兼容性”检查：hips 静止旋转明显不是单位旋转时，
    // 重定向动画很容易把头/四肢扭飞，统一走程序化姿态。
    if (!vrm.scene.userData.__yfSkeletonChecked) {
      const hipsBone = vrm.humanoid?.getRawBoneNode('hips' as VRMHumanBoneName)
      if (hipsBone) {
        const angle = 2 * Math.acos(Math.min(1, Math.abs(hipsBone.quaternion.w)))
        if (angle > Math.PI / 4) {
          vrm.scene.userData.__yfRetargetUnsafe = true
        }
      }
      vrm.scene.userData.__yfSkeletonChecked = true
    }
    return vrm
  }, [vrm])
  const clips = useMemo<Map<string, RetargetedClip>>(
    () =>
      preparedVrm && !preparedVrm.scene.userData.__yfRetargetUnsafe
        ? buildRetargetedClips(animationGltf.scene, animationGltf.animations, preparedVrm)
        : new Map(),
    [animationGltf, preparedVrm],
  )
  const root = useRef<THREE.Group | null>(null)
  const hips = useRef<THREE.Object3D | null>(null)
  const chest = useRef<THREE.Object3D | null>(null)
  const upperChest = useRef<THREE.Object3D | null>(null)
  const head = useRef<THREE.Object3D | null>(null)
  const neck = useRef<THREE.Object3D | null>(null)
  const spine = useRef<THREE.Object3D | null>(null)
  const rightHand = useRef<THREE.Object3D | null>(null)
  const leftHand = useRef<THREE.Object3D | null>(null)
  const leftUpperArm = useRef<THREE.Object3D | null>(null)
  const rightUpperArm = useRef<THREE.Object3D | null>(null)
  const leftLowerArm = useRef<THREE.Object3D | null>(null)
  const rightLowerArm = useRef<THREE.Object3D | null>(null)
  const leftUpperLeg = useRef<THREE.Object3D | null>(null)
  const rightUpperLeg = useRef<THREE.Object3D | null>(null)
  const leftLegRest = useRef<THREE.Quaternion | null>(null)
  const rightLegRest = useRef<THREE.Quaternion | null>(null)
  const restPoses = useRef<Map<THREE.Object3D, THREE.Quaternion>>(new Map())
  const proceduralPhase = useRef(0)
  const idleSeed = useRef(Math.random() * Math.PI * 2)
  const leftLowerLeg = useRef<THREE.Object3D | null>(null)
  const rightLowerLeg = useRef<THREE.Object3D | null>(null)
  const leftFoot = useRef<THREE.Object3D | null>(null)
  const rightFoot = useRef<THREE.Object3D | null>(null)
  const movingRef = useRef(false)
  const [isWalking, setIsWalking] = useState(false)
  const currentYaw = useRef(0)
  const currentPitch = useRef(0)
  const expression = useRef(0)
  const expressionName = useRef('happy')
  const clipName = useRef('Idle_Loop')
  const previousClipName = useRef('Idle_Loop')
  const clipTime = useRef(0)
  const previousClipTime = useRef(0)
  const clipBlend = useRef(1)
  const lastMoveAt = useRef(0)
  const pose = useRef(new Map<string, THREE.Quaternion>())
  const previousPose = useRef(new Map<string, THREE.Quaternion>())
  const touchRig = useRef<THREE.Group | null>(null)
  const touchZones = useRef<THREE.Group | null>(null)
  const touchZoneRefs = useRef<Record<string, THREE.Mesh | null>>({})
  const chestSpring = useRef({
    offset: new THREE.Vector3(),
    velocity: new THREE.Vector3(),
  })
  const lastContactHand = useRef(new THREE.Vector3())
  const centerRaycaster = useMemo(() => new THREE.Raycaster(), [])
  const screenCenter = useMemo(() => new THREE.Vector2(0, 0), [])
  const touchStarted = useRef(0)
  const reactionZone = useRef<Reaction | null>(null)
  const localExpressionName = useRef('surprised')
  const localExpressionUntil = useRef(0)
  const lastExpressionName = useRef('')
  const outfitMeshes = useRef<OutfitMesh[]>([])
  const surfaces = useMemo(
    () => buildSurfaces(worldState, propPositions),
    [propPositions, worldState],
  )
  const postureRequested = useRef<'sit' | 'lie' | null>(null)
  const mountDone = useRef<MountState | null>(null)
  const stepState = useRef<{
    startedAt: number
    top: number
    foot: 'left' | 'right'
  } | null>(null)
  const stepSide = useRef<'left' | 'right'>('left')
  const walkMotion = useRef<WalkMotion | null>(null)
  const walkSpeed = useRef(0)
  const floorSitApplied = useRef(false)

  useEffect(() => {
    if (!locomotion.pendingPosture) postureRequested.current = null
  }, [locomotion.pendingPosture])

  useEffect(() => {
    if (root.current) root.current.rotation.order = 'YXZ'
  }, [])

  useEffect(() => {
    // 她走路时不能把椅子/桌子顶飞。推拉家具只能通过用户手或她自己的
    // push 工具；角色控制器只负责避开障碍。
    character.setApplyImpulsesToDynamicBodies(false)
    character.setCharacterMass(55)
    character.enableAutostep(0.22, 0.18, true)
    return () => character.free()
  }, [character])

  const beginAvatarGrab = useCallback(
    (zone: Reaction, point?: THREE.Vector3 | null) => {
      if (avatarGrab.current) return
      const body = avatarBody.current
      if (!body) return
      const current = body.translation()
      // 落点必须是她身上的真实命中点；没有命中点（极少数程序化触发）
      // 才退回当前手的位置。抓住后手从相机位置伸到落点，而不是在原地摸空气。
      const hand = point ?? new THREE.Vector3(
        viewer.current.hand[0],
        viewer.current.hand[1],
        viewer.current.hand[2],
      )
      const pushDir = hand.clone().sub(camera.position)
      if (pushDir.lengthSq() < 0.000001) pushDir.set(0, 0, -1)
      pushDir.normalize()
      // 手模以指尖为局部原点；抓取点只往体表外退 2cm，避免整只手悬空。
      const handOrigin = hand.clone().addScaledVector(pushDir, -0.02)
      const localReach = handOrigin
        .clone()
        .sub(camera.position)
        .applyQuaternion(camera.quaternion.clone().invert())
      const now = performance.now()
      avatarGrab.current = {
        zone,
        startedAt: now,
        offset: new THREE.Vector3(
          current.x - handOrigin.x,
          0,
          current.z - handOrigin.z,
        ),
        offsetY: current.y - handOrigin.y,
        lastPosition: new THREE.Vector3(current.x, current.y, current.z),
        lastDistanceToCamera: camera.position.distanceTo(
          new THREE.Vector3(current.x, current.y, current.z),
        ),
        lastAt: now,
        kind: 'hold',
        moved: false,
        preserveClip: clipName.current,
        preserveMoving: movingRef.current,
      }
      avatarCollider.current?.setSensor(true)
      colliderSensor.current = true
      viewer.current.handActive = true
      viewer.current.handHolding = '她'
      viewer.current.handContactZone = zone
      viewer.current.handContactKind = 'hold'
      viewer.current.handContactSpeed = 0
      viewer.current.handContactSeconds = 0
      viewer.current.handTarget = [handOrigin.x, handOrigin.y, handOrigin.z]
      viewer.current.handReachOffset = [localReach.x, localReach.y, localReach.z]
      onAvatarGrabStart(zone)
      onReact(zone)
    },
    [camera, onAvatarGrabStart, onReact, viewer],
  )

  useEffect(() => {
    const canvas = gl.domElement
    const pickTouchHit = (ndc: THREE.Vector2) => {
      if (!touchZones.current) return null
      touchZones.current.updateWorldMatrix(true, true)
      centerRaycaster.setFromCamera(ndc, camera)
      const hits = centerRaycaster
        .intersectObjects(touchZones.current.children, true)
      const nearestDistance = hits[0]?.distance ?? Number.POSITIVE_INFINITY
      let selected = hits[0]
      for (const candidate of hits) {
        // 只允许和最近命中同一层的触区参与优先级，避免从正面点小腹时
        // 射线穿到身体后面的尾巴，反而被尾巴高优先级抢走。
        if (candidate.distance > nearestDistance + 0.22) break
        const candidateZone = String(candidate.object.userData.reactionZone || '')
        const selectedZone = String(selected?.object.userData.reactionZone || '')
        if (
          (TOUCH_ZONE_PRIORITY[candidateZone] ?? 0) >
          (TOUCH_ZONE_PRIORITY[selectedZone] ?? 0)
        ) {
          selected = candidate
        }
      }
      if (selected) {
        // 专用触区用“中心 + 角色正前方”作为手落点，不再用大球表面交点。
        const zone = String(selected.object.userData.reactionZone || '')
        const anchorOffset = TOUCH_ANCHOR_OFFSET[zone] ?? 0
        if (anchorOffset > 0) {
          const center = new THREE.Vector3()
          selected.object.getWorldPosition(center)
          const forward = new THREE.Vector3(0, 0, 1)
          const rotation = new THREE.Quaternion()
          root.current?.getWorldQuaternion(rotation)
          forward.applyQuaternion(rotation).normalize()
          selected.point.copy(center).addScaledVector(forward, anchorOffset)
          selected.distance = camera.position.distanceTo(selected.point)
        }
      }
      return selected ?? null
    }
    const touchAtCrosshair = (event: PointerEvent) => {
      if (event.button !== 0 || !document.pointerLockElement) return
      if (!touchZones.current) return
      const hit = pickTouchHit(screenCenter)
      if (!hit) {
        updateSpaceDebug({ touchRay: 'miss' })
        return
      }
      const zone = String(hit.object.userData.reactionZone || '') as Reaction
      if (!zone) {
        updateSpaceDebug({ touchRay: 'no-zone' })
        return
      }
      const distance = camera.position.distanceTo(hit.point)
      if (distance > MAX_TOUCH_DISTANCE) {
        updateSpaceDebug({ touchRay: 'too-far', touchDistance: distance, touchZone: zone })
        return
      }
      event.preventDefault()
      event.stopPropagation()
      updateSpaceDebug({ touchRay: zone, touchDistance: distance, touchZone: zone })
      beginAvatarGrab(zone, hit.point)
    }
    // 自动化验收用：把每个触区中心投影成 NDC，并允许按 NDC 直接验证命中优先级。
    ;(window as any).__YF_DEBUG_TOUCH_PROBES__ = () => {
      if (!touchZones.current) return []
      touchZones.current.updateWorldMatrix(true, true)
      return Object.entries(touchZoneRefs.current)
        .filter(([, mesh]) => Boolean(mesh?.visible))
        .map(([key, mesh]) => {
          const world = new THREE.Vector3()
          mesh!.getWorldPosition(world)
          const projected = world.clone().project(camera)
          return {
            key,
            zone: String(mesh!.userData.reactionZone || ''),
            ndc: [projected.x, projected.y],
          }
        })
    }
    ;(window as any).__YF_DEBUG_PICK_ZONE__ = (ndcX: number, ndcY: number) => {
      const hit = pickTouchHit(new THREE.Vector2(ndcX, ndcY))
      if (!hit) return null
      return {
        zone: String(hit.object.userData.reactionZone || ''),
        point: hit.point.toArray(),
        distance: camera.position.distanceTo(hit.point),
      }
    }
    canvas.addEventListener('pointerdown', touchAtCrosshair, true)
    return () => {
      canvas.removeEventListener('pointerdown', touchAtCrosshair, true)
      delete (window as any).__YF_DEBUG_TOUCH_PROBES__
      delete (window as any).__YF_DEBUG_PICK_ZONE__
    }
  }, [beginAvatarGrab, camera, centerRaycaster, gl, screenCenter])

  useEffect(() => {
    const finish = () => {
      if (!avatarGrab.current) return
      avatarGrab.current = null
      viewer.current.handActive = false
      viewer.current.handHolding = null
      viewer.current.handContactZone = null
      viewer.current.handContactKind = null
      viewer.current.handContactSpeed = 0
      viewer.current.handContactSeconds = 0
      viewer.current.handReachOffset = null
      viewer.current.handTarget = viewer.current.hand
      updateSpaceDebug({
        avatarGrab: null,
        handActive: false,
        handHolding: null,
      })
      const body = avatarBody.current
      if (body) {
        const position = body.translation()
        const ground = groundHeightAt(
          surfaces,
          position.x,
          position.z,
          MAX_WALK_STEP,
        )
        releaseFall.current =
          position.y > ground + 0.03 ? { vy: 0 } : null
      }
      onAvatarGrabEnd()
    }
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', finish)
    window.addEventListener('blur', finish)
    document.addEventListener('pointerlockchange', finish)
    return () => {
      window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', finish)
      window.removeEventListener('blur', finish)
      document.removeEventListener('pointerlockchange', finish)
    }
  }, [onAvatarGrabEnd, surfaces, viewer])

  const stepAvatar = (delta: number): { moving: boolean; yaw: number } => {
    const body = avatarBody.current
    const collider = avatarCollider.current
    updateSpaceDebug({
      avatarTarget: locomotion.position,
      avatarHasBody: Boolean(body),
      avatarHasCollider: Boolean(collider),
    })
    if (!body || !collider) return { moving: false, yaw: locomotion.yaw }
    if ((window as any).__YF_DEBUG_FREEZE__) {
      return { moving: false, yaw: locomotion.yaw }
    }

    const postureNow = performance.now()
    if (
      previousPosture.current !== 'stand' &&
      locomotion.posture === 'stand'
    ) {
      // 刚从坐/躺姿势站起来时，先给半秒无碰撞离座窗口，
      // 否则角色胶囊会被椅子/床的碰撞体卡住。
      sensorGraceUntil.current = postureNow + 520
    }
    previousPosture.current = locomotion.posture
    const shouldBeSensor =
      Boolean(avatarGrab.current) ||
      Boolean(locomotion.mount) ||
      locomotion.posture !== 'stand' ||
      postureNow < sensorGraceUntil.current
    if (colliderSensor.current !== shouldBeSensor) {
      collider.setSensor(shouldBeSensor)
      colliderSensor.current = shouldBeSensor
    }

    const current = body.translation()
    const grab = avatarGrab.current
    const falling = releaseFall.current
    if (!grab && falling) {
      const nowFall = performance.now()
      const dtFall = Math.max(0.001, Math.min(delta, 0.05))
      falling.vy -= 9.81 * dtFall
      const ground = groundHeightAt(surfaces, current.x, current.z, MAX_WALK_STEP)
      let nextY = current.y + falling.vy * dtFall
      if (nextY <= ground) {
        nextY = ground
        releaseFall.current = null
        falling.vy = 0
      }
      body.setNextKinematicTranslation({ x: current.x, y: nextY, z: current.z })
      lastBodyPosition.current.set(current.x, nextY, current.z)
      selfState.current = {
        ...selfState.current,
        position: [current.x, nextY, current.z],
        posture: 'stand',
        sitting: false,
        lying: false,
        support_key: null,
        moving: false,
        still_seconds: (nowFall - stillSince.current) / 1000,
        target: null,
        at_target: false,
      }
      updateSpaceDebug({
        avatar: [current.x, nextY, current.z],
        falling: Boolean(releaseFall.current),
      })
      return { moving: false, yaw: root.current?.rotation.y ?? 0 }
    }
    if (grab) {
      // 用“手这一帧的目标落点”，而不是视觉手当前的平滑位置；
      // 否则伸过去的第一瞬间会把她按反方向拖一下。
      const hand = viewer.current.handReachOffset
        ? viewer.current.handTarget
        : viewer.current.hand
      const now = performance.now()
      const dt = Math.max(0.001, delta)
      const desiredX = THREE.MathUtils.clamp(hand[0] + grab.offset.x, -4.62, 4.62)
      const desiredZ = THREE.MathUtils.clamp(hand[2] + grab.offset.z, -4.62, 4.62)
      const maxStep = 2.6 * dt
      // 用短阻尼跟随手掌，而不是逐帧硬贴。手掌的抚摸正弦和相机微小
      // 抖动不会原封不动传给她的全身，看起来就不会“一抽一抽”。
      const follow = 1 - Math.exp(-dt * 16)
      // 从上一帧的“目标位置”继续积分，而不是回读物理体位置。
      // 运动学体在物理步进里可能有一点点量化/延迟，回读会让指令
      // 出现亚毫米级反向，叠起来就是肉眼可见的高频细抖。
      const currentX = grab.lastPosition.x
      const currentZ = grab.lastPosition.z
      let stepX = (desiredX - currentX) * follow
      let stepZ = (desiredZ - currentZ) * follow
      const stepDistance = Math.hypot(stepX, stepZ)
      if (stepDistance > maxStep) {
        stepX = (stepX / stepDistance) * maxStep
        stepZ = (stepZ / stepDistance) * maxStep
      }
      const nextX = currentX + stepX
      const nextZ = currentZ + stepZ
      // 垂直方向也跟手：抓着往上提就能把她带离地面，不再强行贴地。
      const nextY = THREE.MathUtils.clamp(hand[1] + grab.offsetY, -0.2, 3.4)
      body.setNextKinematicTranslation({ x: nextX, y: nextY, z: nextZ })
      const movedX = nextX - grab.lastPosition.x
      const movedY = nextY - grab.lastPosition.y
      const movedZ = nextZ - grab.lastPosition.z
      const moved = Math.hypot(movedX, movedY, movedZ)
      const speed = moved / dt
      const horizontalSpeed =
        Math.hypot(movedX, movedZ) / Math.max(0.001, dt)
      const verticalSpeed = Math.abs(movedY) / Math.max(0.001, dt)
      const toAvatar = new THREE.Vector3(
        nextX - camera.position.x,
        0,
        nextZ - camera.position.z,
      )
      if (toAvatar.lengthSq() < 0.0001) toAvatar.set(0, 0, -1)
      toAvatar.normalize()
      const moveVector = new THREE.Vector3(
        nextX - grab.lastPosition.x,
        0,
        nextZ - grab.lastPosition.z,
      )
      const awayFromUser = moveVector.dot(toAvatar) / Math.max(0.001, dt)
      const cameraDistance = camera.position.distanceTo(
        new THREE.Vector3(nextX, nextY, nextZ),
      )
      grab.kind =
        speed < 0.22
          ? 'hold'
          : verticalSpeed > horizontalSpeed * 0.8
            ? 'drag'
            : awayFromUser > 0.12
              ? 'push'
              : awayFromUser < -0.12
                ? 'pull'
                : 'drag'
      grab.moved = grab.moved || speed > 0.28
      grab.lastPosition.set(nextX, nextY, nextZ)
      grab.lastDistanceToCamera = cameraDistance
      grab.lastAt = now
      viewer.current.handContactKind = grab.kind
      viewer.current.handContactSpeed = speed
      viewer.current.handContactSeconds = (now - grab.startedAt) / 1000
      selfState.current = {
        ...selfState.current,
        position: [nextX, nextY, nextZ],
        posture: locomotion.posture,
        sitting: locomotion.sitting,
        lying: locomotion.lying,
        support_key: locomotion.supportKey,
        // 抓取只冻结位移；如果她本来在走路，视觉动作继续原地走。
        moving: grab.preserveMoving,
        still_seconds: grab.preserveMoving
          ? 0
          : selfState.current.still_seconds + delta,
        target: null,
        at_target: false,
      }
      updateSpaceDebug({
        avatar: [nextX, nextY, nextZ],
        avatarGrab: {
          zone: grab.zone,
          kind: grab.kind,
          speed,
          seconds: viewer.current.handContactSeconds,
        },
      })
      return {
        moving: grab.preserveMoving,
        // 被抓/被摸不强制转身看 TA；她本来朝哪就朝哪。
        yaw: root.current?.rotation.y ?? 0,
      }
    }

    const currentGround = groundHeightAt(surfaces, current.x, current.z, MAX_WALK_STEP)
    const targetGround = groundHeightAt(
      surfaces,
      locomotion.position[0],
      locomotion.position[2],
      MAX_WALK_STEP,
    )
    updateSpaceDebug({ avatarBody: [current.x, current.y, current.z] })
    const dx = locomotion.position[0] - current.x
    const dz = locomotion.position[2] - current.z
    const distance = Math.hypot(dx, dz)
    const now = performance.now()
    const mount = locomotion.mount
    if (mount) {
      const progress = mountProgress(mount, now)
      const [nextX, nextZ] = mountPosition(mount, progress)
      const nextY = groundHeightAt(surfaces, nextX, nextZ, MAX_WALK_STEP)
      body.setNextKinematicTranslation({ x: nextX, y: nextY, z: nextZ })
      lastBodyPosition.current.set(nextX, nextY, nextZ)
      selfState.current = {
        ...selfState.current,
        position: [nextX, nextY, nextZ],
        posture: 'stand',
        sitting: false,
        lying: false,
        support_key: locomotion.supportKey,
        moving: false,
        still_seconds: 0,
        target: locomotion.targetKey ?? null,
        at_target: false,
      }
      updateSpaceDebug({
        avatar: [nextX, nextY, nextZ],
        mountProgress: progress,
        seatSnap: 'snap',
        mountFrom: mount.from,
        mountTo: mount.to,
      })
      if (progress >= 1 && mountDone.current !== mount) {
        mountDone.current = mount
        onMountComplete(mount.kind)
      }
      return { moving: false, yaw: mount.faceYaw }
    }
    const seated = locomotion.posture !== 'stand'
    if (seated) {
      walkMotion.current = null
      walkSpeed.current = 0
      const mountStep = Math.min(distance, 1.7 * delta)
      const nextX =
        distance > 0.001 ? current.x + (dx / distance) * mountStep : current.x
      const nextZ =
        distance > 0.001 ? current.z + (dz / distance) * mountStep : current.z
      const seatedY = THREE.MathUtils.damp(current.y, currentGround, 12, delta)
      body.setNextKinematicTranslation({ x: nextX, y: seatedY, z: nextZ })
      lastBodyPosition.current.set(nextX, seatedY, nextZ)
      selfState.current = {
        ...selfState.current,
        position: [nextX, seatedY, nextZ],
        posture: locomotion.posture,
        sitting: locomotion.sitting,
        lying: locomotion.lying,
        support_key: locomotion.supportKey,
        moving: false,
        still_seconds: (now - stillSince.current) / 1000,
        target: locomotion.targetKey ?? null,
        at_target:
          Boolean(locomotion.targetKey) &&
          distance <= SPACE_ARRIVAL_DISTANCE,
      }
      updateSpaceDebug({ avatar: [nextX, seatedY, nextZ] })
      return { moving: false, yaw: locomotion.yaw }
    }
    if (distance <= SPACE_ARRIVAL_DISTANCE) {
      walkMotion.current = null
      walkSpeed.current = 0
      if (
        locomotion.pendingPosture &&
        postureRequested.current !== locomotion.pendingPosture
      ) {
        postureRequested.current = locomotion.pendingPosture
        onPostureReady(locomotion.pendingPosture)
      }
      const settledY = THREE.MathUtils.damp(current.y, targetGround, 12, delta)
      body.setNextKinematicTranslation({
        x: locomotion.position[0],
        y: settledY,
        z: locomotion.position[2],
      })
      lastBodyPosition.current.set(locomotion.position[0], settledY, locomotion.position[2])
      selfState.current = {
        ...selfState.current,
        position: [locomotion.position[0], settledY, locomotion.position[2]],
        posture: locomotion.posture,
        sitting: locomotion.sitting,
        lying: locomotion.lying,
        support_key: locomotion.supportKey,
        moving: false,
        still_seconds: (now - stillSince.current) / 1000,
        target: locomotion.targetKey ?? null,
        at_target: Boolean(locomotion.targetKey),
      }
      updateSpaceDebug({ avatar: [locomotion.position[0], settledY, locomotion.position[2]] })
      return { moving: false, yaw: locomotion.yaw }
    }

    // D417：人有起步犹豫、加速、巡航和减速；不是开关键式的匀速滑行。
    const motionKey = `${locomotion.targetKey ?? 'none'}:${locomotion.position[0].toFixed(3)}:${locomotion.position[2].toFixed(3)}`
    if (!walkMotion.current || walkMotion.current.key !== motionKey) {
      const baseSpeed =
        distance < 0.9 ? 0.78 : distance < 2.5 ? 1.12 : distance < 5.5 ? 1.32 : 1.48
      const topSpeed = THREE.MathUtils.clamp(
        baseSpeed + (Math.random() - 0.5) * 0.16,
        0.64,
        1.66,
      )
      walkMotion.current = {
        key: motionKey,
        decisionUntil: now + (distance > 0.45 ? 150 + Math.random() * 360 : 0),
        topSpeed,
        speed: 0,
        acceleration: 1.9 + Math.random() * 0.9,
        deceleration: 2.5 + Math.random() * 1.0,
        seed: Math.random() * 10,
      }
    }
    const motion = walkMotion.current
    if (now < motion.decisionUntil) {
      walkSpeed.current = 0
      selfState.current = {
        ...selfState.current,
        position: [current.x, current.y, current.z],
        posture: 'stand',
        sitting: false,
        lying: false,
        support_key: locomotion.supportKey,
        moving: false,
        still_seconds: (now - stillSince.current) / 1000,
        target: locomotion.targetKey ?? null,
        at_target: false,
      }
      updateSpaceDebug({
        avatar: [current.x, current.y, current.z],
        avatarDecisionIn: Math.max(0, motion.decisionUntil - now),
        walkTopSpeed: motion.topSpeed,
        walkSpeed: 0,
      })
      return { moving: false, yaw: Math.atan2(dx, dz) }
    }

    let dirX = dx / distance
    let dirZ = dz / distance
    const rayOrigin = { x: current.x, y: current.y + 0.72, z: current.z }
    const isBlocked = (x: number, z: number) => {
      const hit = world.castRay(
        new rapier.Ray(rayOrigin, { x, y: 0, z }),
        0.85,
        true,
        undefined,
        undefined,
        undefined,
        undefined,
        (candidate) => candidate.parent()?.bodyType() === 1,
      )
      return Boolean(hit)
    }
    if (isBlocked(dirX, dirZ)) {
      for (const angle of [0.72, -0.72, 1.35, -1.35, 2.05, -2.05]) {
        const cos = Math.cos(angle)
        const sin = Math.sin(angle)
        const nextX = dirX * cos - dirZ * sin
        const nextZ = dirX * sin + dirZ * cos
        if (!isBlocked(nextX, nextZ)) {
          dirX = nextX
          dirZ = nextZ
          break
        }
      }
    }

    const targetYaw = Math.atan2(dirX, dirZ)
    const yawError = Math.abs(
      Math.atan2(
        Math.sin(targetYaw - (root.current?.rotation.y ?? 0)),
        Math.cos(targetYaw - (root.current?.rotation.y ?? 0)),
      ),
    )
    const turnPenalty = THREE.MathUtils.clamp(1 - yawError / 1.85, 0.2, 1)
    const brakeSpeed = Math.sqrt(
      Math.max(0.01, 2 * motion.deceleration * Math.max(0, distance - 0.1)),
    )
    const pace = 1 + Math.sin(now * 0.0011 + motion.seed) * 0.035
    const desiredSpeed = Math.max(
      0.08,
      Math.min(
        motion.topSpeed * turnPenalty * pace,
        brakeSpeed,
        distance < 0.4 ? Math.max(0.1, distance * 1.15) : motion.topSpeed,
      ),
    )
    motion.speed = THREE.MathUtils.damp(
      motion.speed,
      desiredSpeed,
      desiredSpeed > motion.speed ? 4.6 : 7.2,
      delta,
    )
    walkSpeed.current = motion.speed

    const step = Math.min(distance, motion.speed * delta)
    steering.current.set(dirX, 0, dirZ).multiplyScalar(step)
    character.computeColliderMovement(
      collider,
      steering.current,
      undefined,
      undefined,
      (candidate) => candidate.parent()?.bodyType() !== 2,
    )
    const movement = character.computedMovement()
    const nextX = current.x + movement.x
    const nextZ = current.z + movement.z
    const nextGround = groundHeightAt(surfaces, nextX, nextZ, MAX_WALK_STEP)
    const heightDelta = nextGround - currentGround
    if (heightDelta > WALK_STEP_UP && !stepState.current) {
      stepSide.current = stepSide.current === 'left' ? 'right' : 'left'
      stepState.current = {
        startedAt: now,
        top: nextGround,
        foot: stepSide.current,
      }
    }
    const nextBodyY = THREE.MathUtils.damp(current.y, nextGround, 12, delta)
    body.setNextKinematicTranslation({ x: nextX, y: nextBodyY, z: nextZ })
    lastBodyPosition.current.set(nextX, nextBodyY, nextZ)
    const actualDistance = Math.hypot(nextX - current.x, nextZ - current.z)
    const actualSpeed = actualDistance / Math.max(0.0001, delta)
    walkSpeed.current = actualSpeed
    const moving = actualSpeed > 0.012
    if (moving) stillSince.current = now
    selfState.current = {
      ...selfState.current,
      position: [nextX, nextBodyY, nextZ],
      posture: 'stand',
      sitting: false,
      lying: false,
      support_key: locomotion.supportKey,
      moving,
      still_seconds: moving ? 0 : (now - stillSince.current) / 1000,
      target: locomotion.targetKey ?? null,
      at_target: false,
    }
    updateSpaceDebug({
      avatar: [nextX, nextBodyY, nextZ],
      avatarStep: heightDelta,
      avatarStepFoot: stepState.current?.foot ?? null,
      walkSpeed: actualSpeed,
      walkTopSpeed: motion.topSpeed,
      avatarDecisionIn: 0,
    })
    return { moving, yaw: targetYaw }
  }

  const viewerLook = (): { yaw: number; pitch: number } | null => {
    if (!root.current) return null
    const snapshot = viewer.current
    // 她的手/视线是持续刺激，不是“叫一声才看”。手始终优先被追踪，
    // 只有手数据缺失时才退回相机。
    const source = snapshot.hand ?? snapshot.camera
    const worldTarget = new THREE.Vector3(source[0], source[1], source[2])
    const local = root.current.worldToLocal(worldTarget)
    const horizontal = Math.max(0.18, Math.hypot(local.x, local.z))
    return {
      yaw: THREE.MathUtils.clamp(Math.atan2(local.x, local.z), -0.82, 0.82),
      pitch: THREE.MathUtils.clamp(Math.atan2(local.y - 1.38, horizontal), -0.28, 0.26),
    }
  }

  const applyTouchPulse = () => {
    if (!touchRig.current) return
    const grab = avatarGrab.current
    if (grab) {
      // 抓着她时只锁位移，不再给全身叠加持续 pulse；否则原本的动作会被
      // 一层 0.12rad 的周期摆动盖掉，看起来就是“鬼畜/定住”。
      touchRig.current.position.set(0, 0, 0)
      touchRig.current.rotation.set(0, 0, 0)
      return
    }
    const pulse = reactionEnvelope()
    const zone = reactionZone.current
    const stroke = 0
    const recoil =
      zone === 'chest' ||
      zone === 'waist' ||
      zone === 'thigh' ||
      zone === 'butt'
        ? 1
        : 0.45
    touchRig.current.position.set(
      zone === 'ear' ? pulse * 0.006 : 0,
      pulse * 0.012,
      -pulse * 0.022 * recoil,
    )
    touchRig.current.position.x += stroke * 0.006
    touchRig.current.position.y += Math.cos(performance.now() * 0.0029) * 0.002
    touchRig.current.rotation.x = -pulse * 0.035 * recoil
    touchRig.current.rotation.z = pulse * 0.015 + stroke * 0.012
    if (head.current && pulse > 0) {
      head.current.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(
          new THREE.Euler(
            pulse * (zone === 'head' || zone === 'ear' ? -0.08 : 0.04),
            pulse * (zone === 'hand' ? 0.16 : -0.06),
            pulse * (zone === 'ear' ? 0.12 : 0.03),
            'YXZ',
          ),
        ),
      )
      head.current.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(
          new THREE.Euler(0, 0, stroke * 0.045, 'YXZ'),
        ),
      )
    }
    if (spine.current && pulse > 0) {
      spine.current.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(
          new THREE.Euler(-pulse * 0.025 * recoil, 0, 0, 'XYZ'),
        ),
      )
    }
  }

  useEffect(() => {
    if (!vrm) return
    vrm.scene.traverse((object) => {
      if ((object as THREE.Mesh).isMesh) object.frustumCulled = false
    })
    const box = new THREE.Box3().setFromObject(vrm.scene)
    const size = new THREE.Vector3()
    box.getSize(size)
    const scale = 1.58 / Math.max(0.01, size.y)
    vrm.scene.scale.setScalar(scale)
    const scaledBox = new THREE.Box3().setFromObject(vrm.scene)
    vrm.scene.position.y -= scaledBox.min.y

    const humanoid = vrm.humanoid
    if (humanoid) humanoid.autoUpdateHumanBones = true
    const getBone = humanoid?.getNormalizedBoneNode.bind(humanoid)
    hips.current = getBone?.('hips' as VRMHumanBoneName) ?? null
    chest.current = getBone?.('chest' as VRMHumanBoneName) ?? null
    upperChest.current = getBone?.('upperChest' as VRMHumanBoneName) ?? null
    head.current = getBone?.('head' as VRMHumanBoneName) ?? null
    neck.current = getBone?.('neck' as VRMHumanBoneName) ?? null
    spine.current = getBone?.('spine' as VRMHumanBoneName) ?? null
    rightHand.current = getBone?.('rightHand' as VRMHumanBoneName) ?? null
    leftHand.current = getBone?.('leftHand' as VRMHumanBoneName) ?? null
    leftUpperArm.current = getBone?.('leftUpperArm' as VRMHumanBoneName) ?? null
    rightUpperArm.current = getBone?.('rightUpperArm' as VRMHumanBoneName) ?? null
    leftLowerArm.current = getBone?.('leftLowerArm' as VRMHumanBoneName) ?? null
    rightLowerArm.current = getBone?.('rightLowerArm' as VRMHumanBoneName) ?? null
    leftUpperLeg.current = getBone?.('leftUpperLeg' as VRMHumanBoneName) ?? null
    rightUpperLeg.current = getBone?.('rightUpperLeg' as VRMHumanBoneName) ?? null
    leftLowerLeg.current = getBone?.('leftLowerLeg' as VRMHumanBoneName) ?? null
    rightLowerLeg.current = getBone?.('rightLowerLeg' as VRMHumanBoneName) ?? null
    leftFoot.current = getBone?.('leftFoot' as VRMHumanBoneName) ?? null
    rightFoot.current = getBone?.('rightFoot' as VRMHumanBoneName) ?? null
    leftLegRest.current = leftUpperLeg.current?.quaternion.clone() ?? null
    rightLegRest.current = rightUpperLeg.current?.quaternion.clone() ?? null
    const restBones = [
      hips.current,
      chest.current,
      upperChest.current,
      head.current,
      neck.current,
      spine.current,
      leftUpperArm.current,
      rightUpperArm.current,
      leftLowerArm.current,
      rightLowerArm.current,
      leftUpperLeg.current,
      rightUpperLeg.current,
      leftLowerLeg.current,
      rightLowerLeg.current,
      leftFoot.current,
      rightFoot.current,
    ].filter((bone): bone is THREE.Object3D => Boolean(bone))
    // 每个模型的静止姿势只记一次。程序化动画每帧先回到这里再加动作，
    // 否则 head/spine 的 multiply 会一帧帧累积，最后头就会“飞出去”。
    restPoses.current = new Map(
      restBones.map((bone) => [bone, bone.quaternion.clone()] as const),
    )
    proceduralPhase.current = 0
    return () => {
      vrm.scene.removeFromParent()
    }
  }, [vrm])

  useEffect(() => {
    if (!vrm) return
    outfitMeshes.current = collectOutfitMeshes(vrm)
  }, [vrm])

  useEffect(() => {
    if (!vrm) return
    // Tripo 导出的 unlit 材质把颜色放在 emissiveMap 里、baseColor 是黑色。
    // three.js 默认的 ACES tone mapping 会把这类高亮贴图洗成灰白，皮肤尤其
    // 明显。这里只对这种“emissive-only”材质关闭 tone mapping，按贴图原色
    // 显示；普通 PBR 材质（默认 Vivi）不受影响。
    vrm.scene.traverse((object) => {
      const mesh = object as THREE.Mesh
      if (!mesh.isMesh) return
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
      for (const material of materials) {
        const standard = material as THREE.MeshStandardMaterial
        if (!standard || !standard.emissiveMap) continue
        const emissiveOnly =
          !standard.map || (standard.color && standard.color.getHex() === 0x000000)
        if (!emissiveOnly) continue
        standard.toneMapped = false
        standard.needsUpdate = true
      }
    })
  }, [vrm])

  useEffect(() => {
    if (!vrm) return
    // 汐音是深蓝发色。默认 Vivi 模型也在这里运行时压成深蓝；
    // 不修改 VRM 文件，换回原色也很容易。
    const hairTint = new THREE.Color('#4664bf')
    vrm.scene.traverse((object) => {
      const mesh = object as THREE.Mesh
      if (!mesh.isMesh) return
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
      for (const material of materials) {
        if (!material || !String(material.name || '').includes('Hair')) continue
        const colored = material as THREE.MeshStandardMaterial
        if (colored.color) {
          colored.color.copy(hairTint)
          colored.needsUpdate = true
        }
      }
    })
  }, [vrm])

  useEffect(() => {
    if (!vrm) return
    const apply = (slot: OutfitMesh['slot'], value: string) => {
      for (const item of outfitMeshes.current) {
        if (item.slot !== slot) continue
        item.mesh.visible = value !== 'none'
      }
    }
    apply('top', outfit.top)
    apply('bottom', outfit.bottom)
    apply('shoes', outfit.shoes)
    // 有些 VRM（大肥鱼）把整套衣服合成一个 mesh：只要还穿着任一件就显示。
    for (const item of outfitMeshes.current) {
      if (item.slot === 'outfit') {
        item.mesh.visible = outfit.top !== 'none' || outfit.bottom !== 'none'
      }
    }
  }, [outfit, vrm])

  useEffect(() => {
    if (!vrm?.expressionManager) return
    expressionName.current = moodExpression || 'relaxed'
    expression.current = moodExpression ? 0.82 : 0
  }, [moodExpression, vrm])

  useEffect(() => {
    if (!reaction) {
      reactionZone.current = null
      return
    }
    touchStarted.current = performance.now()
    reactionZone.current = reaction
    localExpressionName.current =
      reaction === 'head' || reaction === 'ear' || reaction === 'neck'
        ? 'surprised'
        : reaction === 'chest' ||
            reaction === 'waist' ||
            reaction === 'thigh' ||
            reaction === 'butt'
          ? 'relaxed'
          : 'happy'
    localExpressionUntil.current = performance.now() + 950
  }, [reaction])

  const reactionEnvelope = () => {
    const elapsed = (performance.now() - touchStarted.current) / 1000
    if (elapsed < 0 || elapsed >= 0.9) return 0
    return Math.sin((elapsed / 0.9) * Math.PI) * (1 - elapsed / 0.9)
  }

  const writeExpression = (name: string, value: number) => {
    if (!vrm?.expressionManager) return
    // D451：汐音的 VRM 带专属表情；没有这些自定义表情的模型退回标准预设。
    let mapped = name
    try {
      if (!vrm.expressionManager.getExpression(name)) {
        mapped = EXPRESSION_FALLBACK[name] ?? name
      }
    } catch {
      mapped = EXPRESSION_FALLBACK[name] ?? name
    }
    try {
      if (lastExpressionName.current && lastExpressionName.current !== mapped) {
        vrm.expressionManager.setValue(lastExpressionName.current, 0)
      }
      vrm.expressionManager.setValue(mapped, value)
      lastExpressionName.current = mapped
    } catch {
      // 占位模型不一定有全部表情，不能因此中断房间
    }
  }

  const applyEmotionPose = () => {
    const pose = emotionName ? EMOTION_POSES[emotionName] : null
    if (!pose) return
    const multiply = (
      node: MutableRefObject<THREE.Object3D | null>,
      values: Vec3 | undefined,
    ) => {
      if (!node.current || !values) return
      node.current.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(
          new THREE.Euler(values[0], values[1], values[2], 'XYZ'),
        ),
      )
    }
    multiply(head, pose.head)
    multiply(spine, pose.spine)
  }

  const applyStepLift = () => {
    const step = stepState.current
    if (!step) return
    const progress = (performance.now() - step.startedAt) / 420
    if (progress >= 1) {
      stepState.current = null
      return
    }
    const lift = Math.sin(Math.max(0, Math.min(1, progress)) * Math.PI)
    const upper = step.foot === 'left' ? leftUpperLeg.current : rightUpperLeg.current
    const lower = step.foot === 'left' ? leftLowerLeg.current : rightLowerLeg.current
    if (upper) {
      upper.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(new THREE.Euler(-0.55 * lift, 0, 0, 'XYZ')),
      )
    }
    if (lower) {
      lower.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(new THREE.Euler(0.75 * lift, 0, 0, 'XYZ')),
      )
    }
  }

  const applyFloorSitPose = () => {
    floorSitApplied.current = false
    if (locomotion.posture !== 'sit' || locomotion.supportKey !== 'floor') return
    floorSitApplied.current = true
    // 坐在椅子上时小腿自然下垂；坐地板上要把小腿向前伸平，
    // 让大腿、小腿都贴在地面上，而不是把脚折到屁股下面或伸进地板。
    const lowerDelta = new THREE.Quaternion().setFromEuler(
      new THREE.Euler(1.38, 0, 0, 'XYZ'),
    )
    leftLowerLeg.current?.quaternion.multiply(lowerDelta)
    rightLowerLeg.current?.quaternion.multiply(lowerDelta)
  }

  const restoreRestPose = () => {
    for (const [bone, rest] of restPoses.current) {
      bone.quaternion.copy(rest)
    }
  }

  /**
   * 所有 VRM 共用的一套程序化基础动作：待机呼吸、重心摆动、走路摆腿摆臂。
   * 不依赖模型名、不依赖动画库；上面如果成功套了重定向动画，这一层只做
   * “手臂别横着”的兜底，没套动画时它就是完整的待机/走路动作。
   */
  const applyProceduralMotion = (delta: number, allowFullBody: boolean) => {
    const now = performance.now() / 1000
    const seed = idleSeed.current
    const moving = movingRef.current || isWalking
    const speed = Math.max(0, walkSpeed.current)
    const sway = Math.sin(now * 0.37 + seed)
    const breath = Math.sin(now * 1.25 + seed * 0.7)

    if (allowFullBody) {
      if (spine.current) {
        spine.current.quaternion.multiply(
          new THREE.Quaternion().setFromEuler(
            new THREE.Euler(breath * 0.018, sway * 0.012, 0, 'XYZ'),
          ),
        )
      }
      if (chest.current) {
        chest.current.quaternion.multiply(
          new THREE.Quaternion().setFromEuler(
            new THREE.Euler(breath * 0.01, 0, sway * 0.008, 'XYZ'),
          ),
        )
      }
      if (hips.current) {
        hips.current.quaternion.multiply(
          new THREE.Quaternion().setFromEuler(
            new THREE.Euler(0, sway * 0.012, sway * 0.018, 'XYZ'),
          ),
        )
      }
    }

    if (moving && allowFullBody) {
      proceduralPhase.current += delta * (3.6 + speed * 5.4)
      const phase = proceduralPhase.current
      const amp = THREE.MathUtils.clamp(0.24 + speed * 0.2, 0.24, 0.62)
      const legSwing = Math.sin(phase) * amp
      if (leftUpperLeg.current && leftLegRest.current) {
        leftUpperLeg.current.quaternion
          .copy(leftLegRest.current)
          .multiply(
            new THREE.Quaternion().setFromEuler(
              new THREE.Euler(legSwing, 0, 0, 'XYZ'),
            ),
          )
      }
      if (rightUpperLeg.current && rightLegRest.current) {
        rightUpperLeg.current.quaternion
          .copy(rightLegRest.current)
          .multiply(
            new THREE.Quaternion().setFromEuler(
              new THREE.Euler(-legSwing, 0, 0, 'XYZ'),
            ),
          )
      }
      if (leftLowerLeg.current) {
        leftLowerLeg.current.quaternion.multiply(
          new THREE.Quaternion().setFromEuler(
            new THREE.Euler(Math.max(0, -Math.sin(phase)) * amp * 1.4, 0, 0, 'XYZ'),
          ),
        )
      }
      if (rightLowerLeg.current) {
        rightLowerLeg.current.quaternion.multiply(
          new THREE.Quaternion().setFromEuler(
            new THREE.Euler(Math.max(0, Math.sin(phase)) * amp * 1.4, 0, 0, 'XYZ'),
          ),
        )
      }
      // 走路时身体轻微上下起伏；下一帧 locomotion 会先复位，再叠回来。
      if (root.current) {
        root.current.position.y += Math.sin(phase * 2) * 0.012 * (0.5 + speed)
      }
    } else {
      proceduralPhase.current = 0
    }

    if (allowFullBody) {
      // 手臂朝身体两侧下方；走路时再前后摆。只要模型有上臂/前臂骨就能用。
      const armForward = moving ? Math.sin(proceduralPhase.current) * 0.16 : 0
      aimArmToward(
        leftUpperArm.current,
        leftLowerArm.current,
        root.current,
        new THREE.Vector3(-0.42, -0.9, 0.16 + armForward),
      )
      aimArmToward(
        rightUpperArm.current,
        rightLowerArm.current,
        root.current,
        new THREE.Vector3(0.42, -0.9, 0.16 - armForward),
      )
    }
  }

  const updateTouchZones = () => {
    const rig = touchRig.current
    if (!rig) return
    rig.updateWorldMatrix(true, false)
    const place = (
      key: string,
      bone: THREE.Object3D | null | undefined,
      x: number,
      y: number,
      z: number,
    ) => {
      const mesh = touchZoneRefs.current[key]
      if (!mesh || !bone) return
      const world = new THREE.Vector3()
      bone.getWorldPosition(world)
      rig.worldToLocal(world)
      mesh.position.set(world.x + x, world.y + y, world.z + z)
    }
    place('head', head.current, 0, 0.1, 0.07)
    place('neck', neck.current, 0, -0.01, 0.05)
    const chestBone = upperChest.current ?? chest.current
    place('chest', chestBone, 0, 0.02, 0.04)
    place('waist', spine.current, 0, 0.04, 0.09)
    place('butt', hips.current, 0, -0.03, -0.19)
    place('thigh-left', leftUpperLeg.current, 0, -0.15, 0.07)
    place('thigh-right', rightUpperLeg.current, 0, -0.15, 0.07)
    place('ear-left', head.current, -0.12, 0.08, 0.01)
    place('ear-right', head.current, 0.12, 0.08, 0.01)
    place('hand-left', leftHand.current, 0, 0, 0)
    place('hand-right', rightHand.current, 0, 0, 0)
    touchZoneRefs.current.tail?.position.set(0.2, 0.88, -0.24)
    const centerOf = (key: string) => {
      const mesh = touchZoneRefs.current[key]
      return mesh
        ? mesh.position.toArray().map((value) => Math.round(value * 1000) / 1000)
        : null
    }
    updateSpaceDebug({
      touchZoneCenters: {
        chest: centerOf('chest'),
        waist: centerOf('waist'),
        butt: centerOf('butt'),
      },
    })
  }

  const applyBodyJiggle = (delta: number) => {
    const spring = chestSpring.current
    const zone = viewer.current.handContactZone
    const active =
      !avatarGrab.current &&
      (zone === 'chest' ||
        zone === 'waist' ||
        zone === 'butt')
    const hand = new THREE.Vector3(
      viewer.current.hand[0],
      viewer.current.hand[1],
      viewer.current.hand[2],
    )
    const target = new THREE.Vector3()
    if (active) {
      const bone =
        zone === 'chest'
          ? upperChest.current ?? chest.current
          : zone === 'waist'
            ? spine.current
            : hips.current
      if (bone) {
        const center = new THREE.Vector3()
        bone.getWorldPosition(center)
        const relative = hand.clone().sub(center)
        const movement = hand.clone().sub(lastContactHand.current)
        target.set(
          THREE.MathUtils.clamp(relative.x * 4.0 + movement.x * 20, -0.12, 0.12),
          THREE.MathUtils.clamp(relative.y * 2.0 + movement.y * 20, -0.09, 0.09),
          0,
        )
      }
    }
    lastContactHand.current.copy(hand)
    const dt = Math.max(0.001, Math.min(delta, 0.05))
    spring.velocity.x += (target.x - spring.offset.x) * 120 * dt
    spring.velocity.y += (target.y - spring.offset.y) * 120 * dt
    spring.velocity.multiplyScalar(Math.exp(-7 * dt))
    spring.offset.addScaledVector(spring.velocity, dt)
    if (!active) {
      spring.offset.multiplyScalar(Math.exp(-12 * dt))
      spring.velocity.multiplyScalar(Math.exp(-12 * dt))
    }
    const bone =
      zone === 'chest'
        ? upperChest.current ?? chest.current
        : zone === 'waist'
          ? spine.current
          : zone === 'butt'
            ? hips.current
            : null
    if (bone && spring.offset.lengthSq() > 1e-8) {
      bone.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(
          new THREE.Euler(
            spring.offset.y * 1.5,
            0,
            -spring.offset.x * 2.0,
            'XYZ',
          ),
        ),
      )
    }
  }

  useFrame((_state, delta) => {
    if (!vrm) return
    if (root.current) {
      const body =
        attention === 'busy' && locomotion.posture === 'stand'
          ? IDLE_BODY_POSES[idleKind]
          : null
      const { moving, yaw: desiredYaw } = stepAvatar(delta)
      const facingYaw = desiredYaw + (locomotion.lying ? Math.PI / 2 : 0)
      if (movingRef.current !== moving) {
        movingRef.current = moving
        setIsWalking(moving)
      }
      const yawDiff = Math.atan2(
        Math.sin(facingYaw - root.current.rotation.y),
        Math.cos(facingYaw - root.current.rotation.y),
      )
      root.current.rotation.y += yawDiff * (1 - Math.exp(-delta * (moving ? 8 : 3)))
      root.current.rotation.x = THREE.MathUtils.damp(
        root.current.rotation.x,
        locomotion.lying ? Math.PI / 2 : 0,
        8,
        delta,
      )
      root.current.position.x = THREE.MathUtils.damp(
        root.current.position.x,
        body?.position[0] ?? 0,
        5,
        delta,
      )
      root.current.position.z = THREE.MathUtils.damp(
        root.current.position.z,
        body?.position[2] ?? 0,
        5,
        delta,
      )
      const mount = locomotion.mount
      let targetRootY = locomotion.posture === 'stand' ? 0 : locomotion.rootY
      if (mount) {
        targetRootY = mountRootY(
          mount,
          mountProgress(mount, performance.now()),
        )
      }
      root.current.position.y = THREE.MathUtils.damp(
        root.current.position.y,
        targetRootY,
        mount ? 16 : 9,
        delta,
      )
    }

    const idleClip = locomotion.sitting
      ? 'Sitting_Idle_Loop'
      : performance.now() - lastMoveAt.current < 180
        ? 'Walk_Loop'
        : 'Idle_Loop'
    const mount = locomotion.mount
    const grab = avatarGrab.current
    const desiredClip = grab
      ? grab.preserveClip
      : mount
        ? 'Sitting_Enter'
        : movingRef.current
          ? 'Walk_Loop'
          : (animationClip ?? idleClip)
    if (movingRef.current && !grab) lastMoveAt.current = performance.now()
    if (clipName.current !== desiredClip) {
      previousClipName.current = clipName.current
      previousClipTime.current = clipTime.current
      clipName.current = desiredClip
      clipTime.current = 0
      clipBlend.current = 0
      previousPose.current.clear()
    }
    const animationRate =
      clipName.current === 'Walk_Loop'
        ? THREE.MathUtils.clamp(walkSpeed.current / 1.2, 0.62, 1.48)
        : 1
    clipTime.current += delta * animationRate
    // 先回到静止姿势。重定向动画只写它自己有轨道的骨头；没写的骨头会保留
    // 上一帧的旋转。不先复位的话，head/spine 的每帧叠加最后会把头转飞。
    restoreRestPose()
    const current = clips.get(clipName.current) ?? clips.get('Idle_Loop')
    const retargeted = sampleRetargetedClip(
      current,
      clipTime.current,
      pose.current,
    )
    if (retargeted) {
      if (clipBlend.current < 1) {
        clipBlend.current = Math.min(1, clipBlend.current + delta * 3.6)
        previousClipTime.current += delta * animationRate
        sampleRetargetedClip(
          clips.get(previousClipName.current),
          previousClipTime.current,
          previousPose.current,
        )
        for (const [bone, previous] of previousPose.current) {
          const target = pose.current.get(bone)
          if (target) target.slerp(previous, 1 - clipBlend.current)
        }
      }
      applyRetargetedPose(vrm, pose.current)
      applyStepLift()
    }
    // 所有模型同一套：没有可用的重定向动画时，程序化层负责完整的待机/走路；
    // 有动画时它只补呼吸和“手臂不要横着”。这里不再按 modelUrl 分支。
    applyProceduralMotion(delta, !retargeted)

    // 被摸/被抓时不要自动转头看 TA。她保持原本的身体朝向和头部动作；
    // 只有在聊天 engaged 或自己的深思动作里决定靠近，才会转过去。
    if (!avatarGrab.current && !viewer.current.handContactZone) {
      const viewed = viewerLook()
      const targetYaw =
        viewed?.yaw ?? THREE.MathUtils.clamp(idleTarget[0] * 0.22, -0.68, 0.68)
      const targetPitch =
        viewed?.pitch ??
        THREE.MathUtils.clamp((idleTarget[1] - 1.3) * -0.18, -0.18, 0.14)
      currentYaw.current = THREE.MathUtils.damp(
        currentYaw.current,
        targetYaw,
        4.5,
        delta,
      )
      currentPitch.current = THREE.MathUtils.damp(
        currentPitch.current,
        targetPitch,
        5,
        delta,
      )
    }
    if (head.current) {
      head.current.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(
          new THREE.Euler(currentPitch.current * 0.55, currentYaw.current * 0.4, 0, 'YXZ'),
        ),
      )
    }
    if (neck.current) {
      neck.current.quaternion.multiply(
        new THREE.Quaternion().setFromEuler(
          new THREE.Euler(currentPitch.current * 0.25, currentYaw.current * 0.35, 0, 'YXZ'),
        ),
      )
    }
    const localFace = performance.now() < localExpressionUntil.current
    const touching = Boolean(avatarGrab.current)
    const faceTarget = touching ? 0.72 : localFace ? 0.9 : moodExpression ? 0.82 : 0
    expression.current = THREE.MathUtils.damp(expression.current, faceTarget, 8, delta)
    writeExpression(
      touching
        ? 'relaxed'
        : localFace
          ? localExpressionName.current
          : expressionName.current,
      touching ? expression.current : localFace ? 0.86 : expression.current,
    )
    applyEmotionPose()
    // 动画把上臂甩成 T-pose 时，自动收回身体侧下方。
    aimArmToward(
      leftUpperArm.current,
      leftLowerArm.current,
      root.current,
      new THREE.Vector3(-0.42, -0.9, 0.16),
      true,
    )
    aimArmToward(
      rightUpperArm.current,
      rightLowerArm.current,
      root.current,
      new THREE.Vector3(0.42, -0.9, 0.16),
      true,
    )
    applyFloorSitPose()
    applyTouchPulse()
    applyBodyJiggle(delta)
    vrm.update(delta)
    updateTouchZones()

    if (rightHand.current) {
      const handWorld = new THREE.Vector3()
      rightHand.current.getWorldPosition(handWorld)
      selfState.current.hand = [handWorld.x, handWorld.y, handWorld.z]
    }
    const hipWorld = new THREE.Vector3()
    const leftKneeWorld = new THREE.Vector3()
    const rightKneeWorld = new THREE.Vector3()
    const leftFootWorld = new THREE.Vector3()
    const rightFootWorld = new THREE.Vector3()
    hips.current?.getWorldPosition(hipWorld)
    leftLowerLeg.current?.getWorldPosition(leftKneeWorld)
    rightLowerLeg.current?.getWorldPosition(rightKneeWorld)
    leftFoot.current?.getWorldPosition(leftFootWorld)
    rightFoot.current?.getWorldPosition(rightFootWorld)
    updateSpaceDebug({
      boneY: {
        hips: Math.round(hipWorld.y * 1000) / 1000,
        leftFoot: Math.round(leftFootWorld.y * 1000) / 1000,
        rightFoot: Math.round(rightFootWorld.y * 1000) / 1000,
      },
      bonePos: {
        hips: hipWorld.toArray().map((v) => Math.round(v * 1000) / 1000),
        leftKnee: leftKneeWorld.toArray().map((v) => Math.round(v * 1000) / 1000),
        leftFoot: leftFootWorld.toArray().map((v) => Math.round(v * 1000) / 1000),
      },
      clipName: clipName.current,
      sitting: locomotion.sitting,
      floorSitApplied: floorSitApplied.current,
      chestJiggle: chestSpring.current.offset
        .toArray()
        .map((value) => Math.round(value * 10000) / 10000),
    })
    if (root.current) {
      updateSpaceDebug({
        rootVisualY: root.current.position.y,
        visualYaw: root.current.rotation.y,
      })
    }
  })

  if (!vrm) return <Html center><span className="text-sm text-white/80">正在加载模型…</span></Html>

  const bindZone = (key: string) => (node: THREE.Mesh | null) => {
    touchZoneRefs.current[key] = node
  }

  return (
    <RigidBody
      ref={avatarBody}
      type="kinematicPosition"
      colliders={false}
      position={[0, 0, 0]}
      canSleep={false}
    >
      <CapsuleCollider ref={avatarCollider} args={[0.52, 0.28]} position={[0, 0.8, 0]} friction={0.7} />
      <group ref={root}>
      <group ref={touchRig}>
        <primitive object={vrm.scene} />
        {attention === 'busy' && locomotion.posture === 'stand' && !isWalking && (
          <ActivityProp kind={idleKind} rightHand={rightHand} />
        )}
        {outfit.accessory === 'whale_tail' && (
          <WhaleTail reaction={reaction} idleKind={idleKind} onClick={() => onReact('tail')} />
        )}
      <group ref={touchZones}>
        <mesh
          ref={bindZone('head')}
          userData={{ reactionZone: 'head' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('head', event.point)
          }}
        >
          <sphereGeometry args={[0.19, 16, 16]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('neck')}
          userData={{ reactionZone: 'neck' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('neck', event.point)
          }}
        >
          <capsuleGeometry args={[0.1, 0.1, 6, 12]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('chest')}
          scale={[1.45, 0.9, 0.55]}
          userData={{ reactionZone: 'chest' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('chest', event.point)
          }}
        >
          <sphereGeometry args={[0.17, 18, 16]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('waist')}
          scale={[1.1, 0.6, 0.55]}
          userData={{ reactionZone: 'waist' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('waist', event.point)
          }}
        >
          <sphereGeometry args={[0.15, 16, 14]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('butt')}
          scale={[1.15, 0.85, 0.78]}
          userData={{ reactionZone: 'butt' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('butt', event.point)
          }}
        >
          <sphereGeometry args={[0.16, 16, 14]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('thigh-left')}
          scale={[0.9, 1.35, 0.9]}
          userData={{ reactionZone: 'thigh' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('thigh', event.point)
          }}
        >
          <sphereGeometry args={[0.11, 14, 12]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('thigh-right')}
          scale={[0.9, 1.35, 0.9]}
          userData={{ reactionZone: 'thigh' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('thigh', event.point)
          }}
        >
          <sphereGeometry args={[0.11, 14, 12]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('ear-left')}
          userData={{ reactionZone: 'ear' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('ear', event.point)
          }}
        >
          <sphereGeometry args={[0.08, 12, 10]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('ear-right')}
          userData={{ reactionZone: 'ear' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('ear', event.point)
          }}
        >
          <sphereGeometry args={[0.08, 12, 10]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('hand-left')}
          userData={{ reactionZone: 'hand' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('hand', event.point)
          }}
        >
          <sphereGeometry args={[0.09, 12, 10]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('hand-right')}
          userData={{ reactionZone: 'hand' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('hand', event.point)
          }}
        >
          <sphereGeometry args={[0.09, 12, 10]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
        <mesh
          ref={bindZone('tail')}
          userData={{ reactionZone: 'tail' }}
          onPointerDown={(event) => {
            event.stopPropagation()
            beginAvatarGrab('tail', event.point)
          }}
        >
          <sphereGeometry args={[0.22, 12, 10]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
      </group>
      </group>
      </group>
    </RigidBody>
  )
})

function DraggableProp({
  name,
  position,
  state,
  onInteract,
  onSettled,
  setDragging,
}: {
  name: SpaceObjectKey
  position: Vec3
  state?: string
  onInteract: () => void
  onSettled: (position: Vec3, moved: boolean) => void
  setDragging: (dragging: boolean) => void
}) {
  const body = useRef<RapierRigidBody | null>(null)
  const dragging = useRef(false)
  const moved = useRef(false)
  const startPosition = useRef(new THREE.Vector3())
  const [preview, setPreview] = useState<Vec3 | null>(null)

  useEffect(() => {
    const rigidBody = body.current
    if (!rigidBody) return
    rigidBody.userData = { label: OBJECT_LABELS[name] }
    registerGrabbable(name, rigidBody, OBJECT_LABELS[name], OBJECT_GRAB_RADII[name])
    return () => unregisterGrabbable(name)
  }, [name])

  useEffect(() => {
    const finish = () => {
      if (!dragging.current) return
      dragging.current = false
      setDragging(false)
      setPreview(null)
      const rigidBody = body.current
      if (!rigidBody) return
      rigidBody.setBodyType(BODY_DYNAMIC, true)
      rigidBody.setLinvel({ x: 0, y: 0, z: 0 }, true)
      rigidBody.setAngvel({ x: 0, y: 0, z: 0 }, true)
    }
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', finish)
    return () => {
      window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', finish)
    }
  }, [setDragging])

  const move = (event: ThreeEvent<PointerEvent>) => {
    if (!dragging.current) return
    event.stopPropagation()
    const rigidBody = body.current
    if (!rigidBody) return
    const point = new THREE.Vector3()
    const current = rigidBody.translation()
    const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -current.y)
    if (event.ray.intersectPlane(plane, point)) {
      const x = THREE.MathUtils.clamp(point.x, -2.7, 2.7)
      const z = THREE.MathUtils.clamp(point.z, -2.7, 2.7)
      moved.current =
        moved.current || Math.hypot(x - startPosition.current.x, z - startPosition.current.z) > 0.06
      rigidBody.setNextKinematicTranslation({ x, y: current.y, z })
      setPreview([x, current.y, z])
    }
  }

  return (
    <RigidBody
      ref={body}
      type="dynamic"
      colliders={false}
      position={position}
      linearDamping={0.65}
      angularDamping={1.4}
      ccd
      canSleep
      onSleep={() => {
        const current = body.current?.translation()
        if (!current) return
        updateSpaceDebug({ [`prop_${name}`]: [current.x, current.y, current.z] })
        onSettled([current.x, current.y, current.z], moved.current)
        moved.current = false
      }}
    >
      <group
        onPointerDown={(event) => {
          if (event.button !== 0 || document.pointerLockElement) return
          event.stopPropagation()
          const rigidBody = body.current
          if (!rigidBody) return
          dragging.current = true
          moved.current = false
          const current = rigidBody.translation()
          startPosition.current.set(current.x, current.y, current.z)
          rigidBody.setBodyType(BODY_KINEMATIC_POSITION, true)
          rigidBody.setNextKinematicTranslation(current)
          setDragging(true)
        }}
        onPointerMove={move}
        onPointerUp={() => {
          if (!dragging.current) {
            onInteract()
            return
          }
          const wasMoved = moved.current
          const rigidBody = body.current
          if (rigidBody) {
            rigidBody.setBodyType(BODY_DYNAMIC, true)
            rigidBody.setLinvel({ x: 0, y: 0, z: 0 }, true)
            rigidBody.setAngvel({ x: 0, y: 0, z: 0 }, true)
            if (!wasMoved) onInteract()
          }
          dragging.current = false
          setDragging(false)
          setPreview(null)
        }}
      >
        <PropCollider name={name} />
        <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -position[1] + 0.008, 0]}>
          <ringGeometry args={[0.25, 0.285, 28]} />
          <meshBasicMaterial color="#e9c58d" transparent opacity={0.34} depthWrite={false} />
        </mesh>
        {name === 'book' && (
          <group rotation={[0, -0.35, 0]}>
            <RoundedBox args={[0.5, 0.075, 0.36]} radius={0.018} smoothness={3} castShadow>
              <meshStandardMaterial color="#a94f50" roughness={0.7} />
            </RoundedBox>
            <mesh position={[0.01, 0.041, 0]}>
              <boxGeometry args={[0.43, 0.008, 0.3]} />
              <meshStandardMaterial color="#e9ddc3" roughness={0.9} />
            </mesh>
            <mesh position={[0.19, 0.052, -0.11]} rotation={[0, 0, 0.05]}>
              <boxGeometry args={[0.028, 0.012, 0.12]} />
              <meshStandardMaterial color="#d6b269" roughness={0.6} />
            </mesh>
          </group>
        )}
        {name === 'mug' && (
          <group>
            <mesh castShadow>
              <cylinderGeometry args={[0.11, 0.12, 0.24, 24]} />
              <meshStandardMaterial color="#e9c7ae" roughness={0.45} />
            </mesh>
            <mesh position={[0.13, 0, 0]}>
              <torusGeometry args={[0.075, 0.018, 8, 20]} />
              <meshStandardMaterial color="#d5ad94" roughness={0.5} />
            </mesh>
            {state !== 'empty' && (
              <mesh position={[0, 0.116, 0]}>
                <cylinderGeometry args={[0.085, 0.085, 0.012, 20]} />
                <meshStandardMaterial color="#403028" roughness={0.35} />
              </mesh>
            )}
          </group>
        )}
        {name === 'cushion' && (
          <group rotation={[0, 0, state === 'moved' ? 0.18 : 0]}>
            <RoundedBox args={[0.78, 0.24, 0.58]} radius={0.11} smoothness={4} castShadow>
              <meshStandardMaterial color="#87997d" roughness={0.95} />
            </RoundedBox>
          </group>
        )}
        {name === 'notebook' && (
          <group rotation={[0, 0.42, 0]}>
            <RoundedBox args={[0.42, 0.055, 0.31]} radius={0.014} smoothness={3} castShadow>
              <meshStandardMaterial color="#587e8f" roughness={0.75} />
            </RoundedBox>
            <mesh position={[0.01, 0.03, 0]}>
              <boxGeometry args={[0.35, 0.008, 0.25]} />
              <meshStandardMaterial color="#efe6d2" roughness={0.9} />
            </mesh>
          </group>
        )}
        {name === 'plush' && (
          <group scale={[1, state === 'squished' ? 0.68 : 1, 1]}>
            <mesh castShadow scale={[1, 0.8, 1.08]}>
              <sphereGeometry args={[0.2, 20, 14]} />
              <meshStandardMaterial color="#5b9eb6" roughness={0.85} />
            </mesh>
            <mesh castShadow position={[0.08, -0.03, -0.2]} rotation={[0.4, 0, 0.4]} scale={[0.12, 0.055, 0.06]}>
              <sphereGeometry args={[1, 12, 8]} />
              <meshStandardMaterial color="#9bd7e2" roughness={0.7} />
            </mesh>
            <mesh position={[0.07, 0.09, 0.09]}>
              <sphereGeometry args={[0.022, 8, 8]} />
              <meshStandardMaterial color="#16252b" />
            </mesh>
            <mesh position={[-0.07, 0.09, 0.09]}>
              <sphereGeometry args={[0.022, 8, 8]} />
              <meshStandardMaterial color="#16252b" />
            </mesh>
          </group>
        )}
      </group>
      {preview && (
        <group position={[preview[0] - position[0], preview[1] - position[1], preview[2] - position[2]]}>
          <mesh rotation={[-Math.PI / 2, 0, 0]}>
            <ringGeometry args={[0.28, 0.32, 32]} />
            <meshBasicMaterial color="#7dd3fc" transparent opacity={0.65} depthWrite={false} />
          </mesh>
        </group>
      )}
    </RigidBody>
  )
}

function PropCollider({ name }: { name: SpaceObjectKey }) {
  if (name === 'book') return <CuboidCollider args={[0.25, 0.038, 0.18]} friction={0.75} restitution={0.08} />
  if (name === 'mug') return <CylinderCollider args={[0.12, 0.11]} friction={0.5} restitution={0.08} />
  if (name === 'cushion') return <CuboidCollider args={[0.39, 0.12, 0.29]} friction={0.95} restitution={0.02} />
  if (name === 'notebook') return <CuboidCollider args={[0.21, 0.028, 0.155]} friction={0.75} restitution={0.06} />
  return <BallCollider args={[0.2]} friction={0.8} restitution={0.14} />
}

function Scene({
  attention,
  idleTarget,
  idleKind,
  reaction,
  moodExpression,
  emotionName,
  onReact,
  onAvatarGrabStart,
  onAvatarGrabEnd,
  onPostureReady,
  onMountComplete,
  onInteractObject,
  locomotion,
  animationClip,
  outfit,
  avatarModel,
  propStates,
  world,
  friendId,
  editMode,
  selectedObjectId,
  onSelectObject,
  onMoveObjectEnd,
  props,
  onPropSettled,
  setDragging,
  viewer,
  selfState,
  noclip,
  paused,
  onToggleNoclip,
  onLockChange,
  onGrabChange,
  touchMode,
  touchMove,
  touchVertical,
}: {
  attention: Attention
  idleTarget: Vec3
  idleKind: SpaceIdleKind
  reaction: Reaction | null
  moodExpression: string | null
  emotionName: string | null
  onReact: (reaction: Reaction) => void
  onAvatarGrabStart: (zone: Reaction) => void
  onAvatarGrabEnd: () => void
  onPostureReady: (kind: 'sit' | 'lie') => void
  onMountComplete: (kind: 'sit' | 'lie') => void
  onInteractObject: (name: SpaceObjectKey) => void
  locomotion: Locomotion
  animationClip: string | null
  outfit: SpaceOutfit
  avatarModel: string | null
  propStates: Record<string, string>
  world: WorldState
  friendId: string
  editMode: boolean
  selectedObjectId: string | null
  onSelectObject: (id: string) => void
  onMoveObjectEnd: (id: string, position: Vec3) => void
  props: PropPosition
  onPropSettled: (name: SpaceObjectKey, position: Vec3, moved: boolean) => void
  setDragging: (dragging: boolean) => void
  viewer: MutableRefObject<ViewerSnapshot>
  selfState: MutableRefObject<SpaceSelfState>
  noclip: boolean
  paused: boolean
  onToggleNoclip: () => void
  onLockChange: (locked: boolean) => void
  onGrabChange: (holding: string | null) => void
  touchMode: boolean
  touchMove: MutableRefObject<{ x: number; y: number }>
  touchVertical: MutableRefObject<number>
}) {
  return (
    <>
      {world.environment === 'white' ? (
        <WhiteRoom key="white-room" />
      ) : (
        <Room key="room" fixtures={propStates} />
      )}
      <RoomColliders />
      <Suspense
        key="space-avatar"
        fallback={<Html center><span className="text-sm text-white/70">正在把汐音搬进房间…</span></Html>}
      >
        <group position={[0, 0, 0]}>
          <Avatar
            attention={attention}
            idleTarget={idleTarget}
            idleKind={idleKind}
            reaction={reaction}
            moodExpression={moodExpression}
            emotionName={emotionName}
            locomotion={locomotion}
            animationClip={animationClip}
            outfit={outfit}
            modelUrl={avatarModel}
            worldState={world}
            propPositions={props}
            onReact={onReact}
            onAvatarGrabStart={onAvatarGrabStart}
            onAvatarGrabEnd={onAvatarGrabEnd}
            onPostureReady={onPostureReady}
            onMountComplete={onMountComplete}
            viewer={viewer}
            selfState={selfState}
          />
        </group>
      </Suspense>
      <DraggableProp
        name="book"
        position={props.book}
        state={propStates.book}
        onInteract={() => onInteractObject('book')}
        onSettled={(position, moved) => onPropSettled('book', position, moved)}
        setDragging={setDragging}
      />
      {world.objects.map((object) => (
        <WorldObjectView
          key={object.id}
          object={object}
          friendId={friendId}
          selected={selectedObjectId === object.id}
          editable={editMode}
          onSelect={() => onSelectObject(object.id)}
          onMoveEnd={(position) => onMoveObjectEnd(object.id, position)}
          setDragging={setDragging}
        />
      ))}
      <DraggableProp
        name="mug"
        position={props.mug}
        state={propStates.mug}
        onInteract={() => onInteractObject('mug')}
        onSettled={(position, moved) => onPropSettled('mug', position, moved)}
        setDragging={setDragging}
      />
      <DraggableProp
        name="cushion"
        position={props.cushion}
        state={propStates.cushion}
        onInteract={() => onInteractObject('cushion')}
        onSettled={(position, moved) => onPropSettled('cushion', position, moved)}
        setDragging={setDragging}
      />
      <DraggableProp
        name="notebook"
        position={props.notebook}
        state={propStates.notebook}
        onInteract={() => onInteractObject('notebook')}
        onSettled={(position, moved) => onPropSettled('notebook', position, moved)}
        setDragging={setDragging}
      />
      <DraggableProp
        name="plush"
        position={props.plush}
        state={propStates.plush}
        onInteract={() => onInteractObject('plush')}
        onSettled={(position, moved) => onPropSettled('plush', position, moved)}
        setDragging={setDragging}
      />
      <FreeFlyControls
        viewer={viewer}
        noclip={noclip}
        paused={paused || editMode}
        onToggleNoclip={onToggleNoclip}
        onLockChange={onLockChange}
        touchMode={touchMode}
        touchMove={touchMove}
        touchVertical={touchVertical}
      />
      <UserHand viewer={viewer} enabled={!paused && !editMode} onGrabChange={onGrabChange} />
    </>
  )
}

class SceneErrorBoundary extends Component<{ children: ReactNode; onError: (message: string) => void }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error) {
    this.props.onError(error.message)
  }

  render() {
    if (this.state.failed) {
      return (
        <div className="flex h-full items-center justify-center bg-[#111923] p-8 text-center text-sm text-white/75">
          3D 模型暂时没有加载成功。先退出再进一次，或者检查
          `frontend/public/models/Vivi.vrm` 是否还在。
        </div>
      )
    }
    return this.props.children
  }
}

function AvatarCarry({
  heldKey,
  selfState,
}: {
  heldKey: MutableRefObject<string | null>
  selfState: MutableRefObject<SpaceSelfState>
}) {
  useFrame((_, delta) => {
    const key = heldKey.current
    if (!key) return
    const item = getGrabbable(key)
    if (!item || !item.body.isValid()) {
      heldKey.current = null
      return
    }
    const hand = selfState.current.hand
    if (!hand) return
    const position = item.body.translation()
    const dx = hand[0] - position.x
    const dy = hand[1] - position.y
    const dz = hand[2] - position.z
    const distance = Math.hypot(dx, dy, dz)
    const gain = distance > 0.7 ? 9 : 6
    const speed = Math.min(4.2, distance * gain)
    const scale = distance > 0.0001 ? speed / distance : 0
    item.body.setLinvel(
      { x: dx * scale, y: dy * scale, z: dz * scale },
      true,
    )
    const angular = item.body.angvel()
    item.body.setAngvel(
      { x: angular.x * 0.82, y: angular.y * 0.82, z: angular.z * 0.82 },
      true,
    )
    const dt = Math.min(delta, 0.05)
    if (dt > 0) item.body.wakeUp()
  })
  return null
}

function PerceptionPanel({
  model,
  onClose,
}: {
  model: SpaceWorldModel | null
  onClose: () => void
}) {
  return (
    <div className="pointer-events-auto absolute right-3 top-16 z-50 flex max-h-[72dvh] w-[min(92vw,390px)] flex-col overflow-hidden rounded-2xl border border-white/15 bg-slate-950/96 shadow-2xl backdrop-blur">
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
        <div>
          <p className="text-[10px] font-semibold tracking-[0.2em] text-emerald-200/70">
            PERCEPTION
          </p>
          <p className="mt-0.5 text-sm font-semibold text-white">她此刻读到的世界</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-full border border-white/15 px-2 py-1 text-[11px] text-white/60 hover:bg-white/10"
        >
          关闭
        </button>
      </div>
      <div className="space-y-3 overflow-y-auto px-4 py-3 text-[11px] leading-relaxed text-white/75">
        {!model ? (
          <p className="text-white/45">还没有世界快照。</p>
        ) : (
          <>
            <div className="rounded-xl border border-white/10 bg-white/5 p-2.5">
              <p>{model.time}</p>
              <p>
                空间 {model.room.width.toFixed(1)} × {model.room.depth.toFixed(1)}m
                {model.self.position
                  ? ` · 她在 ${model.self.position[0].toFixed(1)}, ${model.self.position[2].toFixed(1)}`
                  : ''}
              </p>
              <p>
                {model.self.moving ? '正在走' : '停着'}
                {model.self.posture === 'sit' ? ' · 坐着' : ''}
                {model.self.posture === 'lie' ? ' · 躺着' : ''}
                {model.self.support_key ? ` · ${model.self.support_key}` : ''}
                {model.self.activity ? ` · ${model.self.activity}` : ''}
                {model.self.mood ? ` · ${model.self.mood}` : ''}
              </p>
              {model.self.holding && (
                <p>手里拿着：{model.self.holding}</p>
              )}
            </div>

            {model.user.hand_contact_zone && (
              <div className="rounded-xl border border-pink-200/20 bg-pink-200/[0.07] p-2.5">
                <p className="text-[10px] font-semibold tracking-[0.18em] text-pink-200/65">
                  TA 的手
                </p>
                <p className="mt-1 text-white/70">
                  正抓着你：
                  {BODY_ZONE_LABELS[model.user.hand_contact_zone as Reaction] ??
                    model.user.hand_contact_zone}
                  {' · '}
                  {model.user.hand_contact_kind === 'push'
                    ? '往外推'
                    : model.user.hand_contact_kind === 'pull'
                      ? '往自己身边拉'
                      : model.user.hand_contact_kind === 'drag'
                        ? '带着移动'
                        : '抓着不放'}
                  {' · '}
                  {(model.user.hand_contact_seconds ?? 0).toFixed(1)}s
                </p>
              </div>
            )}

            {model.senses && (
              <div className="rounded-xl border border-sky-200/15 bg-sky-200/[0.06] p-2.5">
                <p className="mb-1.5 text-[10px] font-semibold tracking-[0.18em] text-sky-200/60">
                  她此刻的感觉
                </p>
                <div className="space-y-1">
                  {model.senses.summary.map((line, index) => (
                    <p key={`${index}-${line}`} className="text-white/65">
                      {line}
                    </p>
                  ))}
                </div>
              </div>
            )}

            <div className="rounded-xl border border-emerald-200/15 bg-emerald-200/[0.05] p-2.5">
              <p className="text-[10px] font-semibold tracking-[0.18em] text-emerald-200/60">
                她的小世界工具
              </p>
              <p className="mt-1 text-white/60">
                space_plan · space_do · space_say
              </p>
              <p className="mt-1 text-[10px] text-white/40">
                当前可操作 {model.entities.length} 个实体；每个实体的“能做”就是她这轮可用的动作。
              </p>
            </div>

            {model.shared_state && (model.shared_state.inner_note || model.shared_state.intent) && (
              <div className="rounded-xl border border-violet-200/15 bg-violet-200/[0.06] p-2.5">
                <p className="mb-1.5 text-[10px] font-semibold tracking-[0.18em] text-violet-200/60">
                  和小世界共享的她
                </p>
                {model.shared_state.intent && (
                  <p className="text-white/60">此刻的意图：{model.shared_state.intent}</p>
                )}
                {model.shared_state.inner_note && (
                  <p className="mt-1 text-white/75">私下里想：{model.shared_state.inner_note}</p>
                )}
                {model.shared_state.open_loops.length > 0 && (
                  <p className="mt-1 text-[10px] text-white/40">
                    还惦记着：{model.shared_state.open_loops.slice(0, 3).join('；')}
                  </p>
                )}
              </div>
            )}

            {model.recent_chat?.length > 0 && (
              <div className="rounded-xl border border-white/10 bg-white/5 p-2.5">
                <p className="mb-1.5 text-[10px] font-semibold tracking-[0.18em] text-white/40">
                  你们最近说过
                </p>
                <div className="space-y-1">
                  {model.recent_chat.slice(-6).map((item, index) => (
                    <p key={`${item.at}-${index}`} className="text-white/60">
                      <span className="text-white/35">{item.role}：</span>
                      {item.content}
                    </p>
                  ))}
                </div>
              </div>
            )}

            <div>
              <p className="mb-1.5 text-[10px] font-semibold tracking-[0.18em] text-white/40">
                看得见的东西
              </p>
              <div className="space-y-1.5">
                {model.entities.map((entity) => (
                  <div
                    key={entity.key}
                    className="rounded-xl border border-white/10 bg-white/[0.04] p-2.5"
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="font-medium text-white/90">{entity.label}</span>
                      <span className="shrink-0 text-[10px] text-white/35">
                        {entity.zone.label}
                        {entity.support ? ` · ${entity.support}` : ''}
                        {entity.distance_to_me != null
                          ? ` · ${entity.distance_to_me.toFixed(1)}m`
                          : ''}
                      </span>
                    </div>
                    <p className="mt-1 text-white/55">{entity.known}</p>
                    {Object.keys(entity.state).length > 0 && (
                      <p className="mt-1 text-[10px] text-sky-200/55">
                        状态：
                        {Object.entries(entity.state)
                          .map(([key, value]) => `${key}=${String(value)}`)
                          .join(' · ')}
                      </p>
                    )}
                    <p className="mt-1 text-[10px] text-amber-200/55">
                      能做：{entity.affordances.slice(0, 6).join(' / ')}
                    </p>
                    {entity.last_event && (
                      <p className="mt-1 text-[10px] text-white/35">
                        最近：{entity.last_event}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>

            {(model.goal || model.plan.length > 0) && (
              <div className="rounded-xl border border-amber-200/15 bg-amber-200/[0.06] p-2.5">
                <p className="text-[10px] font-semibold tracking-[0.18em] text-amber-200/60">
                  她自己的目标
                </p>
                {model.goal && <p className="mt-1 text-white/80">{model.goal}</p>}
                {model.plan.length > 0 && (
                  <p className="mt-1 text-white/55">
                    {model.plan
                      .map((step, index) =>
                        index === model.plan_step ? `【${step}】` : step,
                      )
                      .join(' → ')}
                  </p>
                )}
              </div>
            )}

            {Object.keys(model.last_action_result).length > 0 && (
              <div className="rounded-xl border border-white/10 bg-white/5 p-2.5">
                <p className="text-[10px] font-semibold tracking-[0.18em] text-white/40">
                  上一条动作
                </p>
                <p className="mt-1 text-white/65">
                  {String(model.last_action_result.status ?? '')}
                  {' · '}
                  {model.last_action_result.ok === true
                    ? '完成'
                    : model.last_action_result.ok === false
                      ? '没完成'
                      : '等待结果'}
                </p>
                {model.last_action_result.detail ? (
                  <p className="mt-1 text-white/45">
                    {String(model.last_action_result.detail)}
                  </p>
                ) : null}
                {model.last_action_result.reflection ? (
                  <p className="mt-1 text-emerald-200/55">
                    她意识到：{String(model.last_action_result.reflection)}
                  </p>
                ) : null}
                {model.last_action_result.preference ? (
                  <p className="mt-1 text-amber-200/55">
                    她的偏好：{String(model.last_action_result.preference)}
                  </p>
                ) : null}
              </div>
            )}

            {model.recent_events.length > 0 && (
              <div>
                <p className="mb-1.5 text-[10px] font-semibold tracking-[0.18em] text-white/40">
                  最近发生
                </p>
                <div className="space-y-1">
                  {model.recent_events.slice(0, 6).map((event, index) => (
                    <p key={`${event.kind}-${index}`} className="text-white/50">
                      · {event.text}
                    </p>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function SpaceMessagesPanel({
  model,
  onClose,
}: {
  model: SpaceWorldModel | null
  onClose: () => void
}) {
  const chat = model?.space_chat ?? []
  const events = model?.recent_events ?? []
  return (
    <div className="pointer-events-auto absolute right-3 top-16 z-50 flex max-h-[72dvh] w-[min(92vw,410px)] flex-col overflow-hidden rounded-2xl border border-white/15 bg-slate-950/96 shadow-2xl backdrop-blur">
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
        <div>
          <p className="text-[10px] font-semibold tracking-[0.2em] text-sky-200/70">
            SMALL WORLD
          </p>
          <h2 className="mt-0.5 text-[15px] font-semibold text-white">小世界记录</h2>
          <p className="mt-0.5 text-[10px] text-white/40">
            聊天只留在这里；只有她自己认定为值得记住的事才进长期记忆。
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-full border border-white/15 px-2.5 py-1 text-[10px] text-white/60 hover:bg-white/10"
        >
          关闭
        </button>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {chat.length === 0 ? (
          <p className="rounded-xl border border-white/10 bg-white/[0.03] px-3 py-4 text-center text-[11px] text-white/40">
            这里还没有只属于小世界的对话。
          </p>
        ) : (
          <div className="space-y-2">
            {chat.map((item, index) => {
              const mine = item.role === 'TA'
              return (
                <div
                  key={`${item.at}-${index}`}
                  className={`flex ${mine ? 'justify-end' : 'justify-start'}`}
                >
                  <div
                    className={`max-w-[82%] rounded-2xl px-3 py-2 text-[11.5px] leading-relaxed ${
                      mine
                        ? 'rounded-br-sm bg-sky-300/15 text-sky-50'
                        : 'rounded-bl-sm bg-white/[0.07] text-white/85'
                    }`}
                  >
                    <div className="whitespace-pre-wrap break-words">{item.content}</div>
                    <div className="mt-1 flex items-center gap-1.5 text-[9px] text-white/35">
                      {item.role}
                      {item.emotion ? <span>· {item.emotion}</span> : null}
                      {item.proactive ? <span>· 主动</span> : null}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        )}
        {events.length > 0 && (
          <div>
            <p className="mb-1.5 text-[10px] font-semibold tracking-[0.18em] text-white/35">
              小世界里发生的事
            </p>
            <div className="space-y-1">
              {events.slice(0, 10).map((event, index) => (
                <p key={`${event.kind}-${index}`} className="text-[10.5px] leading-relaxed text-white/45">
                  · {event.text}
                </p>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function OutfitPanel({
  outfit,
  modelUrl,
  onChange,
  onModelUpload,
  onModelReset,
  onClose,
}: {
  outfit: SpaceOutfit
  modelUrl: string | null
  onChange: (slot: keyof SpaceOutfit, value: string) => void
  onModelUpload: (file: File) => void
  onModelReset: () => void
  onClose: () => void
}) {
  const modelInputRef = useRef<HTMLInputElement | null>(null)
  const labels: Record<keyof SpaceOutfit, string> = {
    top: '上衣',
    bottom: '下装',
    shoes: '鞋',
    accessory: '配饰',
  }
  return (
    <div className="pointer-events-auto absolute right-3 top-16 z-50 max-h-[82dvh] w-[min(92vw,360px)] overflow-y-auto rounded-2xl border border-pink-200/20 bg-slate-950/96 p-4 shadow-2xl backdrop-blur">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-[10px] font-semibold tracking-[0.2em] text-pink-200/70">
            WARDROBE
          </p>
          <p className="mt-0.5 text-sm font-semibold text-white">她的衣橱</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-full border border-white/15 px-2 py-1 text-[11px] text-white/60 hover:bg-white/10"
        >
          关闭
        </button>
      </div>
      <div className="mt-3 space-y-3">
        {(Object.keys(OUTFIT_OPTIONS) as (keyof SpaceOutfit)[]).map((slot) => (
          <div key={slot}>
            <p className="mb-1.5 text-[10px] font-semibold tracking-[0.16em] text-white/40">
              {labels[slot]}
            </p>
            <div className="flex flex-wrap gap-1.5">
              {OUTFIT_OPTIONS[slot].map((option) => {
                const active = outfit[slot] === option.id
                return (
                  <button
                    key={option.id}
                    type="button"
                    onClick={() => onChange(slot, option.id)}
                    className={`rounded-full border px-2.5 py-1.5 text-[11px] transition-colors ${
                      active
                        ? 'border-pink-200/70 bg-pink-200/15 text-pink-100'
                        : 'border-white/15 bg-white/5 text-white/65 hover:bg-white/10'
                    }`}
                  >
                    {option.label}
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>
      <p className="mt-3 text-[10px] leading-relaxed text-white/35">
        衣服槽位会实时改 VRM 的分离衣物材质，并持久化；她也知道你现在给她穿了什么。
      </p>
      <div className="mt-4 border-t border-white/10 pt-3">
        <p className="text-[10px] font-semibold tracking-[0.16em] text-white/40">
          3D 模型
        </p>
        <div className="mt-1.5 flex items-center gap-2">
          <button
            type="button"
            onClick={() => modelInputRef.current?.click()}
            className="rounded-full border border-sky-300/30 bg-sky-300/10 px-2.5 py-1 text-[11px] text-sky-100 hover:bg-sky-300/20"
          >
            上传 VRM
          </button>
          {modelUrl && (
            <button
              type="button"
              onClick={onModelReset}
              className="rounded-full border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-white/65 hover:bg-white/10"
            >
              恢复默认
            </button>
          )}
          <span className="truncate text-[10px] text-white/35">
            {modelUrl ? '当前使用自定义模型' : '当前使用默认模型'}
          </span>
        </div>
        <input
          ref={modelInputRef}
          type="file"
          accept=".vrm,model/gltf-binary"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0]
            event.target.value = ''
            if (file) onModelUpload(file)
          }}
        />
      </div>
    </div>
  )
}

export default function SpaceView({
  friend,
  onClose,
  variant = 'desktop',
}: SpaceViewProps) {
  const isMobile = variant === 'mobile'
  const [attention, setAttention] = useState<Attention>('busy')
  const [reaction, setReaction] = useState<Reaction | null>(null)
  const [profile, setProfile] = useState<SpaceProfile>(FALLBACK_PROFILE)
  const [moodExpression, setMoodExpression] = useState<string | null>(null)
  const [emotionExpression, setEmotionExpression] = useState<string | null>(null)
  const [emotionClip, setEmotionClip] = useState<string | null>(null)
  const [emotionName, setEmotionName] = useState<string | null>(null)
  const [awaitingReply, setAwaitingReply] = useState(false)
  const [line, setLine] = useState<string | null>(null)
  const [liveThought, setLiveThought] = useState('')
  const [dragging, setDragging] = useState(false)
  const [props, setProps] = useState<PropPosition>(() => readLayout(friend.id))
  const [failed, setFailed] = useState<string | null>(null)
  const [mode, setMode] = useState<SpaceMode>('free')
  const [layoutReady, setLayoutReady] = useState(false)
  const [manualIdle, setManualIdle] = useState<SpaceIdleKind | null>(null)
  const [autoStatus, setAutoStatus] = useState<string | null>(null)
  const [propStates, setPropStates] = useState<Record<string, string>>(DEFAULT_PROP_STATES)
  const [outfit, setOutfit] = useState<SpaceOutfit>(DEFAULT_OUTFIT)
  const [avatarModel, setAvatarModel] = useState<string | null>(null)
  const [animationClip, setAnimationClip] = useState<string | null>(null)
  const [roomDraft, setRoomDraft] = useState('')
  const [roomBusy, setRoomBusy] = useState(false)
  const [roomError, setRoomError] = useState<string | null>(null)
  const [world, setWorld] = useState<WorldState>(DEFAULT_WORLD)
  const [editMode, setEditMode] = useState(false)
  const [selectedObjectId, setSelectedObjectId] = useState<string | null>(null)
  const [editorError, setEditorError] = useState<string | null>(null)
  const [noclip, setNoclip] = useState(false)
  const [handHolding, setHandHolding] = useState<string | null>(null)
  const [paused, setPaused] = useState(!isMobile)
  const [pointerLocked, setPointerLocked] = useState(false)
  const [runtime, setRuntime] = useState<SpaceRuntime | null>(null)
  const [knowledge, setKnowledge] = useState<SpaceKnowledge[]>([])
  const [discovery, setDiscovery] = useState<SpaceDiscovery | null>(null)
  const [worldModel, setWorldModel] = useState<SpaceWorldModel | null>(null)
  const [perceptionOpen, setPerceptionOpen] = useState(false)
  const [messagesOpen, setMessagesOpen] = useState(false)
  const [outfitOpen, setOutfitOpen] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)
  const chatOpenRef = useRef(false)
  chatOpenRef.current = chatOpen
  const awaitingReplyRef = useRef(false)
  awaitingReplyRef.current = awaitingReply
  const glbInputRef = useRef<HTMLInputElement | null>(null)
  const roomInputRef = useRef<HTMLInputElement | null>(null)
  const editModeRef = useRef(false)
  editModeRef.current = editMode
  const clipTimer = useRef<number | null>(null)
  const emotionTimer = useRef<number | null>(null)
  const liveThoughtTimer = useRef<number | null>(null)
  const propStatesRef = useRef(propStates)
  propStatesRef.current = propStates
  const outfitRef = useRef(outfit)
  outfitRef.current = outfit
  const [locomotion, setLocomotion] = useState<Locomotion>({
    position: [0, 0, 0],
    yaw: 0,
    posture: 'stand',
    sitting: false,
    lying: false,
    seatHeight: 0,
    rootY: 0,
    supportKey: null,
    supportTop: 0,
    seatPoint: null,
    pendingPosture: null,
    mount: null,
    targetKey: null,
  })
  const locomotionRef = useRef(locomotion)
  locomotionRef.current = locomotion
  // 调试钩子：自动化截图/验收时把角色固定在指定位置和朝向，避免她正走到一半。
  useEffect(() => {
    ;(window as any).__YF_DEBUG_POSE__ = (yaw = 0, x = 0, z = 0) => {
      ;(window as any).__YF_DEBUG_FREEZE__ = true
      setAttention('busy')
      setManualIdle(null)
      setAutoStatus(null)
      setLocomotion({
        position: [x, 0, z],
        yaw,
        posture: 'stand',
        sitting: false,
        lying: false,
        seatHeight: 0,
        rootY: 0,
        supportKey: null,
        supportTop: 0,
        seatPoint: null,
        pendingPosture: null,
        mount: null,
        targetKey: null,
      })
    }
    return () => {
      delete (window as any).__YF_DEBUG_POSE__
    }
  }, [])
  const propsRef = useRef(props)
  propsRef.current = props
  const worldRef = useRef(world)
  worldRef.current = world
  const pausedRef = useRef(paused)
  pausedRef.current = paused
  const menuRequestedRef = useRef(false)
  const escapeUnlockAtRef = useRef(0)
  const viewer = useRef<ViewerSnapshot>(createViewerSnapshot())
  const touchMove = useRef({ x: 0, y: 0 })
  const touchVertical = useRef(0)
  const selfState = useRef<SpaceSelfState>({
    position: [0, 0, 0],
    hand: null,
    holding: null,
    posture: 'stand',
    sitting: false,
    lying: false,
    support_key: null,
    moving: false,
    still_seconds: 0,
    target: null,
    at_target: false,
  })
  const heldByAvatar = useRef<string | null>(null)
  const avatarContactRef = useRef(false)
  const deliberationBusy = useRef(false)
  const curiosityRef = useRef(profile.attention.curiosity)
  curiosityRef.current = profile.attention.curiosity
  const moodRef = useRef<string | null>(moodExpression)
  moodRef.current = moodExpression
  const userBusyUntil = useRef(0)
  const pendingActionResult = useRef<{
    id: string
    kind: SpaceAction['kind']
    target: string | null
    startedAt: number
    deadline: number
    hardDeadline: number
    detail: string
    forceOk: boolean | null
  } | null>(null)
  const motionLockRef = useRef<{
    kind: SpaceAction['kind']
    target: string | null
    until: number
  } | null>(null)
  const attentionTimer = useRef<number | null>(null)
  const reactionTimer = useRef<number | null>(null)
  const messageTimer = useRef<number | null>(null)
  const activityKind = manualIdle ?? 'stretch'
  const activity = {
    label: manualIdle ? `她在${idleLabel(manualIdle)}` : '',
    kind: activityKind,
    target: IDLE_TARGETS[activityKind] ?? IDLE_TARGETS.window,
    focus: activityKind === 'read' ? 'book' : undefined,
  }
  const activityRef = useRef(activity)
  activityRef.current = activity
  const logEvent = useCallback(
    (body: {
      kind: string
      target?: string | null
      detail?: Record<string, unknown>
      notable?: boolean
    }) => {
      void api.logSpaceEvent(friend.id, body).catch(() => undefined)
    },
    [friend.id],
  )

  const viewerPayload = useCallback(
    () => ({
      camera: viewer.current.camera,
      look: viewer.current.look,
      hand: viewer.current.hand,
      hand_active: viewer.current.handActive,
      hand_holding: viewer.current.handHolding,
      hand_contact_zone: viewer.current.handContactZone,
      hand_contact_kind: viewer.current.handContactKind,
      hand_contact_speed: viewer.current.handContactSpeed,
      hand_contact_seconds: viewer.current.handContactSeconds,
      last_moved_at: viewer.current.lastMovedAt,
    }),
    [],
  )
  const selfPayload = useCallback(
    (): SpaceSelfState => ({
      position: [...selfState.current.position],
      hand: selfState.current.hand ? [...selfState.current.hand] : null,
      holding: heldByAvatar.current,
      posture: selfState.current.posture ?? 'stand',
      sitting: selfState.current.sitting ?? false,
      lying: selfState.current.lying ?? false,
      support_key: selfState.current.support_key ?? null,
      moving: selfState.current.moving,
      still_seconds: selfState.current.still_seconds,
      target: selfState.current.target,
      at_target: selfState.current.at_target,
    }),
    [],
  )

  const requestMouseLook = useCallback(() => {
    if (isMobile) {
      setPaused(false)
      return
    }
    const canvas = document.getElementById('yf-space-canvas') as HTMLCanvasElement | null
    if (!canvas) return
    menuRequestedRef.current = false
    const result = canvas.requestPointerLock()
    if (result && typeof result.catch === 'function') {
      void result.catch(() => setPaused(true))
    }
  }, [isMobile])

  const closeRoomChat = useCallback(() => {
    chatOpenRef.current = false
    setChatOpen(false)
    setRoomDraft('')
    setRoomError(null)
    if (isMobile) {
      setPaused(false)
      return
    }
    requestMouseLook()
  }, [isMobile, requestMouseLook])

  const toggleEditMode = useCallback(() => {
    const next = !editModeRef.current
    setEditMode(next)
    setSelectedObjectId(null)
    setPaused(true)
    menuRequestedRef.current = true
    if (document.pointerLockElement) document.exitPointerLock()
    if (next) {
      // 编辑房间时先让她站到中间，避免她正坐在床/靠垫上被地板挡住。
      setLocomotion({
        position: [0, 0, 0],
        yaw: 0,
        posture: 'stand',
        sitting: false,
        lying: false,
        seatHeight: 0,
        rootY: 0,
        supportKey: null,
        supportTop: 0,
        seatPoint: null,
        pendingPosture: null,
        mount: null,
        targetKey: null,
      })
      setManualIdle(null)
      setAutoStatus(null)
    }
  }, [])

  useEffect(() => {
    updateSpaceDebug({
      noclip,
      handHolding,
      friendId: friend.id,
      paused,
      pointerLocked,
      chatOpen,
      discoveryScore: discovery?.score ?? 0,
      discoveryUnderstood: discovery?.understood ?? 0,
      pendingAsk: discovery?.pending_ask?.target ?? null,
      attention: runtime?.attention ?? 'busy',
      knowledgeCount: knowledge.filter((item) => item.status === 'understood').length,
      emotion: emotionName,
      emotionClip: animationClip ?? emotionClip,
      awaitingReply,
      posture: locomotion.posture,
      supportKey: locomotion.supportKey,
      supportTop: locomotion.supportTop,
      rootY: locomotion.rootY,
    })
  }, [
    animationClip,
    awaitingReply,
    chatOpen,
    discovery,
    emotionClip,
    emotionName,
    friend.id,
    handHolding,
    knowledge,
    locomotion,
    noclip,
    paused,
    pointerLocked,
    runtime,
  ])

  useEffect(() => {
    if (chatOpen) roomInputRef.current?.focus()
  }, [chatOpen])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName?.toLowerCase()
      if (event.key === 'Escape' && chatOpenRef.current) {
        event.preventDefault()
        event.stopPropagation()
        closeRoomChat()
        return
      }
      if (tag === 'input' || tag === 'textarea') return
      if (event.key === 'Escape') {
        if (document.pointerLockElement) {
          // 第一次 Esc 只让浏览器释放鼠标，仍保持正常操作状态；
          // 再按一次 Esc 才打开鼠标菜单。
          escapeUnlockAtRef.current = performance.now()
          return
        }
        if (!pausedRef.current) {
          if (performance.now() - escapeUnlockAtRef.current < 220) return
          event.preventDefault()
          setPaused(true)
        }
        return
      }
      if ((event.key === 't' || event.key === 'T') && !editMode) {
        event.preventDefault()
        if (document.pointerLockElement) document.exitPointerLock()
        setChatOpen(true)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [closeRoomChat, editMode])

  useEffect(() => {
    let alive = true
    setLayoutReady(false)
    void api
      .getSpace(friend.id)
      .then((result) => {
        if (!alive) return
        setMode(result.state.mode)
        setPropStates({ ...DEFAULT_PROP_STATES, ...result.state.prop_states })
        setOutfit({
          ...DEFAULT_OUTFIT,
          ...(result.state.appearance?.outfit ?? {}),
        })
        setAvatarModel(result.state.appearance?.model ?? null)
        setWorld(result.state.world ?? DEFAULT_WORLD)
        setRuntime(result.runtime ?? null)
        setKnowledge(result.knowledge ?? [])
        setWorldModel(result.world_model ?? null)
        if (result.runtime) {
          const intent = result.runtime.intent as {
            kind?: string
            target?: string
            label?: string
            reason?: string
          }
          setDiscovery({
            score: result.runtime.discovery_score,
            understood: (result.knowledge ?? []).filter((item) => item.status === 'understood')
              .length,
            pending_ask: intent.kind === 'ask' ? intent : null,
            wonder_said: result.runtime.wonder_said,
          })
        } else {
          setDiscovery(null)
        }
        const serverLayout = mergeServerLayout(result.state.layout)
        const serverIsDefault = (Object.keys(DEFAULT_PROPS) as (keyof PropPosition)[]).every(
          (key) => samePosition(serverLayout[key], DEFAULT_PROPS[key]),
        )
        const localLayout = readLayout(friend.id)
        const localIsDefault = (Object.keys(DEFAULT_PROPS) as (keyof PropPosition)[]).every(
          (key) => samePosition(localLayout[key], DEFAULT_PROPS[key]),
        )
        if (serverIsDefault && !localIsDefault) {
          // 第一次接 DB：把旧的 localStorage 摆位搬上去
          setProps(localLayout)
          void api
            .patchSpace(friend.id, {
              layout: localLayout as unknown as Record<string, [number, number, number]>,
            })
            .catch(() => undefined)
        } else {
          setProps(serverLayout)
        }
        setLayoutReady(true)
      })
      .catch(() => {
        if (alive) setLayoutReady(true)
      })
    return () => {
      alive = false
    }
  }, [friend.id])

  useEffect(() => {
    localStorage.setItem(`${STORAGE_PREFIX}${friend.id}`, JSON.stringify(props))
    if (!layoutReady) return
    const timer = window.setTimeout(() => {
      void api
        .patchSpace(friend.id, {
          layout: props as unknown as Record<string, [number, number, number]>,
        })
        .catch(() => undefined)
    }, 600)
    return () => window.clearTimeout(timer)
  }, [friend.id, props, layoutReady])

  useEffect(() => {
    logEvent({ kind: 'enter' })
    return () => {
      logEvent({ kind: 'leave', notable: false })
    }
  }, [friend.id, logEvent])

  useEffect(() => {
    const key = `${PROFILE_STORAGE_PREFIX}${friend.id}.${friend.updated_at}`
    try {
      const cached = localStorage.getItem(key)
      if (cached) {
        const parsed = JSON.parse(cached) as SpaceProfile
        if (
          parsed?.version === 3 &&
          parsed.zones &&
          parsed.objects &&
          parsed.idle?.length > 0 &&
          parsed.notice?.lines?.length > 0 &&
          parsed.call?.lines?.length > 0
        ) {
          setProfile(parsed)
          return
        }
      }
    } catch {
      // 缓存坏了就重新生成
    }
    let cancelled = false
    void api
      .buildSpaceProfile(friend.id)
      .then((result) => {
        if (cancelled || !result.profile || result.profile.version !== 3) return
        setProfile(result.profile)
        try {
          localStorage.setItem(key, JSON.stringify(result.profile))
        } catch {
          // 本地缓存失败不影响这次使用
        }
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [friend.id, friend.updated_at])

  useEffect(() => {
    if (attention !== 'notice' && attention !== 'engaged') return
    if (attentionTimer.current !== null) window.clearTimeout(attentionTimer.current)
    attentionTimer.current = window.setTimeout(
      () => setAttention('busy'),
      attention === 'engaged'
        ? 12000
        : 3000 + Math.round(profile.attention.patience * 3000),
    )
    return () => {
      if (attentionTimer.current !== null) window.clearTimeout(attentionTimer.current)
    }
  }, [attention, profile.attention.patience])

  useEffect(() => {
    return () => {
      if (reactionTimer.current !== null) window.clearTimeout(reactionTimer.current)
      if (messageTimer.current !== null) window.clearTimeout(messageTimer.current)
      if (clipTimer.current !== null) window.clearTimeout(clipTimer.current)
      if (emotionTimer.current !== null) window.clearTimeout(emotionTimer.current)
      if (liveThoughtTimer.current !== null) window.clearTimeout(liveThoughtTimer.current)
    }
  }, [])

  const showMomentary = (text: string, expression: string | null, duration: number) => {
    setLine(text)
    setMoodExpression(expression)
    if (messageTimer.current !== null) window.clearTimeout(messageTimer.current)
    messageTimer.current = window.setTimeout(() => {
      setMoodExpression(null)
      setLine(null)
    }, duration)
  }

  const setTempClip = useCallback((name: string, durationMs: number) => {
    if (clipTimer.current !== null) window.clearTimeout(clipTimer.current)
    setAnimationClip(name)
    clipTimer.current = window.setTimeout(() => setAnimationClip(null), durationMs)
  }, [])

  const clearTempClip = useCallback(() => {
    if (clipTimer.current !== null) window.clearTimeout(clipTimer.current)
    clipTimer.current = null
    setAnimationClip(null)
  }, [])

  const playEmotion = useCallback(
    (emotion: string, persistent = false) => {
      const style = EMOTION_STYLES[emotion]
      if (!style) return
      if (emotionTimer.current !== null) window.clearTimeout(emotionTimer.current)
      setEmotionExpression(style.expression)
      setEmotionName(emotion)
      // 抓着她时保持原动画，只让表情变化；否则 Dance/Jump/Crouch 这类
      // 临时 clip 会在抓取中途把她的姿势猛地换掉。
      if (avatarContactRef.current) {
        emotionTimer.current = window.setTimeout(
          () => {
            setEmotionExpression(null)
            setEmotionName(null)
          },
          style.duration + 350,
        )
        return
      }
      if (persistent) {
        setEmotionClip(style.holdClip)
        clearTempClip()
        return
      }
      setEmotionClip(null)
      setTempClip(style.clip, style.duration)
      emotionTimer.current = window.setTimeout(
        () => {
          setEmotionExpression(null)
          setEmotionName(null)
        },
        style.duration + 350,
      )
    },
    [clearTempClip, setTempClip],
  )

  const clearEmotion = useCallback(() => {
    if (emotionTimer.current !== null) {
      window.clearTimeout(emotionTimer.current)
      emotionTimer.current = null
    }
    setEmotionExpression(null)
    setEmotionClip(null)
    setEmotionName(null)
  }, [])

  const handlePostureReady = useCallback((kind: 'sit' | 'lie') => {
    setLocomotion((prev) => {
      if (prev.pendingPosture !== kind) return prev
      const latest = resolvePostureTarget(
        worldRef.current,
        kind,
        prev.supportKey,
        selfState.current.position,
        propsRef.current,
      )
      if (!latest.ok || !latest.surface) {
        // 到达后支撑面已经不存在 / 不再可坐：绝不拿旧高度硬坐。
        // 宁可站起来，也不在空中摆一个坐姿。
        return {
          ...prev,
          posture: 'stand',
          sitting: false,
          lying: false,
          rootY: 0,
          supportKey: null,
          supportTop: 0,
          seatPoint: null,
          pendingPosture: null,
          mount: null,
          targetKey: null,
        }
      }
      const surface = latest.surface
      const supportTop = surface?.top ?? prev.supportTop
      const seatPoint = surface
        ? ([surface.center[0], surface.top, surface.center[2]] as Vec3)
        : prev.seatPoint
      const mountTarget = seatPoint ?? prev.position
      const from: Vec3 = [...selfState.current.position]
      return {
        ...prev,
        position: mountTarget,
        yaw: latest.facingYaw ?? prev.yaw,
        posture: 'stand',
        sitting: false,
        lying: false,
        supportTop,
        seatPoint,
        rootY: 0,
        pendingPosture: null,
        mount: {
          kind,
          startedAt: performance.now(),
          // 游戏式坐具：她已经走到可交互边缘后，直接对齐到预设座标。
          // 不再让角色碰撞体横穿/推挤椅子；过高的“攀爬”留给正式动画系统。
          duration: kind === 'lie' ? 420 : 280,
          from,
          to: mountTarget,
          rootStartY: 0,
          rootEndY: postureRootY(supportTop, kind),
          faceYaw: latest.facingYaw ?? prev.yaw,
        },
      }
    })
  }, [])

  const handleMountComplete = useCallback((kind: 'sit' | 'lie') => {
    setLocomotion((prev) => {
      if (!prev.mount || prev.mount.kind !== kind) return prev
      return {
        ...prev,
        mount: null,
        posture: kind,
        sitting: kind === 'sit',
        lying: kind === 'lie',
        rootY: prev.mount.rootEndY,
        yaw: prev.mount.faceYaw,
        position: prev.mount.to,
        pendingPosture: null,
      }
    })
  }, [])

  useEffect(() => {
    if (
      locomotion.posture === 'stand' ||
      !locomotion.supportKey ||
      locomotion.supportKey === 'floor'
    ) {
      return
    }
    const liveSurfaces = buildSurfaces(world, props)
    const surface = liveSurfaces.find(
      (item) => item.key === locomotion.supportKey,
    )
    const [x, , z] = selfState.current.position
    if (surface && pointInSurface(surface, x, z)) return
    // 家具被拖走、删除或改到别处：她不能再保持那个坐姿。
    setLocomotion((prev) => {
      if (prev.posture === 'stand') return prev
      return {
        ...prev,
        position: [x, groundHeightAt(liveSurfaces, x, z), z],
        posture: 'stand',
        sitting: false,
        lying: false,
        rootY: 0,
        supportKey: null,
        supportTop: 0,
        seatPoint: null,
        mount: null,
        pendingPosture: null,
        targetKey: null,
      }
    })
  }, [locomotion.posture, locomotion.supportKey, props, world])

  useEffect(() => {
    const intent = runtime?.intent || {}
    const waiting = intent.kind === 'awaiting_reply'
    const wasWaiting = awaitingReplyRef.current
    awaitingReplyRef.current = waiting
    setAwaitingReply(waiting)
    if (waiting) {
      if (messageTimer.current !== null) {
        window.clearTimeout(messageTimer.current)
        messageTimer.current = null
      }
      setLine(String(intent.line || '').trim() || null)
      playEmotion(String(intent.emotion || moodExpression || 'curious'), true)
    } else if (wasWaiting) {
      setLine(null)
      clearEmotion()
    }
  }, [clearEmotion, playEmotion, runtime])

  const applyAction = useCallback(
    (action: SpaceAction, priority: 'auto' | 'user' = 'auto') => {
      const now = performance.now()
      if (editModeRef.current) return
      if (avatarContactRef.current) return
      if (
        priority === 'auto' &&
        (pendingActionResult.current ||
          (motionLockRef.current && now < motionLockRef.current.until))
      ) {
        // 自主循环不能覆盖用户刚下达的动作，也不能插队正在执行的动作。
        return
      }
      const movementKinds = ['approach', 'go_to', 'sit', 'lie']
      const isMovement = movementKinds.includes(action.kind)
      const postureResolution =
        action.kind === 'sit' || action.kind === 'lie'
          ? resolvePostureTarget(
              worldRef.current,
              action.kind,
              action.target,
              selfState.current.position,
              propsRef.current,
            )
          : null
      const activeLock = motionLockRef.current

      if (
        (action.kind === 'sit' || action.kind === 'lie') &&
        postureResolution?.ok &&
        postureResolution.surface &&
        locomotionRef.current.posture === action.kind &&
        locomotionRef.current.supportKey === postureResolution.surface.key
      ) {
        // 已经在这个支撑面上，不要再重新走一遍、再坐一次。
        return
      }

      if (activeLock && now < activeLock.until) {
        if (
          isMovement &&
          (activeLock.target === (action.target ?? null) ||
            activeLock.target === postureResolution?.surface?.key)
        ) {
          // 自由深思会每隔一两秒重复同一件事；移动中的重复提议不能打断自己。
          return
        }
        if (
          !isMovement &&
          (action.kind === 'wait' || action.kind === 'idle')
        ) {
          // “等一等/发呆”不能把正在进行的走路或爬上椅子打断。
          return
        }
      }

      const previousPending = pendingActionResult.current
      if (previousPending) {
        pendingActionResult.current = null
        void api
          .reportSpaceActionResult(
            friend.id,
            previousPending.id,
            false,
            '被新的动作打断',
          )
          .catch(() => undefined)
      }
      setAutoStatus(null)
      setManualIdle(null)
      setAttention((prev) => (prev === 'engaged' ? prev : 'busy'))
      if (action.kind === 'wait') {
        const [x, y, z] = selfState.current.position
        setLocomotion((prev) => ({
          ...prev,
          position: [x, y, z],
          pendingPosture: null,
          mount: null,
        }))
      }
      if (
        isMovement &&
        ((action.kind !== 'sit' && action.kind !== 'lie') || postureResolution?.ok)
      ) {
        const lockTarget =
          action.kind === 'sit' || action.kind === 'lie'
            ? (postureResolution?.surface?.key ?? action.target ?? null)
            : (action.target ?? null)
        const plannedMs =
          Math.max(0.6, Number(action.duration_seconds) || 4) * 1000
        motionLockRef.current = {
          kind: action.kind,
          target: lockTarget,
          until: now + Math.max(6000, plannedMs + 4500),
        }
      }
      setLocomotion((prev) => {
        if (action.kind === 'approach') {
          const targetKey = action.target ?? ''
          const prop = propsRef.current[targetKey as SpaceObjectKey]
          const worldObject = worldRef.current.objects.find((item) => item.id === targetKey)
          const hand = viewer.current.hand
          const targetPosition: Vec3 | null = prop
            ? [prop[0], 0, prop[2]]
            : worldObject
              ? [worldObject.position[0], 0, worldObject.position[2]]
              : targetKey === 'user' || !targetKey
                ? [hand[0], 0, hand[2]]
                : null
          if (!targetPosition) return prev
          const dx = targetPosition[0] - prev.position[0]
          const dz = targetPosition[2] - prev.position[2]
          const distance = Math.hypot(dx, dz)
          const standoff = distance > 0.9 ? 0.72 : Math.max(0, distance - 0.05)
          const destination: Vec3 = [
            targetPosition[0] - (distance > 0.001 ? (dx / distance) * standoff : 0),
            0,
            targetPosition[2] - (distance > 0.001 ? (dz / distance) * standoff : 0),
          ]
          return {
            position: destination,
            yaw: Math.atan2(
              targetPosition[0] - destination[0],
              targetPosition[2] - destination[2],
            ),
            posture: 'stand',
            sitting: false,
            lying: false,
            seatHeight: 0,
            rootY: 0,
            supportKey: null,
            supportTop: 0,
            seatPoint: null,
            pendingPosture: null,
            mount: null,
            targetKey,
          }
        }
        if (action.kind === 'sit' || action.kind === 'lie') {
          if (!postureResolution?.ok || !postureResolution.surface || !postureResolution.standPoint) {
            const [x, y, z] = selfState.current.position
            return {
              ...prev,
              position: [x, y, z],
              posture: 'stand',
              sitting: false,
              lying: false,
              seatHeight: 0,
              rootY: 0,
              supportKey: null,
              supportTop: 0,
              seatPoint: null,
              pendingPosture: null,
              mount: null,
              targetKey: null,
            }
          }
          const surface = postureResolution.surface
          if (surface.kind === 'floor') {
            const [x, , z] = prev.position
            const floorY = groundHeightAt(
              buildSurfaces(worldRef.current, propsRef.current),
              x,
              z,
            )
            return {
              ...prev,
              position: [x, floorY, z],
              posture: action.kind,
              sitting: action.kind === 'sit',
              lying: action.kind === 'lie',
              seatHeight: floorY,
              rootY: postureRootY(floorY, action.kind),
              supportKey: surface.key,
              supportTop: floorY,
              seatPoint: [x, floorY, z],
              pendingPosture: null,
              mount: null,
              targetKey: surface.key,
            }
          }
          const standPoint = postureResolution.standPoint
          return {
            ...prev,
            position: standPoint,
            yaw: Math.atan2(
              surface.center[0] - standPoint[0],
              surface.center[2] - standPoint[2],
            ),
            posture: 'stand',
            sitting: false,
            lying: false,
            seatHeight: 0,
            rootY: 0,
            supportKey: surface.key,
            supportTop: surface.top,
            seatPoint: [
              surface.center[0],
              groundHeightAt(
                buildSurfaces(worldRef.current, propsRef.current),
                surface.center[0],
                surface.center[2],
              ),
              surface.center[2],
            ],
            pendingPosture: action.kind,
            mount: null,
            targetKey: surface.key,
          }
        }
        if (action.kind !== 'go_to') return prev
        const anchor = ANCHORS[action.target ?? 'center'] ?? ANCHORS.center
        const yaw = Math.atan2(anchor[0] - prev.position[0], anchor[2] - prev.position[2])
        return {
          ...prev,
          position: [
            anchor[0],
            groundHeightAt(
              buildSurfaces(worldRef.current, propsRef.current),
              anchor[0],
              anchor[2],
            ),
            anchor[2],
          ],
          yaw,
          posture: 'stand',
          sitting: false,
          lying: false,
          seatHeight: 0,
          rootY: 0,
          supportKey: null,
          supportTop: 0,
          seatPoint: null,
          pendingPosture: null,
          mount: null,
          targetKey: action.target ?? null,
        }
      })
      if (action.kind === 'use' || action.kind === 'arrange' || action.kind === 'idle') {
        const mapped = ACTION_IDLE[action.target ?? '']
        if (mapped) setManualIdle(mapped)
      }
      const target = action.target ?? ''
      let forceOk: boolean | null = null
      let resultDetail = action.text || describeAction(action)
      let resultTarget = action.target
      if ((action.kind === 'sit' || action.kind === 'lie') && !postureResolution?.ok) {
        forceOk = false
        resultDetail = postureResolution?.reason || '那里没有可用的支撑面'
      } else if (
        (action.kind === 'sit' || action.kind === 'lie') &&
        postureResolution?.surface
      ) {
        resultTarget = postureResolution.surface.key
        resultDetail = `${action.kind === 'sit' ? '坐下' : '躺下'}：${postureResolution.surface.label}`
      }
      const targetGrabbable = target ? getGrabbable(target) : null
      if (action.kind === 'pick_up') {
        if (!targetGrabbable) {
          forceOk = false
          resultDetail = `她够不到${OBJECT_LABELS[target as SpaceObjectKey] ?? '那样东西'}`
        } else {
          const bodyPosition = targetGrabbable.body.translation()
          const selfPosition = selfState.current.position
          const reach = Math.hypot(
            bodyPosition.x - selfPosition[0],
            bodyPosition.z - selfPosition[2],
          )
          if (reach > 1.8) {
            forceOk = false
            resultDetail = `离${OBJECT_LABELS[target as SpaceObjectKey] ?? '那样东西'}太远了`
          } else {
            const currentlyHeld = heldByAvatar.current
            if (currentlyHeld && currentlyHeld !== target) {
              const previous = getGrabbable(currentlyHeld)
              previous?.body.setGravityScale(1, true)
            }
            heldByAvatar.current = target
            targetGrabbable.body.wakeUp()
            targetGrabbable.body.setGravityScale(0, true)
            targetGrabbable.body.setAngvel({ x: 0, y: 0, z: 0 }, true)
            forceOk = true
            resultDetail = `她拿起了${OBJECT_LABELS[target as SpaceObjectKey] ?? targetGrabbable.label}`
          }
        }
      } else if (action.kind === 'put_down') {
        const releaseKey = heldByAvatar.current ?? (targetGrabbable ? target : null)
        resultTarget = releaseKey
        const released = releaseKey ? getGrabbable(releaseKey) : null
        if (released) {
          released.body.setGravityScale(1, true)
          const velocity = released.body.linvel()
          released.body.setLinvel(
            { x: velocity.x * 0.25, y: -0.2, z: velocity.z * 0.25 },
            true,
          )
          forceOk = true
          resultDetail = `她放下了${OBJECT_LABELS[releaseKey as SpaceObjectKey] ?? released.label}`
        } else {
          forceOk = false
          resultDetail = '她手里没有可以放下的东西'
        }
        heldByAvatar.current = null
      } else if (action.kind === 'push') {
        if (!targetGrabbable) {
          forceOk = false
          resultDetail = '她够不到要推的东西'
        } else {
          const position = targetGrabbable.body.translation()
          const selfPosition = selfState.current.position
          const dx = position.x - selfPosition[0]
          const dz = position.z - selfPosition[2]
          const distance = Math.max(0.001, Math.hypot(dx, dz))
          if (distance > 2.2) {
            forceOk = false
            resultDetail = '离要推的东西太远了'
          } else {
            const mass = targetGrabbable.body.mass?.() ?? 1
            const strength = Math.max(0.2, Math.min(3, action.strength ?? 1))
            targetGrabbable.body.applyImpulse(
              {
                x: (dx / distance) * 2.4 * strength * mass,
                y: 0.45 * strength * mass,
                z: (dz / distance) * 2.4 * strength * mass,
              },
              true,
            )
            forceOk = true
            resultDetail = `她推了推${OBJECT_LABELS[target as SpaceObjectKey] ?? targetGrabbable.label}`
          }
        }
      }
      if (forceOk === false) setAutoStatus(resultDetail)
      const currentProps = propStatesRef.current
      let nextProps = currentProps
      if (action.kind === 'set_state' && target in OBJECT_LABELS) {
        const state = action.state ?? {}
        const next = { ...currentProps }
        if (target === 'book' || target === 'notebook') {
          if (typeof state.open === 'boolean') next[target] = state.open ? 'open' : 'closed'
        } else if (target === 'mug') {
          if (typeof state.full === 'boolean') next.mug = state.full ? 'full' : 'empty'
        } else if (target === 'plush') {
          if (typeof state.squished === 'boolean') {
            next.plush = state.squished ? 'squished' : 'normal'
          }
        } else if (target === 'cushion') {
          if (typeof state.moved === 'boolean') {
            next.cushion = state.moved ? 'moved' : 'normal'
          }
        }
        if (typeof state.state === 'string' && state.state.trim()) {
          next[target as SpaceObjectKey] = state.state.trim()
        }
        nextProps = next
      } else if (action.kind === 'set_state' && FIXTURE_KEYS.has(target)) {
        const state = action.state ?? {}
        const next = { ...currentProps }
        if (target === 'room_light') {
          if (typeof state.on === 'boolean') next.room_light = state.on ? 'on' : 'off'
        } else if (typeof state.open === 'boolean') {
          next[target] = state.open ? 'open' : 'closed'
        }
        if (typeof state.state === 'string' && state.state.trim()) {
          next[target] = state.state.trim()
        }
        nextProps = next
      } else if (action.kind === 'set_state' && target === 'outfit') {
        const state = action.state ?? {}
        const nextOutfit = { ...outfitRef.current }
        for (const slot of ['top', 'bottom', 'shoes', 'accessory'] as const) {
          const value = state[slot]
          if (typeof value === 'string' && value.trim()) {
            nextOutfit[slot] = value.trim()
          }
        }
        setOutfit(nextOutfit)
        void api
          .patchSpace(friend.id, { appearance: { outfit: nextOutfit } })
          .catch(() => undefined)
      } else if (action.kind === 'set_state') {
        const worldObject = worldRef.current.objects.find((item) => item.id === target)
        if (worldObject) {
          const nextWorld = {
            ...worldRef.current,
            objects: worldRef.current.objects.map((item) =>
              item.id === target
                ? { ...item, state: { ...(item.state ?? {}), ...(action.state ?? {}) } }
                : item,
            ),
          }
          setWorld(nextWorld)
          void api.patchSpace(friend.id, { world: nextWorld }).catch(() => undefined)
        }
      } else if (action.kind === 'use') {
        if (target === 'book' || target === 'notebook') {
          nextProps = { ...currentProps, [target]: 'open' }
        } else if (target === 'mug') {
          nextProps = { ...currentProps, mug: 'empty' }
        } else if (target === 'plush') {
          nextProps = { ...currentProps, plush: 'squished' }
        }
      } else if (action.kind === 'arrange' && target === 'cushion') {
        nextProps = { ...currentProps, cushion: 'moved' }
      }
      if (nextProps !== currentProps) {
        setPropStates(nextProps)
        void api.patchSpace(friend.id, { prop_states: nextProps }).catch(() => undefined)
      }
      if (action.kind === 'use') {
        setTempClip(target === 'mug' ? 'Interact' : 'PickUp_Table', 1800)
      } else if (action.kind === 'pick_up' || action.kind === 'put_down') {
        setTempClip(action.kind === 'pick_up' ? 'PickUp_Table' : 'Interact', 1600)
      } else if (action.kind === 'push') {
        setTempClip('Interact', 1200)
      } else if (action.kind === 'arrange' || action.kind === 'set_state') {
        setTempClip('Interact', 2000)
      } else {
        clearTempClip()
      }
      void api
        .patchSpace(friend.id, {
          current_action: action as unknown as Record<string, unknown>,
        })
        .catch(() => undefined)
      if (action.id) {
        const requested = Math.max(0.6, Number(action.duration_seconds) || 4)
        let expectedSeconds = requested
        if (isMovement) {
          const start = selfState.current.position
          let travel = 0
          if (
            (action.kind === 'sit' || action.kind === 'lie') &&
            postureResolution?.surface &&
            postureResolution.standPoint
          ) {
            const stand = postureResolution.standPoint
            const surface = postureResolution.surface
            travel =
              Math.hypot(stand[0] - start[0], stand[2] - start[2]) +
              Math.hypot(
                surface.center[0] - stand[0],
                surface.center[2] - stand[2],
              )
            expectedSeconds = Math.max(
              expectedSeconds,
              travel / 1.62 + (action.kind === 'lie' ? 2.6 : 2.2),
            )
          } else if (action.kind === 'go_to') {
            const anchor = ANCHORS[action.target ?? 'center'] ?? ANCHORS.center
            travel = Math.hypot(anchor[0] - start[0], anchor[2] - start[2])
            expectedSeconds = Math.max(expectedSeconds, travel / 1.62 + 1.5)
          } else if (action.kind === 'approach') {
            const targetKey = action.target ?? ''
            const prop = propsRef.current[targetKey as SpaceObjectKey]
            const worldObject = worldRef.current.objects.find(
              (item) => item.id === targetKey,
            )
            const hand = viewer.current.hand
            const destination = prop
              ? [prop[0], 0, prop[2]]
              : worldObject
                ? [worldObject.position[0], 0, worldObject.position[2]]
                : targetKey === 'user' || !targetKey
                  ? [hand[0], 0, hand[2]]
                  : null
            if (destination) {
              travel = Math.hypot(destination[0] - start[0], destination[2] - start[2])
              expectedSeconds = Math.max(expectedSeconds, travel / 1.62 + 1.3)
            }
          }
        }
        const startedAt = performance.now()
        const deadlineMs = Math.min(expectedSeconds, 30) * 1000
        pendingActionResult.current = {
          id: action.id,
          kind: action.kind,
          target: resultTarget,
          startedAt,
          deadline: startedAt + deadlineMs,
          hardDeadline: startedAt + Math.max(deadlineMs + 7000, 18000),
          detail: resultDetail,
          forceOk,
        }
      }
    },
    [clearTempClip, friend.id, setTempClip, viewer],
  )

  const handleAvatarGrabStart = useCallback(
    (zone: Reaction) => {
      avatarContactRef.current = true
      userBusyUntil.current = Date.now() + 1200
      setAutoStatus(`你正抓着她 · ${BODY_ZONE_LABELS[zone] ?? '身体'}`)
      // 触碰不是“叫她立正”的开关。她正在坐、正在走、正在看书都继续，
      // 只有她自己的深思/触碰反应确实决定改变动作时才改。
    },
    [],
  )

  const handleAvatarGrabEnd = useCallback(() => {
    avatarContactRef.current = false
    userBusyUntil.current = Date.now() + 1400
    setAutoStatus(null)
    if (selfState.current.position[1] > 0.35) {
      setLocomotion((prev) =>
        prev.posture === 'stand'
          ? prev
          : {
              ...prev,
              posture: 'stand',
              sitting: false,
              lying: false,
              rootY: 0,
              supportKey: null,
              supportTop: 0,
              seatPoint: null,
              mount: null,
              pendingPosture: null,
              targetKey: null,
            },
      )
    }
  }, [])

  const applyActionRef = useRef(applyAction)
  applyActionRef.current = applyAction
  useEffect(() => {
    ;(window as unknown as { __YF_APPLY_SPACE_ACTION__?: unknown })
      .__YF_APPLY_SPACE_ACTION__ = applyAction
    return () => {
      delete (window as unknown as { __YF_APPLY_SPACE_ACTION__?: unknown })
        .__YF_APPLY_SPACE_ACTION__
    }
  }, [applyAction])
  const showMomentaryRef = useRef(showMomentary)
  showMomentaryRef.current = showMomentary

  useEffect(() => {
    const timer = window.setInterval(() => {
      const pending = pendingActionResult.current
      if (!pending) return
      const now = performance.now()
      const self = selfState.current
      const needsArrival = ['go_to', 'sit', 'lie', 'approach'].includes(pending.kind)
      const needsCarry = pending.kind === 'pick_up'
      const needsRelease = pending.kind === 'put_down'
      const settledAtTarget =
        needsArrival &&
        self.target === pending.target &&
        self.at_target &&
        !self.moving &&
        now - pending.startedAt > 800
      const carriedItem = needsCarry && pending.target ? getGrabbable(pending.target) : null
      const carryPosition = carriedItem?.body.translation()
      const carryDistance =
        self.hand && carryPosition
          ? Math.hypot(
              self.hand[0] - carryPosition.x,
              self.hand[1] - carryPosition.y,
              self.hand[2] - carryPosition.z,
            )
          : Infinity
      const carried =
        needsCarry &&
        Boolean(pending.target) &&
        heldByAvatar.current === pending.target &&
        carryDistance <= 0.62 &&
        now - pending.startedAt > 450
      const releasedBody =
        needsRelease && pending.target ? getGrabbable(pending.target)?.body : null
      const releaseVelocity = releasedBody?.linvel()
      const releaseSpeed = releaseVelocity
        ? Math.hypot(releaseVelocity.x, releaseVelocity.y, releaseVelocity.z)
        : Infinity
      const released =
        needsRelease &&
        Boolean(pending.target) &&
        heldByAvatar.current !== pending.target &&
        Boolean(releasedBody) &&
        (releasedBody?.isSleeping() || releaseSpeed < 0.28) &&
        now - pending.startedAt > 650
      const applied =
        !needsArrival && !needsCarry && !needsRelease && now - pending.startedAt > 650
      const failedFast = pending.forceOk === false && now - pending.startedAt > 200
      const movementStillRunning =
        needsArrival &&
        self.target === pending.target &&
        !self.at_target &&
        now < pending.hardDeadline
      if (now >= pending.deadline && movementStillRunning) {
        // 大房间从一头走到另一头往往超过模型估的 3 秒；只要她确实在朝目标走，
        // 就把这次动作的期限往后延，不能每 3 秒判失败再重发一次。
        pending.deadline = now + 1800
        return
      }
      const expired = now >= pending.deadline
      if (!settledAtTarget && !carried && !released && !applied && !failedFast && !expired) return
      pendingActionResult.current = null
      motionLockRef.current = null
      const ok =
        pending.forceOk === false
          ? false
          : needsArrival
            ? settledAtTarget
            : needsCarry
              ? carried
              : needsRelease
                ? released
                : true
      const detail =
        pending.forceOk === false
          ? pending.detail
          : needsArrival
            ? ok
              ? `${pending.detail}（已到达目标）`
              : `${pending.detail}（没有按时到达）`
            : needsCarry
              ? ok
                ? `${pending.detail}（已经稳稳拿在手里）`
                : `${pending.detail}（没把东西拿稳）`
              : needsRelease
                ? ok
                  ? `${pending.detail}（已经落稳）`
                  : `${pending.detail}（松手了，但还没落稳）`
            : pending.detail
      void api
        .reportSpaceActionResult(friend.id, pending.id, ok, detail)
        .catch(() => undefined)
    }, 250)
    return () => window.clearInterval(timer)
  }, [friend.id])
  useEffect(() => {
    if (!layoutReady) return
    let alive = true
    let busy = false
    const previousPositions = new Map<string, Vec3>()
    const collectObjects = (): SpacePerceptionObject[] => {
      const raw: Omit<SpacePerceptionObject, 'moving'>[] = [
        ...(Object.keys(OBJECT_LABELS) as SpaceObjectKey[]).map((key) => ({
          key,
          label: OBJECT_LABELS[key],
          position: propsRef.current[key],
        })),
        ...worldRef.current.objects.map((object) => ({
          key: object.id,
          label:
            WORLD_ASSETS.find((asset) => asset.id === object.asset)?.label ??
            (object.asset.startsWith('glb:') ? '导入的物件' : object.asset),
          position: object.position,
        })),
      ]
      return raw.map((item) => {
        const previous = previousPositions.get(item.key)
        const moving = previous
          ? Math.hypot(
              item.position[0] - previous[0],
              item.position[1] - previous[1],
              item.position[2] - previous[2],
            ) > 0.015
          : false
        previousPositions.set(item.key, item.position)
        return { ...item, moving }
      })
    }
    const latest = {
      viewer: viewerPayload(),
      objects: collectObjects(),
    }
    const snapshotTimer = window.setInterval(() => {
      latest.viewer = viewerPayload()
      latest.objects = collectObjects()
    }, 100)
    const tick = async () => {
      if (!alive || busy) return
      busy = true
      try {
        const result = await api.perceiveSpace(friend.id, {
          viewer: latest.viewer,
          self_state: selfPayload(),
          objects: latest.objects,
          paused: pausedRef.current || editModeRef.current,
          personality_curiosity: curiosityRef.current,
          activity_kind: activityRef.current.kind,
          activity_target: activityRef.current.focus,
          activity_label: activityRef.current.label,
          mood: moodRef.current,
        })
        if (!alive) return
        setRuntime(result.runtime)
        setKnowledge(result.knowledge)
        setDiscovery(result.discovery)
        if (result.line) {
          setAutoStatus(result.line)
          showMomentaryRef.current(result.line, null, 5200)
        }
        if (result.line || result.action || result.ask_intent) {
          setAttention(result.runtime.attention)
        }
        if (
          result.action &&
          !pausedRef.current &&
          !editModeRef.current &&
          Date.now() >= userBusyUntil.current
        ) {
          applyActionRef.current(result.action)
        }
        if (
          result.deliberation_due &&
          !deliberationBusy.current &&
          !awaitingReplyRef.current &&
          Date.now() >= userBusyUntil.current &&
          !chatOpenRef.current &&
          !pausedRef.current &&
          !editModeRef.current
        ) {
          deliberationBusy.current = true
          void api
            .deliberateSpace(friend.id, {
              viewer: viewerPayload(),
              self_state: selfPayload(),
              objects: latest.objects,
              paused: false,
              personality_curiosity: curiosityRef.current,
              activity_kind: activityRef.current.kind,
              activity_target: activityRef.current.focus,
              activity_label: activityRef.current.label,
              mood: moodRef.current,
            })
            .then((answer) => {
              if (!alive) return
              setRuntime(answer.runtime)
              setKnowledge(answer.knowledge)
              setDiscovery(answer.discovery)
              if (answer.emotion) {
                playEmotion(answer.emotion, Boolean(answer.awaiting_reply))
              }
              if (answer.line) {
                showMomentaryRef.current(answer.line, null, 5200)
              }
              if (
                answer.action &&
                !pausedRef.current &&
                !editModeRef.current &&
                Date.now() >= userBusyUntil.current
              ) {
                applyActionRef.current(answer.action)
              }
              if (answer.ask_intent) setAttention('notice')
            })
            .catch(() => undefined)
            .finally(() => {
              deliberationBusy.current = false
            })
        }
      } catch {
        // 感知 tick 失败不阻塞小世界；下一拍继续。
      } finally {
        busy = false
      }
    }
    const initialTimer = window.setTimeout(tick, 900)
    const tickTimer = window.setInterval(tick, 500)
    return () => {
      alive = false
      window.clearInterval(snapshotTimer)
      window.clearInterval(tickTimer)
      window.clearTimeout(initialTimer)
    }
  }, [friend.id, layoutReady, playEmotion, selfPayload, viewerPayload])

  const interact = (kind: Reaction) => {
    const contactZone = viewer.current.handContactZone
    const touchInteraction = contactZone
      ? (viewer.current.handContactKind ?? 'hold')
      : 'tap'
    const sustainedSeconds = contactZone ? viewer.current.handContactSeconds : 0
    userBusyUntil.current = Date.now() + 1200
    if (!contactZone) setAutoStatus(null)
    logEvent({
      kind: 'touch',
      target: kind,
      detail: {
        interaction: touchInteraction,
        sustained_seconds: sustainedSeconds,
      },
    })
    setReaction(kind)
    void api
      .reactSpace(friend.id, {
        zone: kind,
        viewer: viewerPayload(),
        self_state: selfPayload(),
        objects: [],
        activity_label: activityRef.current.label,
        mood: moodRef.current,
        interaction: touchInteraction,
        sustained_seconds: sustainedSeconds,
      })
      .then((answer) => {
        if (answer.busy) return
        setRuntime(answer.runtime)
        setDiscovery(answer.discovery)
        if (answer.line) {
          setAutoStatus(answer.line)
          showMomentary(answer.line, null, 5200)
        }
        if (answer.emotion) {
          playEmotion(answer.emotion, answer.awaiting_reply)
        }
        if (
          answer.action &&
          !pausedRef.current &&
          !editModeRef.current &&
          Date.now() >= userBusyUntil.current
        ) {
          applyActionRef.current(answer.action)
        }
      })
      .catch(() => undefined)
    if (reactionTimer.current !== null) window.clearTimeout(reactionTimer.current)
    reactionTimer.current = window.setTimeout(() => {
      setReaction(null)
    }, 3600)
  }

  const interactObject = (name: SpaceObjectKey) => {
    userBusyUntil.current = Date.now() + 1200
    setAutoStatus(null)
    setManualIdle(null)
    logEvent({ kind: 'use', target: name })
    const nextStates = { ...propStates }
    if (name === 'mug') nextStates.mug = propStates.mug === 'empty' ? 'full' : 'empty'
    else if (name === 'book' || name === 'notebook') {
      nextStates[name] = propStates[name] === 'open' ? 'closed' : 'open'
    } else if (name === 'plush') {
      nextStates.plush = propStates.plush === 'squished' ? 'normal' : 'squished'
    } else if (name === 'cushion') {
      nextStates.cushion = propStates.cushion === 'moved' ? 'normal' : 'moved'
    }
    setPropStates(nextStates)
    void api.patchSpace(friend.id, { prop_states: nextStates }).catch(() => undefined)
  }

  const callHer = () => {
    userBusyUntil.current = Date.now() + 1200
    setAutoStatus(null)
    setManualIdle(null)
    logEvent({ kind: 'call' })
  }

  const resetProps = () => setProps(DEFAULT_PROPS)

  const handlePropDragEnd = useCallback(
    (name: SpaceObjectKey, position: Vec3, moved: boolean) => {
      if (!moved) return
      userBusyUntil.current = Date.now() + 6000
      setAutoStatus(null)
      logEvent({ kind: 'move', target: name, detail: { to: position } })
    },
    [logEvent],
  )

  const handlePropSettled = useCallback(
    (name: SpaceObjectKey, position: Vec3, moved: boolean) => {
      setProps((previous) => ({ ...previous, [name]: position }))
      if (moved) handlePropDragEnd(name, position, true)
    },
    [handlePropDragEnd],
  )

  const persistWorld = (next: WorldState) => {
    setWorld(next)
    void api.patchSpace(friend.id, { world: next }).catch(() => undefined)
  }

  const addWorldObject = (asset: string) => {
    const def = WORLD_ASSETS.find((item) => item.id === asset)
    if (!def && !asset.startsWith('glb:')) return
    const spawn = WORLD_SPAWNS[world.objects.length % WORLD_SPAWNS.length]
    const proceduralState =
      asset === 'procedural'
        ? {
            shape: ['box', 'cylinder', 'cone'][Math.floor(Math.random() * 3)],
            color: ['#b58b5f', '#87997d', '#8aa7b8', '#c98f7f', '#d8c7b4'][
              Math.floor(Math.random() * 5)
            ],
            width: 0.45 + Math.random() * 0.5,
            height: 0.45 + Math.random() * 0.7,
            depth: 0.45 + Math.random() * 0.5,
          }
        : undefined
    const object: WorldObject = {
      id: `obj-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      asset,
      // 从固定空位摆放，别一加就压在角色/镜头前面。
      position: [spawn[0], def?.defaultY ?? 0, spawn[2]],
      rotation: 0,
      scale: 1,
      state: proceduralState,
    }
    persistWorld({ ...world, objects: [...world.objects, object] })
    setSelectedObjectId(object.id)
  }

  async function importGlb(file: File) {
    setEditorError(null)
    try {
      const result = await api.uploadSpaceAsset(friend.id, file)
      addWorldObject(result.asset)
    } catch (error) {
      setEditorError(error instanceof Error ? error.message : String(error))
    }
  }

  const mutateSelected = (patch: Partial<WorldObject>) => {
    if (!selectedObjectId) return
    persistWorld({
      ...world,
      objects: world.objects.map((item) =>
        item.id === selectedObjectId ? { ...item, ...patch } : item,
      ),
    })
  }

  const deleteSelected = () => {
    if (!selectedObjectId) return
    persistWorld({
      ...world,
      objects: world.objects.filter((item) => item.id !== selectedObjectId),
    })
    setSelectedObjectId(null)
  }

  const canMakeNest =
    world.objects.some((item) => item.asset === 'cushion') &&
    world.objects.some((item) => item.asset === 'blanket')

  const makeNest = () => {
    const cushion = world.objects.find((item) => item.asset === 'cushion')
    const blanket = world.objects.find((item) => item.asset === 'blanket')
    if (!cushion || !blanket) return
    const nest: WorldObject = {
      id: `obj-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      asset: 'nest',
      position: [
        (cushion.position[0] + blanket.position[0]) / 2,
        0,
        (cushion.position[2] + blanket.position[2]) / 2,
      ],
      rotation: 0,
      scale: 1,
    }
    persistWorld({
      ...world,
      objects: [
        ...world.objects.filter((item) => item.id !== cushion.id && item.id !== blanket.id),
        nest,
      ],
    })
    setSelectedObjectId(nest.id)
  }

  async function switchMode(next: SpaceMode) {
    setMode(next)
    setAutoStatus(null)
    setManualIdle(null)
    userBusyUntil.current = Date.now() + 2500
    try {
      await api.patchSpace(friend.id, { mode: next })
    } catch {
      // 写失败也保留本地模式；下次进来再同步
    }
    logEvent({ kind: 'mode', detail: { mode: next } })
  }

  async function talkToHer() {
    const text = roomDraft.trim()
    if (!text || roomBusy) return
    if (chatOpen) {
      setChatOpen(false)
      requestMouseLook()
    }
    setRoomBusy(true)
    setRoomError(null)
    setRoomDraft('')
    userBusyUntil.current = Date.now() + 1800
    setAttention('engaged')
    awaitingReplyRef.current = false
    setAwaitingReply(false)
    clearEmotion()
    setLine(null)
    if (liveThoughtTimer.current !== null) {
      window.clearTimeout(liveThoughtTimer.current)
      liveThoughtTimer.current = null
    }
    setLiveThought('')
    try {
      let streamed = ''
      let firstSpeak = false
      let doneEvent: {
        line: string | null
        thought: string
        emotion: string
        action: SpaceAction | null
        runtime: SpaceRuntime | null
        knowledge?: SpaceKnowledge[]
        discovery?: SpaceDiscovery | null
      } | null = null
      for await (const event of api.streamSpaceChat(friend.id, text, viewerPayload())) {
        if (event.type === 'error') {
          throw new Error(event.message)
        }
        if (event.type === 'thought') {
          setLiveThought(event.delta)
          continue
        }
        if (event.type === 'speak') {
          streamed += event.delta
          setLine(streamed)
          setAutoStatus(streamed)
          if (!firstSpeak) {
            firstSpeak = true
            if (!locomotion.lying) {
              setTempClip(
                locomotion.sitting ? 'Sitting_Talking_Loop' : 'Idle_Talking_Loop',
                6000,
              )
            }
          }
          continue
        }
        doneEvent = event
        if (event.runtime) setRuntime(event.runtime)
        if (event.knowledge) setKnowledge(event.knowledge)
        if (event.discovery) setDiscovery(event.discovery)
        if (event.thought) setLiveThought(event.thought)
        if (event.emotion) playEmotion(event.emotion, false)
        if (event.action) applyAction(event.action, 'user')
        if (event.line) {
          setAutoStatus(event.line)
          showMomentary(event.line, null, 5200)
        }
        if (liveThoughtTimer.current !== null) {
          window.clearTimeout(liveThoughtTimer.current)
        }
        liveThoughtTimer.current = window.setTimeout(() => {
          setLiveThought('')
          liveThoughtTimer.current = null
        }, 6500)
      }
      if (!doneEvent) {
        throw new Error('流式对话没有正常结束')
      }
    } catch (error) {
      setRoomError(error instanceof Error ? error.message : String(error))
    } finally {
      setRoomBusy(false)
    }
  }

  async function togglePerception() {
    if (!perceptionOpen) {
      try {
        const result = await api.getSpace(friend.id)
        setWorldModel(result.world_model)
        setRuntime(result.runtime)
        setKnowledge(result.knowledge)
      } catch {
        // 面板打开但不刷新时，继续显示上一次快照
      }
    }
    setPerceptionOpen((value) => !value)
  }

  async function toggleMessages() {
    if (!messagesOpen) {
      try {
        const result = await api.getSpace(friend.id)
        setWorldModel(result.world_model)
        setRuntime(result.runtime)
      } catch {
        // 用上一次快照也能打开
      }
    }
    setMessagesOpen((value) => !value)
  }

  useEffect(() => {
    if (!messagesOpen) return
    let alive = true
    const load = () => {
      void api
        .getSpace(friend.id)
        .then((result) => {
          if (!alive) return
          setWorldModel(result.world_model)
          setRuntime(result.runtime)
        })
        .catch(() => undefined)
    }
    load()
    const timer = window.setInterval(load, 2200)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [friend.id, messagesOpen])

  const changeOutfit = (slot: keyof SpaceOutfit, value: string) => {
    const next = { ...outfit, [slot]: value }
    setOutfit(next)
    void api
      .patchSpace(friend.id, { appearance: { outfit: next } })
      .catch(() => undefined)
  }

  const uploadAvatarModel = async (file: File) => {
    try {
      const result = await api.uploadAvatarModel(friend.id, file)
      setAvatarModel(result.model_url)
      setEditorError(null)
    } catch (error) {
      setEditorError(error instanceof Error ? error.message : String(error))
    }
  }

  const resetAvatarModel = () => {
    void api
      .deleteAvatarModel(friend.id)
      .then(() => setAvatarModel(null))
      .catch((error) => {
        setEditorError(error instanceof Error ? error.message : String(error))
      })
  }

  const runtimeThought =
    typeof runtime?.intent?.thought === 'string'
      ? String(runtime.intent.thought).trim()
      : ''
  const visibleThought = liveThought.trim() || runtimeThought
  const status = roomBusy
    ? '她在想…'
    : handHolding
      ? `你手里拿着${handHolding}`
      : autoStatus ?? ''
  const selectedObject =
    world.objects.find((item) => item.id === selectedObjectId) ?? null

  return (
    <div className="fixed inset-0 z-[80] overflow-hidden bg-[#111923] text-white">
      <SceneErrorBoundary onError={setFailed}>
        <Canvas
          shadows
          dpr={isMobile ? [1, 1.25] : [1, 1.5]}
          camera={SPACE_CAMERA}
          gl={SPACE_GL}
          onCreated={({ gl }) => {
            gl.domElement.id = 'yf-space-canvas'
            gl.domElement.style.touchAction = 'none'
            gl.setClearColor('#111923')
          }}
        >
          <ShadowBudget />
          <FrameBudgetProbe />
          <MotionSmoothnessProbe viewer={viewer} />
          <Physics gravity={[0, -9.81, 0]} timeStep={1 / 60}>
            <Scene
              attention={attention}
              idleTarget={activity.target}
              idleKind={activity.kind}
              reaction={reaction}
              moodExpression={moodExpression ?? emotionExpression}
              emotionName={emotionName}
              onReact={interact}
              onAvatarGrabStart={handleAvatarGrabStart}
              onAvatarGrabEnd={handleAvatarGrabEnd}
              onPostureReady={handlePostureReady}
              onMountComplete={handleMountComplete}
              onInteractObject={interactObject}
              locomotion={locomotion}
              animationClip={animationClip ?? emotionClip}
              outfit={outfit}
              avatarModel={avatarModel}
              propStates={propStates}
              world={world}
              friendId={friend.id}
              editMode={editMode}
              selectedObjectId={selectedObjectId}
              onSelectObject={setSelectedObjectId}
              onMoveObjectEnd={(id, position) =>
                persistWorld({
                  ...world,
                  objects: world.objects.map((item) =>
                    item.id === id ? { ...item, position } : item,
                  ),
                })
              }
              props={props}
              onPropSettled={handlePropSettled}
              setDragging={setDragging}
              viewer={viewer}
              selfState={selfState}
              noclip={noclip}
              paused={paused}
              onToggleNoclip={() => setNoclip((value) => !value)}
              onLockChange={(locked) => {
                if (isMobile) {
                  setPointerLocked(locked)
                  setPaused(false)
                  return
                }
                setPointerLocked(locked)
                if (locked) {
                  escapeUnlockAtRef.current = 0
                  menuRequestedRef.current = false
                  setPaused(false)
                  return
                }
                // 只有用户明确点「菜单」时才在释放鼠标后弹菜单。
                // 浏览器原生 Esc 释放鼠标时继续保持正常操作，第二次 Esc 才开菜单。
                if (menuRequestedRef.current) {
                  menuRequestedRef.current = false
                  setPaused(true)
                  return
                }
                setPaused(false)
              }}
              onGrabChange={setHandHolding}
              touchMode={isMobile}
              touchMove={touchMove}
              touchVertical={touchVertical}
            />
            <AvatarCarry heldKey={heldByAvatar} selfState={selfState} />
          </Physics>
        </Canvas>
      </SceneErrorBoundary>

      {isMobile && !paused && !chatOpen && (
        <>
          <TouchJoystick move={touchMove} />
          <div className="pointer-events-auto absolute bottom-40 left-4 z-30 flex flex-col gap-2">
            <button
              type="button"
              onPointerDown={() => { touchVertical.current = 1 }}
              onPointerUp={() => { touchVertical.current = 0 }}
              onPointerCancel={() => { touchVertical.current = 0 }}
              onPointerLeave={() => { touchVertical.current = 0 }}
              className="h-11 w-11 rounded-full border border-white/25 bg-slate-950/40 text-lg text-white/75 backdrop-blur active:bg-white/20"
              aria-label="升高"
            >
              ↑
            </button>
            <button
              type="button"
              onPointerDown={() => { touchVertical.current = -1 }}
              onPointerUp={() => { touchVertical.current = 0 }}
              onPointerCancel={() => { touchVertical.current = 0 }}
              onPointerLeave={() => { touchVertical.current = 0 }}
              className="h-11 w-11 rounded-full border border-white/25 bg-slate-950/40 text-lg text-white/75 backdrop-blur active:bg-white/20"
              aria-label="降低"
            >
              ↓
            </button>
          </div>
          <p className="pointer-events-none absolute bottom-44 right-4 z-20 max-w-[9rem] text-right text-[10px] leading-relaxed text-white/35">
            右侧拖动转视角
            <br />
            左侧摇杆移动
          </p>
        </>
      )}

      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_50%_38%,transparent_35%,rgba(4,9,14,0.52)_100%)]" />
      {line && (
        <div className="pointer-events-none absolute left-1/2 top-[17%] z-10 max-w-[310px] -translate-x-1/2 rounded-2xl rounded-bl-sm border border-white/20 bg-slate-950/80 px-3.5 py-2 text-center text-[12px] leading-relaxed text-white shadow-xl backdrop-blur">
          {line}
        </div>
      )}
      <header className={`absolute inset-x-0 top-0 flex items-start justify-between ${isMobile ? 'gap-2 p-3' : 'p-5'}`}>
        <div className="pointer-events-none">
          {!isMobile && (
            <p className="text-[10px] font-semibold tracking-[0.24em] text-white/45">YUMEFRIEND / SMALL WORLD</p>
          )}
          <h1 className={`mt-1 font-semibold tracking-tight ${isMobile ? 'text-lg' : 'text-xl'}`}>{friend.name}的小世界</h1>
          <p className="mt-1 text-[11px] text-white/55">{status}</p>
          {visibleThought && (
            <p className="mt-1 max-w-[520px] text-[11px] leading-relaxed text-white/40">
              此刻念头：{visibleThought.length > 120 ? `${visibleThought.slice(0, 120)}…` : visibleThought}
            </p>
          )}
        </div>
        <div className={`pointer-events-auto flex items-center gap-2 ${isMobile ? 'max-w-[62%] flex-wrap justify-end' : ''}`}>
          {isMobile ? (
            <>
              <button
                type="button"
                onClick={() => void toggleMessages()}
                className={`rounded-full border px-2.5 py-1.5 text-[10.5px] backdrop-blur ${messagesOpen ? 'border-sky-300/70 bg-sky-300/15 text-sky-100' : 'border-white/20 bg-white/10 text-white/85'}`}
              >
                记录
              </button>
              <button
                type="button"
                onClick={() => void togglePerception()}
                className={`rounded-full border px-2.5 py-1.5 text-[10.5px] backdrop-blur ${perceptionOpen ? 'border-emerald-300/70 bg-emerald-300/15 text-emerald-100' : 'border-white/20 bg-white/10 text-white/85'}`}
              >
                感知
              </button>
              <button
                type="button"
                onClick={() => setOutfitOpen((value) => !value)}
                className={`rounded-full border px-2.5 py-1.5 text-[10.5px] backdrop-blur ${outfitOpen ? 'border-pink-300/70 bg-pink-300/15 text-pink-100' : 'border-white/20 bg-white/10 text-white/85'}`}
              >
                衣橱
              </button>
              <button
                type="button"
                onClick={() => {
                  if (document.pointerLockElement) document.exitPointerLock()
                  setPaused(true)
                }}
                className="rounded-full border border-white/20 bg-white/10 px-2.5 py-1.5 text-[10.5px] text-white/85 backdrop-blur"
              >
                菜单
              </button>
            </>
          ) : (
            <>
          <button
            type="button"
            onClick={() => void toggleMessages()}
            title="只属于小世界的对话和事件；普通聊天不会混进来"
            className={`rounded-full border px-3 py-1.5 text-[11px] backdrop-blur transition-colors ${
              messagesOpen
                ? 'border-sky-300/70 bg-sky-300/15 text-sky-100'
                : 'border-white/20 bg-white/10 text-white/85 hover:bg-white/20'
            }`}
          >
            小世界
          </button>
          <button
            type="button"
            onClick={() => void togglePerception()}
            title="查看她此刻读到的世界：实体、状态、区域、可做的事、最近事件"
            className={`rounded-full border px-3 py-1.5 text-[11px] backdrop-blur transition-colors ${
              perceptionOpen
                ? 'border-emerald-300/70 bg-emerald-300/15 text-emerald-100'
                : 'border-white/20 bg-white/10 text-white/85 hover:bg-white/20'
            }`}
          >
            感知
          </button>
          <button
            type="button"
            onClick={() => setOutfitOpen((value) => !value)}
            title="打开衣橱：上衣 / 下装 / 鞋 / 配饰会真实改变她身上的衣服材质"
            className={`rounded-full border px-3 py-1.5 text-[11px] backdrop-blur transition-colors ${
              outfitOpen
                ? 'border-pink-300/70 bg-pink-300/15 text-pink-100'
                : 'border-white/20 bg-white/10 text-white/85 hover:bg-white/20'
            }`}
          >
            衣橱
          </button>
          <button
            type="button"
            onClick={toggleEditMode}
            title="编辑房间：添加/移动/旋转/缩放/删除物件"
            className={`rounded-full border px-3 py-1.5 text-[11px] backdrop-blur transition-colors ${
              editMode
                ? 'border-amber-300/70 bg-amber-200/20 text-amber-100'
                : 'border-white/20 bg-white/10 text-white/85 hover:bg-white/20'
            }`}
          >
            {editMode ? '完成' : '编辑'}
          </button>
          <button
            type="button"
            onClick={() => void switchMode(mode === 'free' ? 'together' : 'free')}
            title="自由：她自己找事做；陪伴：她更注意你。碰她/叫她永远优先。"
            className="rounded-full border border-white/20 bg-white/10 px-3 py-1.5 text-[11px] text-white/85 backdrop-blur transition-colors hover:bg-white/20"
          >
            {mode === 'free' ? '自由' : '陪伴'}
          </button>
          <button
            type="button"
            onClick={() => setNoclip((value) => !value)}
            title="默认会撞到墙和家具；穿墙模式可以飞出房间边界。V 键也切换。"
            className={`rounded-full border px-3 py-1.5 text-[11px] backdrop-blur transition-colors ${
              noclip
                ? 'border-sky-300/70 bg-sky-300/15 text-sky-100'
                : 'border-white/20 bg-white/10 text-white/85 hover:bg-white/20'
            }`}
          >
            {noclip ? '穿墙开' : '穿墙关'}
          </button>
          <button
            type="button"
            onClick={callHer}
            className="rounded-full border border-white/20 bg-white/10 px-3 py-1.5 text-[11px] text-white/85 backdrop-blur transition-colors hover:bg-white/20"
          >
            叫她
          </button>
          <button
            type="button"
            onClick={resetProps}
            className="rounded-full border border-white/15 bg-black/20 px-3 py-1.5 text-[11px] text-white/60 backdrop-blur transition-colors hover:bg-white/10"
          >
            重置摆位
          </button>
          <button
            type="button"
            onClick={() => {
              if (editMode) {
                setEditMode(false)
                setSelectedObjectId(null)
              }
              menuRequestedRef.current = true
              if (document.pointerLockElement) document.exitPointerLock()
              setPaused(true)
            }}
            className="rounded-full border border-white/20 bg-white/10 px-3 py-1.5 text-[11px] text-white/85 backdrop-blur transition-colors hover:bg-white/20"
          >
            菜单
          </button>
            </>
          )}
        </div>
      </header>

      {perceptionOpen && (
        <PerceptionPanel model={worldModel} onClose={() => setPerceptionOpen(false)} />
      )}
      {messagesOpen && (
        <SpaceMessagesPanel
          model={worldModel}
          onClose={() => setMessagesOpen(false)}
        />
      )}
      {outfitOpen && (
        <OutfitPanel
          outfit={outfit}
          modelUrl={avatarModel}
          onChange={changeOutfit}
          onModelUpload={(file) => void uploadAvatarModel(file)}
          onModelReset={resetAvatarModel}
          onClose={() => setOutfitOpen(false)}
        />
      )}

      {paused && !editMode && !chatOpen && (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-slate-950/65 p-6 backdrop-blur-sm">
          <div className="pointer-events-auto w-[min(92%,440px)] rounded-3xl border border-white/15 bg-slate-900/92 p-6 text-white shadow-2xl">
            <p className="text-[10px] font-semibold tracking-[0.22em] text-white/40">
              YUMEFRIEND / SMALL WORLD
            </p>
            <h2 className="mt-1 text-2xl font-semibold tracking-tight">
              {isMobile ? '小世界菜单' : '鼠标菜单'}
            </h2>
            <p className="mt-2 text-[12px] leading-relaxed text-white/60">
              {isMobile
                ? '左侧摇杆移动，右侧拖动转视角；点她的身体可以互动。'
                : '鼠标已释放，视角和手暂停。点「继续」重新锁定鼠标。'}
            </p>

            <div className="mt-5 space-y-2">
              <button
                type="button"
                onClick={requestMouseLook}
                className="w-full rounded-2xl bg-amber-200 px-4 py-3 text-[13px] font-semibold text-slate-950 transition-colors hover:bg-amber-100"
              >
                {isMobile ? '继续' : '继续 · 锁定鼠标'}
              </button>
              {isMobile && (
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => void toggleMessages()}
                    className="rounded-2xl border border-sky-200/20 bg-sky-200/10 px-3 py-2.5 text-[12px] text-sky-100"
                  >
                    小世界记录
                  </button>
                  <button
                    type="button"
                    onClick={callHer}
                    className="rounded-2xl border border-white/15 bg-white/5 px-3 py-2.5 text-[12px] text-white/85"
                  >
                    叫她
                  </button>
                  <button
                    type="button"
                    onClick={() => setOutfitOpen((value) => !value)}
                    className="rounded-2xl border border-pink-200/20 bg-pink-200/10 px-3 py-2.5 text-[12px] text-pink-100"
                  >
                    衣橱
                  </button>
                  <button
                    type="button"
                    onClick={() => void switchMode(mode === 'free' ? 'together' : 'free')}
                    className="rounded-2xl border border-white/15 bg-white/5 px-3 py-2.5 text-[12px] text-white/85"
                  >
                    {mode === 'free' ? '切换陪伴' : '切换自由'}
                  </button>
                </div>
              )}
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => setNoclip((value) => !value)}
                  className="rounded-2xl border border-white/15 bg-white/5 px-3 py-2.5 text-[12px] text-white/85 transition-colors hover:bg-white/10"
                >
                  穿墙：{noclip ? '开' : '关'}
                </button>
                <button
                  type="button"
                  onClick={toggleEditMode}
                  className="rounded-2xl border border-white/15 bg-white/5 px-3 py-2.5 text-[12px] text-white/85 transition-colors hover:bg-white/10"
                >
                  编辑房间
                </button>
              </div>
              {!isMobile && (
                <button
                  type="button"
                  onClick={() => void togglePerception()}
                  className="w-full rounded-2xl border border-emerald-200/20 bg-emerald-200/10 px-3 py-2.5 text-[12px] text-emerald-100 transition-colors hover:bg-emerald-200/20"
                >
                  感知她看到的世界
                </button>
              )}
              <button
                type="button"
                onClick={() => {
                  setPaused(true)
                  onClose()
                }}
                className="w-full rounded-2xl border border-red-300/20 bg-red-400/10 px-3 py-2.5 text-[12px] text-red-100 transition-colors hover:bg-red-400/20"
              >
                退出小世界
              </button>
            </div>

            {!isMobile && (
              <div className="mt-5 border-t border-white/10 pt-4 text-[11px] leading-relaxed text-white/45">
                <p>WASD 移动 · Space / Shift 升降 · 鼠标转视角 · V 穿墙 · R 回出生点</p>
                <p className="mt-1">
                  T 说话 · 按住左键抓她 / 抓物件（不拖 = 持续触碰） · V 穿墙 · Esc 释放鼠标 · 再按 Esc 打开菜单
                </p>
                {!pointerLocked && (
                  <p className="mt-2 text-amber-200/70">
                    当前没有锁定鼠标；浏览器只允许在点击后锁定。
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {!isMobile && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-between p-5">
          <p className="text-[11px] text-white/55">
            {chatOpen
              ? '正在输入 · Enter 发送 · Esc 取消'
              : dragging
                ? '正在搬东西，蓝色圈是碰撞预览…'
                : 'WASD 移动 · Space/Shift 升降 · T 说话 · 按住左键抓她/物件（不拖 = 持续触碰） · V 穿墙 · R 回出生点 · Esc 释放鼠标，再按 Esc 打开菜单'}
          </p>
          <p className="text-[10px] text-white/35">
            {avatarModel ? 'VRM: 自定义模型' : 'VRM: Vivi · CC0（见模型目录 LICENSE）'}
          </p>
        </div>
      )}
      {isMobile && !paused && !chatOpen && !editMode && (
        <button
          type="button"
          onClick={() => setChatOpen(true)}
          className="pointer-events-auto absolute bottom-5 right-4 z-30 h-16 w-16 rounded-full border border-white/25 bg-slate-950/45 text-[13px] font-medium text-white/85 shadow-xl backdrop-blur active:bg-white/20"
        >
          说话
        </button>
      )}
      {!editMode && (!isMobile || chatOpen) && (
        <div className={`pointer-events-auto absolute left-1/2 z-20 flex -translate-x-1/2 items-center gap-2 rounded-2xl border border-white/20 bg-slate-950/75 p-2 shadow-2xl backdrop-blur ${isMobile ? 'bottom-4 w-[calc(100%-1.5rem)]' : 'bottom-16 w-[min(92%,520px)]'}`}>
          {roomBusy && (
            <span className="shrink-0 pl-2 text-[10.5px] text-white/45">
              她在想…
            </span>
          )}
          <input
            ref={roomInputRef}
            value={roomDraft}
            onChange={(event) => setRoomDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault()
                event.stopPropagation()
                closeRoomChat()
                return
              }
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void talkToHer()
              }
            }}
            placeholder="在房间里跟她说点什么（也会进聊天记录）…"
            className="min-w-0 flex-1 bg-transparent px-2 py-1.5 text-[12px] text-white outline-none placeholder:text-white/35"
          />
          <button
            type="button"
            disabled={roomBusy || !roomDraft.trim()}
            onClick={() => void talkToHer()}
            className="shrink-0 rounded-xl border border-white/20 bg-white/10 px-3 py-1.5 text-[11px] text-white/85 transition-colors hover:bg-white/20 disabled:opacity-35"
          >
            {roomBusy ? '…' : '说'}
          </button>
        </div>
      )}
      {roomError && (
        <p className="pointer-events-none absolute bottom-28 left-1/2 z-20 -translate-x-1/2 text-[10.5px] text-red-200">
          {roomError}
        </p>
      )}
      {!editMode && !paused && (
        <div className="pointer-events-none absolute left-1/2 top-1/2 z-10 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/70 bg-white/20 shadow-[0_0_8px_rgba(255,255,255,0.45)]" />
      )}
      {editMode && (
        <div className="pointer-events-auto absolute bottom-16 left-1/2 z-20 w-[min(92%,780px)] -translate-x-1/2 rounded-2xl border border-white/20 bg-slate-950/80 p-3 text-white shadow-2xl backdrop-blur">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10.5px] uppercase tracking-[0.18em] text-white/45">
              环境
            </span>
            {[
              { id: 'room' as const, label: '暖色房间' },
              { id: 'white' as const, label: '白空间' },
            ].map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => persistWorld({ ...world, environment: item.id })}
                className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                  world.environment === item.id
                    ? 'border-amber-300/70 bg-amber-200/20 text-amber-100'
                    : 'border-white/15 bg-white/5 text-white/70 hover:bg-white/10'
                }`}
              >
                {item.label}
              </button>
            ))}
            <span className="ml-auto text-[10.5px] text-white/40">
              {world.objects.length}/24 个物件
            </span>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="text-[10.5px] text-white/45">添加：</span>
            {WORLD_ASSETS.map((asset) => (
              <button
                key={asset.id}
                type="button"
                onClick={() => addWorldObject(asset.id)}
                className="rounded-full border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-white/75 transition-colors hover:bg-white/10"
              >
                {asset.label}
              </button>
            ))}
            {canMakeNest && (
              <button
                type="button"
                onClick={makeNest}
                className="rounded-full border border-amber-300/40 bg-amber-200/15 px-2.5 py-1 text-[11px] text-amber-100"
              >
                靠垫 + 毯子 = 小窝
              </button>
            )}
            <input
              ref={glbInputRef}
              type="file"
              accept=".glb,model/gltf-binary"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (file) void importGlb(file)
              }}
            />
            <button
              type="button"
              onClick={() => glbInputRef.current?.click()}
              className="rounded-full border border-sky-300/30 bg-sky-300/10 px-2.5 py-1 text-[11px] text-sky-100"
            >
              导入 GLB
            </button>
          </div>
          {editorError && <p className="mt-1 text-[10.5px] text-red-200">{editorError}</p>}
          {selectedObject && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-white/10 pt-2">
              <span className="text-[10.5px] text-white/45">
                选中：{WORLD_ASSETS.find((item) => item.id === selectedObject.asset)?.label ?? selectedObject.asset}
              </span>
              <button
                type="button"
                onClick={() => mutateSelected({ rotation: selectedObject.rotation - Math.PI / 12 })}
                className="rounded-full border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-white/75"
              >
                左转 15°
              </button>
              <button
                type="button"
                onClick={() => mutateSelected({ rotation: selectedObject.rotation + Math.PI / 12 })}
                className="rounded-full border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-white/75"
              >
                右转 15°
              </button>
              <button
                type="button"
                onClick={() => mutateSelected({ scale: Math.max(0.35, selectedObject.scale - 0.1) })}
                className="rounded-full border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-white/75"
              >
                缩小
              </button>
              <button
                type="button"
                onClick={() => mutateSelected({ scale: Math.min(2.5, selectedObject.scale + 0.1) })}
                className="rounded-full border border-white/15 bg-white/5 px-2.5 py-1 text-[11px] text-white/75"
              >
                放大
              </button>
              <button
                type="button"
                onClick={deleteSelected}
                className="rounded-full border border-red-300/30 bg-red-400/10 px-2.5 py-1 text-[11px] text-red-100"
              >
                删除
              </button>
            </div>
          )}
        </div>
      )}
      {failed && (
        <div className="absolute bottom-14 left-5 rounded-lg border border-red-300/20 bg-red-950/60 px-3 py-2 text-[11px] text-red-100/80">
          模型加载失败：{failed}
        </div>
      )}
    </div>
  )
}
