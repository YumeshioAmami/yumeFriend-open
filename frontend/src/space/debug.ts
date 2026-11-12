/** Phase A 运行期探针：给无头浏览器/本地排查读取相机、手、角色和物件的真实物理位置。 */

declare global {
  interface Window {
    __YF_SPACE_DEBUG__?: Record<string, unknown>
  }
}

export function updateSpaceDebug(patch: Record<string, unknown>) {
  if (typeof window === 'undefined') return
  const state = window.__YF_SPACE_DEBUG__ ?? {}
  Object.assign(state, patch)
  window.__YF_SPACE_DEBUG__ = state
}
