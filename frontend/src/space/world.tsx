/** 可编辑世界的内置资产库与物件渲染（v7.23 / D376）。
 *
 * 第一版全部用基础几何体拼：桌子/椅子/书架/床/地毯/植物/灯/箱子。
 * 素材的 ids 与后端 `space_service.WORLD_ASSETS` 保持一致。
 */

import { Suspense, useEffect, useMemo, useRef, useState, type ReactElement } from 'react'
import { RoundedBox } from '@react-three/drei'
import { useFrame, useLoader, type ThreeEvent } from '@react-three/fiber'
import {
  CuboidCollider,
  RigidBody,
  type RapierRigidBody,
} from '@react-three/rapier'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'

import { registerGrabbable, unregisterGrabbable } from './grabRegistry'

export type Vec3 = [number, number, number]
export type WorldEnvironment = 'room' | 'white'

export interface WorldObject {
  id: string
  asset: string
  position: Vec3
  rotation: number
  scale: number
  state?: Record<string, unknown>
}

export interface WorldState {
  environment: WorldEnvironment
  objects: WorldObject[]
}

export const DEFAULT_WORLD: WorldState = { environment: 'room', objects: [] }

function TableAsset() {
  return (
    <group>
      <RoundedBox args={[1.3, 0.1, 0.75]} radius={0.03} smoothness={3} position={[0, 0.72, 0]} castShadow>
        <meshStandardMaterial color="#8c6248" roughness={0.75} />
      </RoundedBox>
      {[-0.52, 0.52].map((x) =>
        [-0.28, 0.28].map((z) => (
          <mesh key={`${x}-${z}`} position={[x, 0.35, z]} castShadow>
            <boxGeometry args={[0.08, 0.7, 0.08]} />
            <meshStandardMaterial color="#634531" roughness={0.8} />
          </mesh>
        )),
      )}
    </group>
  )
}

function ChairAsset() {
  return (
    <group>
      <RoundedBox args={[0.55, 0.1, 0.55]} radius={0.04} smoothness={3} position={[0, 0.46, 0]} castShadow>
        <meshStandardMaterial color="#a87a5b" roughness={0.8} />
      </RoundedBox>
      <RoundedBox args={[0.55, 0.62, 0.09]} radius={0.04} smoothness={3} position={[0, 0.78, -0.25]} castShadow>
        <meshStandardMaterial color="#8f6449" roughness={0.8} />
      </RoundedBox>
      {[-0.21, 0.21].map((x) =>
        [-0.21, 0.21].map((z) => (
          <mesh key={`${x}-${z}`} position={[x, 0.22, z]} castShadow>
            <boxGeometry args={[0.06, 0.44, 0.06]} />
            <meshStandardMaterial color="#604331" roughness={0.8} />
          </mesh>
        )),
      )}
    </group>
  )
}

function ShelfAsset() {
  return (
    <group>
      <mesh position={[0, 0.9, 0]} castShadow>
        <boxGeometry args={[0.95, 1.8, 0.35]} />
        <meshStandardMaterial color="#6d4d3c" roughness={0.75} />
      </mesh>
      {[0.25, 0.72, 1.19, 1.66].map((y) => (
        <mesh key={y} position={[0, y, 0.2]}>
          <boxGeometry args={[0.86, 0.035, 0.02]} />
          <meshStandardMaterial color="#d1a77a" />
        </mesh>
      ))}
    </group>
  )
}

function BedAsset() {
  return (
    <group>
      <mesh position={[0, 0.18, 0]} castShadow>
        <boxGeometry args={[1.5, 0.32, 0.8]} />
        <meshStandardMaterial color="#72513d" roughness={0.8} />
      </mesh>
      <RoundedBox args={[1.42, 0.16, 0.72]} radius={0.06} smoothness={3} position={[0, 0.39, 0]} castShadow>
        <meshStandardMaterial color="#d8c7b4" roughness={0.95} />
      </RoundedBox>
      <RoundedBox args={[0.42, 0.12, 0.4]} radius={0.1} smoothness={4} position={[-0.42, 0.52, 0]} castShadow>
        <meshStandardMaterial color="#f0e7da" roughness={0.95} />
      </RoundedBox>
    </group>
  )
}

