/** 小世界 v6 Phase A：六自由度自由飞行相机。
 *
 * 相机不接重力，WASD 平移、Q/E 升降、鼠标看向；默认用 Rapier
 * 对静态碰撞体做球体扫掠，避免穿过墙和家具。V 键可切换穿墙。
 */

import { useEffect, useLayoutEffect, useRef, type MutableRefObject } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import {
  BallCollider,
  RigidBody,
  useRapier,
  type RapierCollider,
  type RapierRigidBody,
} from '@react-three/rapier'
import * as THREE from 'three'

import { normalizeAngle } from './cameraMath'
import { updateSpaceDebug } from './debug'

export interface ViewerSnapshot {
  camera: [number, number, number]
  look: [number, number, number]
  hand: [number, number, number]
  /** 手这一帧“想去”的位置；抓取时它就是贴在她身上的落点。 */
  handTarget: [number, number, number]
  /** 抓住她后，手落点相对相机的本地偏移；用于手跟着相机拖拽。 */
  handReachOffset: [number, number, number] | null
  /** 手正在抓东西，或刚发生推/抓动作。 */
  handActive: boolean
  /** 当前抓着什么；供房间对话和后续提示词使用。 */
  handHolding: string | null
  /** 当前是否直接抓着她，以及抓的是哪个身体区域。 */
  handContactZone: string | null
  /** 抓着不放 / 推 / 拉 / 带着移动。 */
  handContactKind: 'hold' | 'drag' | 'push' | 'pull' | null
  handContactSpeed: number
  handContactSeconds: number
  /** 相机或手最近一次明显移动的 performance.now()。 */
  lastMovedAt: number
}

export function createViewerSnapshot(): ViewerSnapshot {
  return {
    camera: [0, 1.5, 3.6],
    look: [0, 0, -1],
    hand: [0.18, 1.32, 3.1],
    handTarget: [0.18, 1.32, 3.1],
    handReachOffset: null,
    handActive: false,
    handHolding: null,
    handContactZone: null,
    handContactKind: null,
    handContactSpeed: 0,
    handContactSeconds: 0,
    lastMovedAt: 0,
  }
}

const ROOM_MIN = -4.78
const ROOM_MAX = 4.78
const EYE_MIN = 0.28
const EYE_MAX = 3.38
const LOOK_SENSITIVITY = 0.0017
const MAX_MOUSE_STEP = 64
const MAX_FRAME_LOOK_STEP = 84
const DEFAULT_CAMERA_POSITION = new THREE.Vector3(0, 1.5, 3.6)
const DEFAULT_CAMERA_PITCH = -0.14

/**
 * 相机姿态存在模块级，而不是只存在组件 ref 里。
 *
 * R3F 在窗口尺寸变化、WebGL 上下文恢复、或者上层组件意外重建时可能把
 * Canvas 子树重新挂载一次；如果姿态只存在 FreeFlyControls 的 useRef 里，
 * 新实例会从出生点重新 lookAt，表现就是“视角突然重置”。
 * 这里把最近一帧的真实姿态留下来，任何新实例都先恢复它。
 */
const cameraMemory = {
  ready: false,
  position: new THREE.Vector3(),
  yaw: 0,
  pitch: DEFAULT_CAMERA_PITCH,
}

function rememberCamera(camera: THREE.Camera, yaw: number, pitch: number) {
  cameraMemory.ready = true
  cameraMemory.position.copy(camera.position)
  cameraMemory.yaw = yaw
  cameraMemory.pitch = pitch
}

