/**
 * 「刚生成的语音」名单（D88）。
 *
 * 为什么需要它：Level 1（边流边合成）让"拼好 → ready"变得很快，
 * `ready` 可能比前端的 `loadHistory` 还早到 —— 光看消息里的 `audio_status`，
 * 前端分不清"刚生成的"和"历史里的"，自动播放就会时有时无。
 *
 * 判据很干净：**只有 WebSocket 推来的 `ready` 才算"新鲜"**
 * （历史接口永远不会产生 WS 事件）。语音条渲染时来这儿领一次：
 * 领到 → 自动播；领不到（刷新 / 切朋友后的历史）→ 保持安静。
 *
 * 带 2 分钟保质期：切到别的朋友半小时后再回来，不会突然放出声。
 */
const TTL_MS = 2 * 60 * 1000
const fresh = new Map<string, number>()

export function markFreshAudio(messageId: string): void {
  if (!messageId) return
  const now = Date.now()
  fresh.set(messageId, now)
  // 顺手清掉过期的，别让这个 Map 一直长
  for (const [id, at] of fresh) {
    if (now - at > TTL_MS) fresh.delete(id)
  }
}

export function consumeFreshAudio(messageId: string): boolean {
  const at = fresh.get(messageId)
  if (at === undefined) return false
  fresh.delete(messageId) // 只领一次
  return Date.now() - at <= TTL_MS
}
