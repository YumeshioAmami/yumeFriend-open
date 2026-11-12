/** 聊天节奏的纯函数（D345）：方便单测，不依赖 React / DOM。 */

import type { Message } from './api'

export const BUBBLE_GAP_MIN_MS = 5000
export const BUBBLE_GAP_MAX_MS = 6000
/** 最多 3 泡 + 首泡延迟，留出足够缓冲后再判定一轮展示结束。 */
export const BUBBLE_PACING_TIMEOUT_MS = 25000

/** 实时流里，相邻助手气泡的最短展示间隔。 */
export function bubbleGapMs(random: () => number = Math.random): number {
  const value = Math.min(1, Math.max(0, random()))
  return Math.round(BUBBLE_GAP_MIN_MS + value * (BUBBLE_GAP_MAX_MS - BUBBLE_GAP_MIN_MS))
}

/** 还有未展示的气泡时显示“正在输入中...”；首泡前和分割消息之间都要有。 */
export function shouldShowTypingBubble(rawCount: number, visibleCount: number): boolean {
  return rawCount > 0 && visibleCount < rawCount
}

export function mergeInputTexts(texts: string[]): string {
  return texts.map((text) => text.trim()).filter(Boolean).join('\n')
}

export function newPendingId(): string {
  return `pending-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

/** 同一朋友只保留一个 pending 气泡：连续输入时更新内容，保留原来的创建时间。 */
export function upsertPendingMessage(list: Message[], pending: Message): Message[] {
  const index = list.findIndex((message) => message.id === pending.id)
  if (index < 0) return [...list, pending]
  const next = [...list]
  next[index] = { ...pending, created_at: list[index].created_at }
  return next
}

/** 只结算指定的 pending，不能误伤同一朋友后来排队的另一个 pending。 */
export function settlePendingMessage(
  list: Message[],
  pendingId: string,
  realId: string,
): Message[] {
  let hit = false
  const next = list.map((message) => {
    if (message.id !== pendingId) return message
    hit = true
    return { ...message, id: realId }
  })
  return hit ? next : list
}
