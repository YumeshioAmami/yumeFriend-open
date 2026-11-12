/** 小世界第一人称手部代理。
 *
 * 视觉手是从 CC0 Vivi 模型提取的右手 + 前臂（见 FirstPersonHand.LICENSE.txt）；
 * 物理仍是运动学手代理，负责推拉物件和触摸身体。
 */

import { memo, useEffect, useMemo, useRef } from 'react'
import { useFrame, useLoader, useThree } from '@react-three/fiber'
import {
  BallCollider,
  CuboidCollider,
  RigidBody,
  type RapierCollider,
  type RapierRigidBody,
} from '@react-three/rapier'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import * as THREE from 'three'

import { updateSpaceDebug } from './debug'
import type { ViewerSnapshot } from './freeFly'
import { pickGrabbable } from './grabRegistry'

const GRAB_DISTANCE = 3.1
const HAND_MODEL_PATH = '/models/FirstPersonHand.glb'

export const UserHand = memo(function UserHand({
  viewer,
  enabled,
  onGrabChange,
}: {
  viewer: React.MutableRefObject<ViewerSnapshot>
  enabled: boolean
  onGrabChange?: (holding: string | null) => void
}) {
  const { camera } = useThree()
  const body = useRef<RapierRigidBody | null>(null)
  const collider = useRef<RapierCollider | null>(null)
  const visual = useRef<THREE.Group | null>(null)
  const grabbed = useRef<RapierRigidBody | null>(null)
  const grabOffset = useRef(new THREE.Vector3())
  const handTarget = useRef(new THREE.Vector3())
  const desiredHand = useRef(new THREE.Vector3())
  const pushDir = useRef(new THREE.Vector3(0, 0, -1))
  const handReady = useRef(false)
  const lastHand = useRef(new THREE.Vector3())
  const handVelocity = useRef(new THREE.Vector3())
  const pressedAt = useRef(0)
  const movedDistance = useRef(0)
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled

  const handGltf = useLoader(GLTFLoader, HAND_MODEL_PATH)
  const handScene = useMemo(() => {
    const source = handGltf.scene.clone(true)
    const scene = new THREE.Group()
    scene.add(source)
    source.traverse((object: THREE.Object3D) => {
      const mesh = object as THREE.Mesh
      if (!mesh.isMesh) return
      mesh.frustumCulled = false
      mesh.castShadow = false
      mesh.receiveShadow = false
      const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
      for (const material of materials) {
        if (!material) continue
        const standard = material as THREE.MeshStandardMaterial
        standard.roughness = 0.64
        standard.metalness = 0
        standard.envMapIntensity = 0.8
        standard.emissive = new THREE.Color('#2a1611')
        standard.emissiveIntensity = 0.07
        material.side = THREE.DoubleSide
        material.depthWrite = true
        if (standard.map) standard.map.anisotropy = 4
      }
    })

    // GLB 的长轴是 +Y，手指朝 +Y；转成第一人称的“手指朝 -Z”。
    source.rotation.x = -Math.PI / 2
    scene.updateMatrixWorld(true)
    const rawBox = new THREE.Box3().setFromObject(scene)
    const rawSize = new THREE.Vector3()
    rawBox.getSize(rawSize)
    scene.updateMatrixWorld(true)
    const longest = Math.max(rawSize.x, rawSize.y, rawSize.z, 0.0001)
    scene.scale.setScalar(0.16 / longest)
    scene.updateMatrixWorld(true)
    const scaledBox = new THREE.Box3().setFromObject(scene)
    const center = new THREE.Vector3()
    scaledBox.getCenter(center)
    scene.position.x -= center.x
    scene.position.y -= center.y
    // 让指尖成为局部原点：视觉手跟随接触点时，手掌和前臂往相机方向延伸。
    scene.position.z -= scaledBox.min.z
    updateSpaceDebug({
      handModel: {
        scale: scene.scale.toArray().map((value) => Math.round(value * 10000) / 10000),
        size: rawSize.toArray().map((value) => Math.round(value * 10000) / 10000),
        contact: center.toArray().map((value) => Math.round(value * 10000) / 10000),
      },
    })
    return scene
  }, [handGltf])


  useEffect(() => {
    const isUiTarget = (target: EventTarget | null) => {
      if (!(target instanceof Element)) return false
      return Boolean(target.closest('button, input, textarea, select, a'))
    }

    const down = (event: PointerEvent) => {
      if (!enabledRef.current || event.button !== 0 || isUiTarget(event.target)) return
      pressedAt.current = performance.now()
      movedDistance.current = 0
      if (!body.current || !collider.current) return

      const origin = camera.position
      const direction = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion)
      const picked = pickGrabbable(
        [origin.x, origin.y, origin.z],
        [direction.x, direction.y, direction.z],
        GRAB_DISTANCE,
      )
      if (!picked) return
      const targetBody = picked.body
      grabbed.current = targetBody
      targetBody.wakeUp()
      targetBody.setGravityScale(0, true)
      const objectPosition = targetBody.translation()
      grabOffset.current.set(
        objectPosition.x - handTarget.current.x,
        objectPosition.y - handTarget.current.y,
        objectPosition.z - handTarget.current.z,
      )
      viewer.current.handActive = true
      viewer.current.handHolding = picked.label
      viewer.current.handContactZone = null
      viewer.current.handContactKind = null
      viewer.current.handContactSpeed = 0
      viewer.current.handContactSeconds = 0
      onGrabChange?.(viewer.current.handHolding)
    }

    const move = (event: PointerEvent) => {
      if (event.buttons & 1) {
        movedDistance.current += Math.abs(event.movementX) + Math.abs(event.movementY)
      }
    }

    const up = (event: PointerEvent) => {
      if (event.button !== 0) return
      if (grabbed.current) {
        const heavy = (grabbed.current.mass?.() ?? 1) > 5
        const throwVelocity = handVelocity.current
          .clone()
          .multiplyScalar(heavy ? 0.22 : 0.55)
        throwVelocity.clampLength(0, heavy ? 2.6 : 5.5)
        const currentVelocity = grabbed.current.linvel()
        grabbed.current.setLinvel(
          {
            x: currentVelocity.x + throwVelocity.x,
            y: currentVelocity.y + throwVelocity.y,
            z: currentVelocity.z + throwVelocity.z,
          },
          true,
        )
        grabbed.current.setGravityScale(1, true)
        grabbed.current = null
        viewer.current.handActive = false
        viewer.current.handHolding = null
        viewer.current.handContactZone = null
        viewer.current.handContactKind = null
        viewer.current.handContactSpeed = 0
        viewer.current.handContactSeconds = 0
        onGrabChange?.(null)
      } else if (performance.now() - pressedAt.current < 220 && movedDistance.current < 8) {
        viewer.current.lastMovedAt = performance.now()
      }
    }

    window.addEventListener('pointerdown', down)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointerdown', down)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      if (grabbed.current) {
        grabbed.current.setGravityScale(1, true)
        grabbed.current = null
        viewer.current.handActive = false
        viewer.current.handHolding = null
      }
    }
  }, [camera, onGrabChange, viewer])

  useEffect(() => {
    if (enabled || !grabbed.current) return
    grabbed.current.setGravityScale(1, true)
    grabbed.current = null
    viewer.current.handActive = false
    viewer.current.handHolding = null
    onGrabChange?.(null)
  }, [enabled, onGrabChange, viewer])

  useFrame((_, delta) => {
    const dt = Math.min(delta, 0.05)
    const now = performance.now()
    const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion)
    const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion)
    const up = new THREE.Vector3(0, 1, 0)
    const down = new THREE.Vector3(0, -1, 0)
    const contactZone = viewer.current.handContactZone
    const petting =
      Boolean(contactZone) &&
      !grabbed.current &&
      (viewer.current.handContactKind === 'hold' ||
        viewer.current.handContactSpeed < 0.22)
    const pettingOffset = new THREE.Vector3()

    const reach = viewer.current.handReachOffset
    if (reach) {
      // 手伸到她真正被命中的体表位置：落点存成相机本地偏移，
      // 这样手仍然跟着相机移动/拖拽，而不是钉死在世界里。
      desiredHand.current
        .copy(camera.position)
        .add(
          new THREE.Vector3(reach[0], reach[1], reach[2]).applyQuaternion(
            camera.quaternion,
          ),
        )
      pushDir.current.copy(desiredHand.current).sub(camera.position)
      if (pushDir.current.lengthSq() < 0.000001) pushDir.current.copy(forward)
      pushDir.current.normalize()
      if (petting) {
        pettingOffset.addScaledVector(right, Math.sin(now * 0.0018) * 0.026)
        pettingOffset.addScaledVector(up, Math.cos(now * 0.0024) * 0.011)
        pettingOffset.addScaledVector(
          pushDir.current,
          Math.sin(now * 0.0011) * 0.004,
        )
      }
    } else {
      desiredHand.current
        .copy(camera.position)
        .addScaledVector(forward, 0.66)
        .addScaledVector(right, 0.18)
        .addScaledVector(down, 0.22)
      pushDir.current.copy(forward)
    }

    if (!handReady.current) {
      handTarget.current.copy(desiredHand.current)
      handReady.current = true
    } else {
      handTarget.current.lerp(
        desiredHand.current,
        1 - Math.exp(-dt * (reach ? 22 : 14)),
      )
    }
    viewer.current.handTarget = [
      desiredHand.current.x,
      desiredHand.current.y,
      desiredHand.current.z,
    ]

    const handBody = body.current
    if (handBody) {
      handBody.setNextKinematicTranslation({
        x: handTarget.current.x,
        y: handTarget.current.y,
        z: handTarget.current.z,
      })
      handBody.setNextKinematicRotation({
        x: camera.quaternion.x,
        y: camera.quaternion.y,
        z: camera.quaternion.z,
        w: camera.quaternion.w,
      })
      const actual = handBody.translation()
      updateSpaceDebug({ handBody: [actual.x, actual.y, actual.z] })
    }

    if (visual.current) {
      visual.current.position.copy(handTarget.current)
      if (reach) {
        // 指尖在体表；再退 1cm，避免手模压进皮肤。
        visual.current.position.addScaledVector(pushDir.current, -0.01)
      }
      // 抚摸只移动视觉手，物理抓取点保持稳定。
      visual.current.position.add(pettingOffset)
      const strokeRoll = petting ? Math.sin(now * 0.0018) * 0.055 : 0
      const strokeYaw = petting ? Math.cos(now * 0.0014) * 0.045 : 0
      const baseQuaternion = new THREE.Quaternion()
      baseQuaternion.copy(camera.quaternion)
      visual.current.quaternion
        .copy(baseQuaternion)
        .multiply(
          new THREE.Quaternion().setFromEuler(
            new THREE.Euler(strokeYaw, strokeRoll * 0.35, strokeRoll, 'YXZ'),
          ),
        )
    }

    const moved = handTarget.current.clone().sub(lastHand.current)
    handVelocity.current
      .copy(moved)
      .multiplyScalar(1 / Math.max(0.001, dt))
      .clampLength(0, 10)
    if (handTarget.current.distanceToSquared(lastHand.current) > 0.000002) {
      lastHand.current.copy(handTarget.current)
      viewer.current.lastMovedAt = now
    }
    viewer.current.hand = [handTarget.current.x, handTarget.current.y, handTarget.current.z]
    updateSpaceDebug({
      hand: viewer.current.hand,
      handTarget: viewer.current.handTarget,
      handReachOffset: viewer.current.handReachOffset,
      handVisual: visual.current
        ? visual.current.position.toArray().map((value) => Math.round(value * 1000) / 1000)
        : null,
      handHolding: viewer.current.handHolding,
      handActive: viewer.current.handActive,
      handGrip: grabbed.current ? 1 : contactZone ? 0.1 : 0.04,
      handPetting: petting,
      handPettingOffset: pettingOffset
        .toArray()
        .map((value) => Math.round(value * 10000) / 10000),
    })
    const held = grabbed.current
    if (!held) return
    if (!held.isValid()) {
      grabbed.current = null
      viewer.current.handActive = false
      viewer.current.handHolding = null
      onGrabChange?.(null)
      return
    }

    // 弹簧抓取：物件仍然受碰撞约束，不会瞬移到手里。
    const target = handTarget.current.clone().add(grabOffset.current)
    const translation = held.translation()
    const deltaToTarget = target.sub(new THREE.Vector3(translation.x, translation.y, translation.z))
    const distance = deltaToTarget.length()
    const mass = held.mass?.() ?? 1
    const heavy = mass > 5
    if (distance > (heavy ? 3.4 : 2.4)) {
      held.setGravityScale(1, true)
      grabbed.current = null
      viewer.current.handActive = false
      viewer.current.handHolding = null
      onGrabChange?.(null)
      return
    }
    const desiredVelocity = deltaToTarget.multiplyScalar(heavy ? 6 : 9)
    if (desiredVelocity.length() > (heavy ? 2.8 : 4.5)) {
      desiredVelocity.setLength(heavy ? 2.8 : 4.5)
    }
    const currentVelocity = held.linvel()
    const blend = 1 - Math.exp(-dt * 14)
    held.setLinvel(
      {
        x: currentVelocity.x + (desiredVelocity.x - currentVelocity.x) * blend,
        y: currentVelocity.y + (desiredVelocity.y - currentVelocity.y) * blend,
        z: currentVelocity.z + (desiredVelocity.z - currentVelocity.z) * blend,
      },
      true,
    )
    const angular = held.angvel()
    held.setAngvel(
      { x: angular.x * 0.86, y: angular.y * 0.86, z: angular.z * 0.86 },
      true,
    )
  })

  return (
    <>
      <RigidBody ref={body} type="kinematicPosition" colliders={false} canSleep={false}>
        <CuboidCollider
          ref={collider}
          args={[0.055, 0.032, 0.1]}
          position={[0, 0.01, 0.065]}
          friction={0.55}
          restitution={0.03}
        />
        <BallCollider
          args={[0.055]}
          position={[0, 0.012, -0.008]}
          friction={0.55}
          restitution={0.03}
        />
      </RigidBody>
      <group ref={visual}>
        <primitive object={handScene} />
      </group>
    </>
  )
})
