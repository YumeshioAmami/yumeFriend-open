/** VRM 人形重定向：把 Quaternius / Rigify 风格的骨骼动画转到 VRM normalized bones。
 *
 * 素材：Quaternius Universal Animation Library（CC0 1.0），GLTF 版本。
 * 源模型不进入房间；这里把源骨骼相对静止姿态的**完整世界旋转差量**烘焙下来，
 * 再按目标骨骼的父级世界旋转换算成 VRM 局部四元数。
 */

import * as THREE from 'three'
import type { VRM } from '@pixiv/three-vrm'

const BONE_MAP: Record<string, string> = {
  hips: 'DEF-hips',
  spine: 'DEF-spine.001',
  chest: 'DEF-spine.002',
  upperChest: 'DEF-spine.003',
  neck: 'DEF-neck',
  head: 'DEF-head',
  leftShoulder: 'DEF-shoulder.L',
  leftUpperArm: 'DEF-upper_arm.L',
  leftLowerArm: 'DEF-forearm.L',
  leftHand: 'DEF-hand.L',
  rightShoulder: 'DEF-shoulder.R',
  rightUpperArm: 'DEF-upper_arm.R',
  rightLowerArm: 'DEF-forearm.R',
  rightHand: 'DEF-hand.R',
  leftUpperLeg: 'DEF-thigh.L',
  leftLowerLeg: 'DEF-shin.L',
  leftFoot: 'DEF-foot.L',
  leftToes: 'DEF-toe.L',
  rightUpperLeg: 'DEF-thigh.R',
  rightLowerLeg: 'DEF-shin.R',
  rightFoot: 'DEF-foot.R',
  rightToes: 'DEF-toe.R',
}

const PREFERRED_CLIPS = [
  'Idle_Loop',
  'Walk_Loop',
  'Sitting_Idle_Loop',
  'Sitting_Talking_Loop',
  'Sitting_Enter',
  'Sitting_Exit',
  'Interact',
  'PickUp_Table',
  'Idle_Talking_Loop',
  'Dance_Loop',
  'Jump_Start',
  'Jump_Loop',
  'Crouch_Fwd_Loop',
  'Crouch_Idle_Loop',
  'Punch_Cross',
  'Push_Loop',
]

export interface RetargetedClip {
  name: string
  duration: number
  fps: number
  /** 每根骨骼相对源静止姿态的世界旋转差量。 */
  frames: Record<string, THREE.Quaternion[]>
}

interface RetargetRig {
  scene: THREE.Object3D
  nodes: Map<string, THREE.Object3D>
  /** 目标骨骼在静止时的世界旋转。 */
  restWorldQ: Map<string, THREE.Quaternion>
  /** VRM 场景静止时的世界旋转；用来把根节点旋转带进每一帧。 */
  sceneRestWorldQ: THREE.Quaternion
}

const RIGS = new WeakMap<VRM, RetargetRig>()

function nodeMap(root: THREE.Object3D): Map<string, THREE.Object3D> {
  const result = new Map<string, THREE.Object3D>()
  root.traverse((node) => {
    if (node.name) result.set(node.name, node)
  })
  return result
}