function RugAsset() {
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.012, 0]} receiveShadow>
      <circleGeometry args={[0.85, 36]} />
      <meshStandardMaterial color="#9aa88f" roughness={1} />
    </mesh>
  )
}

function PlantAsset() {
  return (
    <group>
      <mesh position={[0, 0.22, 0]} castShadow>
        <cylinderGeometry args={[0.2, 0.26, 0.44, 18]} />
        <meshStandardMaterial color="#b77a5b" roughness={0.9} />
      </mesh>
      {[0, 1, 2, 3, 4].map((i) => (
        <mesh
          key={i}
          position={[Math.cos(i * 1.9) * 0.13, 0.58 + (i % 2) * 0.14, Math.sin(i * 1.9) * 0.1]}
          rotation={[0, 0, (i - 2) * 0.35]}
          castShadow
        >
          <coneGeometry args={[0.11, 0.42, 8]} />
          <meshStandardMaterial color="#4f806e" roughness={0.9} />
        </mesh>
      ))}
    </group>
  )
}

function LampAsset() {
  return (
    <group>
      <mesh position={[0, 0.04, 0]}>
        <cylinderGeometry args={[0.2, 0.24, 0.08, 18]} />
        <meshStandardMaterial color="#6a5a4b" roughness={0.7} />
      </mesh>
      <mesh position={[0, 0.72, 0]}>
        <cylinderGeometry args={[0.035, 0.035, 1.35, 10]} />
        <meshStandardMaterial color="#8f7a63" roughness={0.6} />
      </mesh>
      <mesh position={[0, 1.42, 0]}>
        <coneGeometry args={[0.28, 0.32, 20, 1, true]} />
        <meshStandardMaterial color="#e8d4b4" side={THREE.DoubleSide} roughness={0.8} />
      </mesh>
      <mesh position={[0, 1.37, 0]}>
        <sphereGeometry args={[0.09, 14, 10]} />
        <meshStandardMaterial color="#ffe4b0" emissive="#ffd28a" emissiveIntensity={1.2} />
      </mesh>
    </group>
  )
}

function BoxAsset() {
  return (
    <group>
      <RoundedBox args={[0.55, 0.55, 0.55]} radius={0.04} smoothness={3} position={[0, 0.28, 0]} castShadow>
        <meshStandardMaterial color="#b58b5f" roughness={0.85} />
      </RoundedBox>
      <mesh position={[0, 0.28, 0.281]}>
        <boxGeometry args={[0.4, 0.04, 0.01]} />
        <meshStandardMaterial color="#6f5138" />
      </mesh>
    </group>
  )
}

function CushionAsset() {
  return (
    <RoundedBox args={[0.72, 0.2, 0.55]} radius={0.09} smoothness={4} position={[0, 0.11, 0]} castShadow>
      <meshStandardMaterial color="#87997d" roughness={0.95} />
    </RoundedBox>
  )
}

function BlanketAsset() {
  return (
    <group>
      <RoundedBox args={[0.9, 0.06, 0.65]} radius={0.03} smoothness={3} position={[0, 0.04, 0]} castShadow>
        <meshStandardMaterial color="#b98f7a" roughness={0.98} />
      </RoundedBox>
      {[-0.22, 0.22].map((x) => (
        <mesh key={x} position={[x, 0.075, 0]}>
          <boxGeometry args={[0.04, 0.012, 0.58]} />
          <meshStandardMaterial color="#e6d3c4" roughness={0.95} />
        </mesh>
      ))}
    </group>
  )
}

