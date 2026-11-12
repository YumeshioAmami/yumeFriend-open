/** 小世界 v6 Phase A：房间与可编辑物件的静态碰撞体。
 *
 * 视觉模型仍由 Room / WorldObjectView 负责；这里只放不可见碰撞，
 * 让相机、角色控制器和动态物件有同一套物理边界。
 */

import { CuboidCollider, RigidBody } from '@react-three/rapier'

export function RoomColliders() {
  return (
    <RigidBody type="fixed" colliders={false}>
      {/* 地板 / 四墙 / 天花板 */}
      <CuboidCollider args={[5.1, 0.06, 5.1]} position={[0, -0.06, 0]} friction={0.9} />
      <CuboidCollider args={[5.1, 1.8, 0.06]} position={[0, 1.8, -5.05]} friction={0.8} />
      <CuboidCollider args={[0.06, 1.8, 5.1]} position={[-5.05, 1.8, 0]} friction={0.8} />
      <CuboidCollider args={[0.06, 1.8, 5.1]} position={[5.05, 1.8, 0]} friction={0.8} />
      <CuboidCollider args={[5.1, 1.8, 0.06]} position={[0, 1.8, 5.05]} friction={0.8} />
      <CuboidCollider args={[5.1, 0.06, 5.1]} position={[0, 3.6, 0]} friction={0.8} />

      {/* 暖色房间里的固定家具；白空间也保留这些不可见边界，避免飞出去。 */}
      <CuboidCollider args={[0.775, 0.14, 0.36]} position={[2.8, 0.2, -4.3]} friction={0.8} />
      <CuboidCollider args={[0.575, 0.05, 0.21]} position={[3.45, 0.7, 4.22]} friction={0.8} />
      <CuboidCollider args={[0.29, 1.05, 0.19]} position={[-4.0, 1.25, -4.1]} friction={0.8} />
      <CuboidCollider args={[0.29, 1.15, 0.05]} position={[-1.4, 1.15, 4.97]} friction={0.8} />
    </RigidBody>
  )
}