export function buildRetargetedClips(
  sourceScene: THREE.Object3D,
  animations: THREE.AnimationClip[],
  vrm: VRM,
): Map<string, RetargetedClip> {
  const result = new Map<string, RetargetedClip>()
  const humanoid = (
    vrm as unknown as {
      humanoid?: { getNormalizedBoneNode: (name: string) => THREE.Object3D | null }
    }
  ).humanoid
  if (!humanoid) return result

  sourceScene.updateMatrixWorld(true)
  vrm.scene.updateMatrixWorld(true)
  const sourceNodes = nodeMap(sourceScene)
  const findSourceNode = (name: string) =>
    sourceNodes.get(name) ?? sourceNodes.get(THREE.PropertyBinding.sanitizeNodeName(name))

  const sceneRestWorldQ = new THREE.Quaternion()
  vrm.scene.getWorldQuaternion(sceneRestWorldQ)
  const sourceRestWorldQ = new Map<string, THREE.Quaternion>()
  const targetRestWorldQ = new Map<string, THREE.Quaternion>()
  const targetNodes = new Map<string, THREE.Object3D>()

  for (const [targetBone, sourceName] of Object.entries(BONE_MAP)) {
    const source = findSourceNode(sourceName)
    const target = humanoid.getNormalizedBoneNode(targetBone)
    if (!source || !target) continue
    const sourceQ = new THREE.Quaternion()
    const targetQ = new THREE.Quaternion()
    source.getWorldQuaternion(sourceQ)
    target.getWorldQuaternion(targetQ)
    sourceRestWorldQ.set(targetBone, sourceQ)
    targetRestWorldQ.set(targetBone, targetQ)
    targetNodes.set(targetBone, target)
  }
  if (targetNodes.size < 8) return result

  const mixer = new THREE.AnimationMixer(sourceScene)
  for (const clip of animations) {
    if (!PREFERRED_CLIPS.includes(clip.name)) continue
    mixer.stopAllAction()
    const action = mixer.clipAction(clip)
    action.reset().play()
    const fps = 30
    const frameCount = Math.max(2, Math.ceil(clip.duration * fps) + 1)
    const frames: Record<string, THREE.Quaternion[]> = {}
    for (const bone of targetNodes.keys()) frames[bone] = []

    for (let index = 0; index < frameCount; index += 1) {
      const time = (index / Math.max(1, frameCount - 1)) * clip.duration
      mixer.setTime(time)
      sourceScene.updateMatrixWorld(true)
      for (const [bone] of targetNodes) {
        const source = findSourceNode(BONE_MAP[bone])
        const sourceBase = sourceRestWorldQ.get(bone)
        if (!source || !sourceBase) continue
        const currentQ = new THREE.Quaternion()
        source.getWorldQuaternion(currentQ)
        frames[bone].push(currentQ.multiply(sourceBase.clone().invert()).normalize())
      }
    }
    result.set(clip.name, {
      name: clip.name,
      duration: clip.duration,
      fps,
      frames,
    })
  }
  mixer.stopAllAction()
  RIGS.set(vrm, {
    scene: vrm.scene,
    nodes: targetNodes,
    restWorldQ: targetRestWorldQ,
    sceneRestWorldQ,
  })
  return result
}

export function sampleRetargetedClip(
  clip: RetargetedClip | undefined,
  time: number,
  out: Map<string, THREE.Quaternion>,
): boolean {
  if (!clip) return false
  let sampled = false
  for (const [bone, frames] of Object.entries(clip.frames)) {
    if (frames.length < 2) continue
    const position = ((time % clip.duration) / clip.duration) * (frames.length - 1)
    const i0 = Math.floor(position)
    const i1 = Math.min(i0 + 1, frames.length - 1)
    const alpha = position - i0
    const target = out.get(bone) ?? new THREE.Quaternion()
    target.slerpQuaternions(frames[i0], frames[i1], alpha)
    out.set(bone, target)
    sampled = true
  }
  return sampled
}

export function applyRetargetedPose(
  vrm: VRM,
  pose: Map<string, THREE.Quaternion>,
): void {
  const rig = RIGS.get(vrm)
  if (!rig) return

  rig.scene.updateMatrixWorld(true)
  const sceneWorldQ = new THREE.Quaternion()
  rig.scene.getWorldQuaternion(sceneWorldQ)
  // 根节点/VRM0 场景在运行时的旋转，要整体带进骨骼世界姿态。
  const rootDeltaQ = sceneWorldQ
    .clone()
    .multiply(rig.sceneRestWorldQ.clone().invert())
    .normalize()
  const parentWorldQ = new THREE.Quaternion()

  for (const [bone, deltaWorldQ] of pose) {
    const node = rig.nodes.get(bone)
    const restWorldQ = rig.restWorldQ.get(bone)
    if (!node?.parent || !restWorldQ) continue

    node.parent.updateWorldMatrix(true, false)
    node.parent.getWorldQuaternion(parentWorldQ)

    // D * T0 是目标骨骼在“静止世界”里的新姿态；再乘根节点当前旋转。
    const desiredWorldQ = rootDeltaQ
      .clone()
      .multiply(deltaWorldQ)
      .multiply(restWorldQ)
      .normalize()
    node.quaternion.copy(parentWorldQ.invert().multiply(desiredWorldQ).normalize())
  }
  rig.scene.updateMatrixWorld(true)
}