function NestAsset() {
  return (
    <group>
      <mesh position={[0, 0.12, 0]} castShadow>
        <cylinderGeometry args={[0.55, 0.62, 0.24, 28]} />
        <meshStandardMaterial color="#8c9a80" roughness={0.98} />
      </mesh>
      <mesh position={[0, 0.25, 0]} rotation={[-0.08, 0, 0]} castShadow>
        <cylinderGeometry args={[0.48, 0.52, 0.08, 28]} />
        <meshStandardMaterial color="#c7a68d" roughness={0.98} />
      </mesh>
      <mesh position={[0, 0.31, -0.18]} rotation={[-0.4, 0, 0]} castShadow>
        <boxGeometry args={[0.55, 0.06, 0.34]} />
        <meshStandardMaterial color="#d8b8a4" roughness={0.98} />
      </mesh>
    </group>
  )
}

function ProceduralAsset({ state }: { state?: Record<string, unknown> }) {
  const shape = String(state?.shape || 'box')
  const color = String(state?.color || '#b58b5f')
  const width = Math.max(0.2, Math.min(1.4, Number(state?.width) || 0.6))
  const height = Math.max(0.2, Math.min(1.6, Number(state?.height) || 0.6))
  const depth = Math.max(0.2, Math.min(1.4, Number(state?.depth) || 0.6))
  if (shape === 'cylinder') {
    return (
      <mesh position={[0, height / 2, 0]} castShadow>
        <cylinderGeometry args={[width / 2, width / 2, height, 24]} />
        <meshStandardMaterial color={color} roughness={0.8} />
      </mesh>
    )
  }
  if (shape === 'cone') {
    return (
      <mesh position={[0, height / 2, 0]} castShadow>
        <coneGeometry args={[width / 2, height, 24]} />
        <meshStandardMaterial color={color} roughness={0.8} />
      </mesh>
    )
  }
  return (
    <RoundedBox
      args={[width, height, depth]}
      radius={0.04}
      smoothness={3}
      position={[0, height / 2, 0]}
      castShadow
    >
      <meshStandardMaterial color={color} roughness={0.85} />
    </RoundedBox>
  )
}

export interface WorldAssetDef {
  id: string
  label: string
  Component: () => ReactElement
  defaultY: number
}

export const WORLD_ASSETS: WorldAssetDef[] = [
  { id: 'table', label: '桌子', Component: TableAsset, defaultY: 0 },
  { id: 'chair', label: '椅子', Component: ChairAsset, defaultY: 0 },
  { id: 'shelf', label: '书架', Component: ShelfAsset, defaultY: 0 },
  { id: 'bed', label: '床', Component: BedAsset, defaultY: 0 },
  { id: 'rug', label: '地毯', Component: RugAsset, defaultY: 0 },
  { id: 'plant', label: '植物', Component: PlantAsset, defaultY: 0 },
  { id: 'lamp', label: '灯', Component: LampAsset, defaultY: 0 },
  { id: 'box', label: '箱子', Component: BoxAsset, defaultY: 0 },
  { id: 'cushion', label: '靠垫', Component: CushionAsset, defaultY: 0 },
  { id: 'blanket', label: '毯子', Component: BlanketAsset, defaultY: 0 },
  { id: 'nest', label: '小窝', Component: NestAsset, defaultY: 0 },
  {
    id: 'procedural',
    label: '程序化物件',
    Component: () => <ProceduralAsset state={{ shape: 'box' }} />,
    defaultY: 0,
  },
]

const ASSET_PHYSICS: Record<
  string,
  { half: Vec3; center: Vec3; mass: number; grabRadius: number }
> = {
  table: { half: [0.65, 0.36, 0.375], center: [0, 0.36, 0], mass: 18, grabRadius: 0.95 },
  chair: { half: [0.28, 0.46, 0.28], center: [0, 0.46, 0], mass: 8, grabRadius: 0.68 },
  shelf: { half: [0.48, 0.9, 0.18], center: [0, 0.9, 0], mass: 24, grabRadius: 1.0 },
  bed: { half: [0.75, 0.28, 0.4], center: [0, 0.28, 0], mass: 20, grabRadius: 1.15 },
  rug: { half: [0.82, 0.012, 0.82], center: [0, 0.012, 0], mass: 2, grabRadius: 1.15 },
  plant: { half: [0.24, 0.46, 0.24], center: [0, 0.46, 0], mass: 5, grabRadius: 0.62 },
  lamp: { half: [0.18, 0.72, 0.18], center: [0, 0.72, 0], mass: 4, grabRadius: 0.62 },
  box: { half: [0.28, 0.28, 0.28], center: [0, 0.28, 0], mass: 3, grabRadius: 0.52 },
  cushion: { half: [0.36, 0.11, 0.28], center: [0, 0.11, 0], mass: 1, grabRadius: 0.58 },
  blanket: { half: [0.45, 0.035, 0.33], center: [0, 0.035, 0], mass: 1, grabRadius: 0.66 },
  nest: { half: [0.6, 0.16, 0.6], center: [0, 0.16, 0], mass: 7, grabRadius: 0.88 },
  procedural: { half: [0.34, 0.38, 0.34], center: [0, 0.38, 0], mass: 3, grabRadius: 0.62 },
}