export function FreeFlyControls({
  viewer,
  noclip,
  paused,
  onToggleNoclip,
  onLockChange,
  touchMode = false,
  touchMove,
  touchVertical,
}: {
  viewer: React.MutableRefObject<ViewerSnapshot>
  noclip: boolean
  paused: boolean
  onToggleNoclip: () => void
  onLockChange: (locked: boolean) => void
  /** 手机端没有鼠标锁定：单指拖动转视角，虚拟摇杆负责移动。 */
  touchMode?: boolean
  touchMove?: MutableRefObject<{ x: number; y: number }>
  touchVertical?: MutableRefObject<number>
}) {
  const { camera, gl } = useThree()
  const { world } = useRapier()
  const body = useRef<RapierRigidBody | null>(null)
  const collider = useRef<RapierCollider | null>(null)
  const yaw = useRef(0)
  const pitch = useRef(0)
  const pendingLook = useRef(new THREE.Vector2())
  const lastPendingLook = useRef(new THREE.Vector2())
  const keys = useRef(new Set<string>())
  const velocity = useRef(new THREE.Vector3())
  const lastCamera = useRef(new THREE.Vector3())
  const lastMoveAt = useRef(0)
  const noclipRef = useRef(noclip)
  const pausedRef = useRef(paused)
  const onToggleNoclipRef = useRef(onToggleNoclip)
  const onLockChangeRef = useRef(onLockChange)
  const touchModeRef = useRef(touchMode)
  const touchMoveRef = useRef(touchMove)
  const touchVerticalRef = useRef(touchVertical)
  const character = useRef<ReturnType<typeof world.createCharacterController> | null>(null)
  const initialized = useRef(false)

  noclipRef.current = noclip
  pausedRef.current = paused
  onToggleNoclipRef.current = onToggleNoclip
  onLockChangeRef.current = onLockChange
  touchModeRef.current = touchMode
  touchMoveRef.current = touchMove
  touchVerticalRef.current = touchVertical

  useEffect(() => {
    const controller = world.createCharacterController(0.02)
    controller.setApplyImpulsesToDynamicBodies(false)
    controller.setSlideEnabled(true)
    controller.enableAutostep(0.18, 0.16, false)
    controller.enableSnapToGround(0.08)
    character.current = controller
    return () => {
      controller.free()
      character.current = null
    }
  }, [world])

  useLayoutEffect(() => {
    if (initialized.current) return
    initialized.current = true
    camera.rotation.order = 'YXZ'
    if (cameraMemory.ready) {
      camera.position.copy(cameraMemory.position)
      yaw.current = cameraMemory.yaw
      pitch.current = cameraMemory.pitch
    } else {
      camera.position.copy(DEFAULT_CAMERA_POSITION)
      camera.lookAt(0, 1.0, 0)
      yaw.current = camera.rotation.y
      pitch.current = camera.rotation.x
    }
    camera.rotation.set(pitch.current, yaw.current, 0, 'YXZ')
    lastCamera.current.copy(camera.position)
    body.current?.setTranslation(
      { x: camera.position.x, y: camera.position.y, z: camera.position.z },
      true,
    )
    rememberCamera(camera, yaw.current, pitch.current)
  }, [camera, viewer])

  useEffect(() => {
    const canvas = gl.domElement

    const requestLook = (event: MouseEvent) => {
      if (event.button !== 0 || pausedRef.current) return
      const target = event.target as HTMLElement | null
      if (target?.closest('button, input, textarea, select, a')) return
      if (document.pointerLockElement !== canvas) {
        const result = canvas.requestPointerLock()
        if (result && typeof result.catch === 'function') {
          void result.catch(() => onLockChangeRef.current(false))
        }
      }
    }
    const onMouseMove = (event: MouseEvent) => {
      if (document.pointerLockElement !== canvas) return
      const dx = THREE.MathUtils.clamp(
        event.movementX,
        -MAX_MOUSE_STEP,
        MAX_MOUSE_STEP,
      )
      const dy = THREE.MathUtils.clamp(
        event.movementY,
        -MAX_MOUSE_STEP,
        MAX_MOUSE_STEP,
      )
      pendingLook.current.x += dx
      pendingLook.current.y += dy
      viewer.current.lastMovedAt = performance.now()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName?.toLowerCase()
      if (pausedRef.current || tag === 'input' || tag === 'textarea') return
      keys.current.add(event.code)
      if (event.code === 'KeyV') {
        event.preventDefault()
        onToggleNoclipRef.current()
      }
      if (event.code === 'KeyR') {
        event.preventDefault()
        camera.position.copy(DEFAULT_CAMERA_POSITION)
        yaw.current = 0
        pitch.current = DEFAULT_CAMERA_PITCH
        pendingLook.current.set(0, 0)
        lastPendingLook.current.set(0, 0)
        velocity.current.set(0, 0, 0)
        body.current?.setTranslation({ x: 0, y: 1.5, z: 3.6 }, true)
        rememberCamera(camera, yaw.current, pitch.current)
      }
    }
    const onKeyUp = (event: KeyboardEvent) => keys.current.delete(event.code)
    const clearKeys = () => keys.current.clear()
    const onLockChange = () => {
      const locked = document.pointerLockElement === canvas
      keys.current.clear()
      pendingLook.current.set(0, 0)
      lastPendingLook.current.set(0, 0)
      if (!locked) velocity.current.set(0, 0, 0)
      onLockChangeRef.current(locked)
    }
    const onLockError = () => {
      keys.current.clear()
      pendingLook.current.set(0, 0)
      lastPendingLook.current.set(0, 0)
      velocity.current.set(0, 0, 0)
      onLockChangeRef.current(false)
    }

    let touchPointer: number | null = null
    let touchLastX = 0
    let touchLastY = 0
    const onTouchPointerDown = (event: PointerEvent) => {
      if (!touchModeRef.current || event.pointerType === 'mouse') return
      if (touchPointer !== null) return
      touchPointer = event.pointerId
      touchLastX = event.clientX
      touchLastY = event.clientY
      canvas.setPointerCapture?.(event.pointerId)
      onLockChangeRef.current(true)
      event.preventDefault()
    }
    const onTouchPointerMove = (event: PointerEvent) => {
      if (touchPointer !== event.pointerId) return
      const dx = THREE.MathUtils.clamp(event.clientX - touchLastX, -MAX_MOUSE_STEP, MAX_MOUSE_STEP)
      const dy = THREE.MathUtils.clamp(event.clientY - touchLastY, -MAX_MOUSE_STEP, MAX_MOUSE_STEP)
      touchLastX = event.clientX
      touchLastY = event.clientY
      pendingLook.current.x += dx * 1.35
      pendingLook.current.y += dy * 1.35
      viewer.current.lastMovedAt = performance.now()
      event.preventDefault()
    }
    const endTouch = (event: PointerEvent) => {
      if (touchPointer !== event.pointerId) return
      touchPointer = null
      canvas.releasePointerCapture?.(event.pointerId)
      pendingLook.current.set(0, 0)
      lastPendingLook.current.set(0, 0)
      velocity.current.set(0, 0, 0)
    }

    canvas.addEventListener('mousedown', requestLook)
    canvas.addEventListener('pointerdown', onTouchPointerDown, { passive: false })
    canvas.addEventListener('pointermove', onTouchPointerMove, { passive: false })
    canvas.addEventListener('pointerup', endTouch)
    canvas.addEventListener('pointercancel', endTouch)
    document.addEventListener('pointerlockchange', onLockChange)
    document.addEventListener('pointerlockerror', onLockError)
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', clearKeys)
    return () => {
      canvas.removeEventListener('mousedown', requestLook)
      canvas.removeEventListener('pointerdown', onTouchPointerDown)
      canvas.removeEventListener('pointermove', onTouchPointerMove)
      canvas.removeEventListener('pointerup', endTouch)
      canvas.removeEventListener('pointercancel', endTouch)
      document.removeEventListener('pointerlockchange', onLockChange)
      document.removeEventListener('pointerlockerror', onLockError)
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', clearKeys)
      if (document.pointerLockElement === canvas) document.exitPointerLock()
    }
  }, [body, camera, gl, viewer])

  useFrame((_, delta) => {
    const dt = Math.min(delta, 0.05)
    yaw.current = normalizeAngle(yaw.current)
    // 鼠标事件先按帧聚一次，再做一帧轻量平均：
    // 低帧率时不会因为事件扎堆突然甩一大下，也不会像阻尼那样“先慢后追”。
    const rawLook = pendingLook.current
    pendingLook.current = new THREE.Vector2(0, 0)
    if (Math.abs(rawLook.x) + Math.abs(rawLook.y) >= 0.5) {
      const frameLook = new THREE.Vector2(
        THREE.MathUtils.clamp(
          rawLook.x * 0.72 + lastPendingLook.current.x * 0.28,
          -MAX_FRAME_LOOK_STEP,
          MAX_FRAME_LOOK_STEP,
        ),
        THREE.MathUtils.clamp(
          rawLook.y * 0.72 + lastPendingLook.current.y * 0.28,
          -MAX_FRAME_LOOK_STEP,
          MAX_FRAME_LOOK_STEP,
        ),
      )
      lastPendingLook.current.copy(rawLook)
      yaw.current = normalizeAngle(
        yaw.current - frameLook.x * LOOK_SENSITIVITY,
      )
      pitch.current = THREE.MathUtils.clamp(
        pitch.current - frameLook.y * LOOK_SENSITIVITY,
        -Math.PI / 2 + 0.05,
        Math.PI / 2 - 0.05,
      )
    } else {
      lastPendingLook.current.set(0, 0)
    }
    camera.rotation.set(pitch.current, yaw.current, 0, 'YXZ')

    const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion)
    const right = new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion)
    const up = new THREE.Vector3(0, 1, 0)
    const move = new THREE.Vector3()

    if (keys.current.has('KeyW')) move.add(forward)
    if (keys.current.has('KeyS')) move.sub(forward)
    if (keys.current.has('KeyD')) move.add(right)
    if (keys.current.has('KeyA')) move.sub(right)
    if (keys.current.has('KeyE') || keys.current.has('Space')) move.add(up)
    if (keys.current.has('KeyQ') || keys.current.has('ShiftLeft') || keys.current.has('ShiftRight')) {
      move.sub(up)
    }

    const joystick = touchMoveRef.current?.current
    if (joystick) {
      move.addScaledVector(forward, joystick.y)
      move.addScaledVector(right, joystick.x)
    }
    const vertical = touchVerticalRef.current?.current ?? 0
    if (vertical !== 0) move.addScaledVector(up, vertical)

    const speed = keys.current.has('ControlLeft') ? 5.2 : touchModeRef.current ? 2.35 : 3.0
    if (pausedRef.current) {
      velocity.current.multiplyScalar(Math.exp(-dt * 24))
    } else if (move.lengthSq() > 0) {
      move.normalize().multiplyScalar(speed * dt)
      velocity.current.lerp(move, 1 - Math.exp(-dt * 16))
    } else {
      velocity.current.multiplyScalar(Math.exp(-dt * 12))
    }

    let next = camera.position.clone().add(velocity.current)
    const controller = character.current
    const cameraBody = body.current
    const cameraCollider = collider.current
    if (
      !noclipRef.current &&
      controller &&
      cameraBody &&
      cameraCollider &&
      velocity.current.lengthSq() > 0.0000001
    ) {
      // 用带半径的运动学球体扫掠；撞墙时沿表面滑动，而不是整块速度被吃掉。
      controller.computeColliderMovement(
        cameraCollider,
        { x: velocity.current.x, y: velocity.current.y, z: velocity.current.z },
        undefined,
        undefined,
        (candidate) => candidate.parent()?.bodyType() !== 2,
      )
      const movement = controller.computedMovement()
      next = camera.position.clone().add(
        new THREE.Vector3(movement.x, movement.y, movement.z),
      )
      if (Math.hypot(movement.x, movement.y, movement.z) < 0.00025) {
        velocity.current.multiplyScalar(0.15)
      }
    }
    next.set(
      THREE.MathUtils.clamp(next.x, ROOM_MIN, ROOM_MAX),
      THREE.MathUtils.clamp(next.y, EYE_MIN, EYE_MAX),
      THREE.MathUtils.clamp(next.z, ROOM_MIN, ROOM_MAX),
    )
    if (!Number.isFinite(next.x) || !Number.isFinite(next.y) || !Number.isFinite(next.z)) {
      next.copy(lastCamera.current)
      velocity.current.set(0, 0, 0)
    }
    camera.position.copy(next)
    cameraBody?.setNextKinematicTranslation({ x: next.x, y: next.y, z: next.z })
    rememberCamera(camera, yaw.current, pitch.current)

    const now = performance.now()
    if (next.distanceToSquared(lastCamera.current) > 0.000002) {
      lastMoveAt.current = now
      lastCamera.current.copy(next)
    }
    const look = new THREE.Vector3(0, 0, -1).applyQuaternion(camera.quaternion)
    viewer.current.camera = [next.x, next.y, next.z]
    viewer.current.look = [look.x, look.y, look.z]
    if (now - lastMoveAt.current < 350) viewer.current.lastMovedAt = now
    updateSpaceDebug({ camera: viewer.current.camera, look: viewer.current.look })
  })

  return (
    <RigidBody
      ref={body}
      type="kinematicPosition"
      colliders={false}
      canSleep={false}
      position={[camera.position.x, camera.position.y, camera.position.z]}
    >
      <BallCollider ref={collider} args={[0.22]} friction={0.05} restitution={0} sensor />
    </RigidBody>
  )
}