export function WorldObjectView({
  object,
  friendId,
  selected,
  editable,
  onSelect,
  onMoveEnd,
  setDragging,
}: {
  object: WorldObject
  friendId: string
  selected: boolean
  editable: boolean
  onSelect: () => void
  onMoveEnd: (position: Vec3) => void
  setDragging: (dragging: boolean) => void
}) {
  const asset = WORLD_ASSETS.find((item) => item.id === object.asset) ?? WORLD_ASSETS[0]
  const Asset = asset.Component
  const physics = ASSET_PHYSICS[object.asset] ?? ASSET_PHYSICS.procedural
  const glbUrl = object.asset.startsWith('glb:')
    ? `/api/friends/${friendId}/space/assets/${object.asset.slice(4)}`
    : null
  const procedural = object.asset === 'procedural'
  const body = useRef<RapierRigidBody | null>(null)
  const dragging = useRef(false)
  const floor = useMemo(() => new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), [])
  const [localPosition, setLocalPosition] = useState<Vec3>(object.position)
  const lastSaved = useRef<Vec3>(object.position)
  const stillSince = useRef(0)

  useEffect(() => {
    setLocalPosition(object.position)
    lastSaved.current = object.position
  }, [object.position])

  useEffect(() => {
    if (!editable) return
    const current = body.current?.translation()
    if (current) setLocalPosition([current.x, current.y, current.z])
  }, [editable])

  useEffect(() => {
    const current = body.current
    if (!current || editable) return
    registerGrabbable(
      object.id,
      current,
      asset.label,
      physics.grabRadius * object.scale,
    )
    return () => unregisterGrabbable(object.id)
  }, [asset.label, editable, object.id, object.scale, physics.grabRadius])

  useEffect(() => {
    const up = () => {
      if (!dragging.current) return
      dragging.current = false
      setDragging(false)
      onMoveEnd(localPosition)
    }
    window.addEventListener('pointerup', up)
    return () => window.removeEventListener('pointerup', up)
  }, [onMoveEnd, setDragging])

  const move = (event: ThreeEvent<PointerEvent>) => {
    if (!dragging.current) return
    event.stopPropagation()
    const point = new THREE.Vector3()
    if (event.ray.intersectPlane(floor, point)) {
      const next: Vec3 = [
        THREE.MathUtils.clamp(point.x, -4.75, 4.75),
        localPosition[1],
        THREE.MathUtils.clamp(point.z, -4.75, 4.75),
      ]
      setLocalPosition(next)
      const current = body.current
      if (current) {
        const translation = { x: next[0], y: next[1], z: next[2] }
        current.setTranslation(translation, true)
        current.setNextKinematicTranslation(translation)
        current.setLinvel({ x: 0, y: 0, z: 0 }, true)
      }
    }
  }

  useFrame(() => {
    if (editable) return
    const current = body.current
    if (!current) return
    const translation = current.translation()
    const velocity = current.linvel()
    const speed = Math.hypot(velocity.x, velocity.y, velocity.z)
    const now = performance.now()
    if (speed > 0.045) {
      stillSince.current = 0
      return
    }
    if (!stillSince.current) stillSince.current = now
    if (now - stillSince.current < 900) return
    const saved = lastSaved.current
    if (
      Math.hypot(
        translation.x - saved[0],
        translation.y - saved[1],
        translation.z - saved[2],
      ) < 0.04
    ) {
      return
    }
    const next: Vec3 = [translation.x, translation.y, translation.z]
    lastSaved.current = next
    onMoveEnd(next)
  })

  return (
    <RigidBody
      ref={body}
      type={editable ? 'kinematicPosition' : 'dynamic'}
      colliders={false}
      position={editable ? localPosition : object.position}
      rotation={[0, object.rotation, 0]}
      mass={physics.mass}
      linearDamping={0.55}
      angularDamping={0.7}
      canSleep
    >
      <group
        scale={object.scale}
        onPointerDown={(event) => {
          event.stopPropagation()
          onSelect()
          if (!editable) return
          dragging.current = true
          setDragging(true)
        }}
        onPointerMove={move}
        onPointerUp={() => {
          if (!dragging.current) return
          dragging.current = false
          setDragging(false)
          onMoveEnd(localPosition)
        }}
      >
        {procedural ? (
          <ProceduralAsset state={object.state} />
        ) : glbUrl ? (
          <Suspense fallback={null}>
            <GlbAsset url={glbUrl} />
          </Suspense>
        ) : (
          <Asset />
        )}
        {selected && (
          <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.015, 0]}>
            <ringGeometry args={[0.55, 0.62, 36]} />
            <meshBasicMaterial color="#f0b86e" transparent opacity={0.85} depthWrite={false} />
          </mesh>
        )}
      </group>
      <CuboidCollider
        args={[
          physics.half[0] * object.scale,
          physics.half[1] * object.scale,
          physics.half[2] * object.scale,
        ]}
        position={[
          physics.center[0] * object.scale,
          physics.center[1] * object.scale,
          physics.center[2] * object.scale,
        ]}
        friction={0.8}
        restitution={0.04}
      />
    </RigidBody>
  )
}

function GlbAsset({ url }: { url: string }) {
  const gltf = useGLTFAsset(url)
  const scene = useMemo(() => {
    const clone = gltf.scene.clone(true)
    clone.traverse((node) => {
      if ((node as THREE.Mesh).isMesh) {
        ;(node as THREE.Mesh).castShadow = true
        ;(node as THREE.Mesh).receiveShadow = true
      }
    })
    return clone
  }, [gltf])
  return <primitive object={scene} />
}

function useGLTFAsset(url: string) {
  // 用三件套自带 loader 缓存，避免同一个 GLB 每次重渲染都重新下载。
  return useLoader(GLTFLoader, url)
}

export function WhiteRoom() {
  return (
    <>
      <color attach="background" args={['#f3f0ea']} />
      <fog attach="fog" args={['#f3f0ea', 7, 14]} />
      <ambientLight intensity={1.6} color="#fff6ea" />
      <directionalLight
        position={[3, 5, 2]}
        intensity={1.4}
        color="#fff3df"
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
      {/*
        白房间原来在 -0.02 高度同时放了一块平面地板和一个同高度的圆柱顶面，
        两块几何体完全共面，会形成一圈闪烁的 z-fighting“阴影圈”。这里只
        保留平面地板；不要再用共面圆柱做圆形地台。
      */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.02, 0]} receiveShadow>
        <planeGeometry args={[12, 12]} />
        <meshStandardMaterial color="#f7f4ee" roughness={0.92} />
      </mesh>
    </>
  )
}

/** 让编辑模式下的物件轻微浮动，方便一眼看出"这是可编辑的"。 */
export function EditGlow() {
  const ref = useRef<THREE.Mesh | null>(null)
  useFrame((state) => {
    if (ref.current) ref.current.rotation.z = Math.sin(state.clock.elapsedTime * 1.4) * 0.03
  })
  return (
    <mesh ref={ref} position={[0, 0.02, 0]} rotation={[-Math.PI / 2, 0, 0]}>
      <ringGeometry args={[0.58, 0.64, 40]} />
      <meshBasicMaterial color="#f0b86e" transparent opacity={0.5} depthWrite={false} />
    </mesh>
  )
}
