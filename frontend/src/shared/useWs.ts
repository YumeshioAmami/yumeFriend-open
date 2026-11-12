/**
 * 主动消息推送（api.md §7）。
 *
 * 只做一件事：连 `/ws`，把 `new_message` 事件交给回调。
 * 断线自动重连（退避到最多 6 秒），因为朋友可能十几分钟后才冒泡，
 * 中间断一次不该让人错过消息。
 */

import { useEffect, useRef } from 'react'

import { markFreshAudio } from './autoPlay'
import type { Message } from './api'

export interface ProactiveEvent {
  friendId: string
  friendName: string
  message: Message
}

/** 语音合成状态变了（v2 / T2.0-5）：只更新那一条消息上的语音条。 */
export interface AudioStatusEvent {
  friendId: string
  messageId: string
  audioStatus: 'pending' | 'ready' | 'failed' | null
  audioUrl: string | null
  audioDurationMs: number | null
  audioError: string | null
}

/** v2.1（D99）：她要做"改动磁盘"的事 → 弹卡片让用户点头。 */
export interface ApprovalEvent {
  id: string
  friendId: string
  friendName: string
  tool: string
  summary: string
  detail: string
  expiresIn: number
}

/** v2.2（D105）：她的**某一段**语音好了 → 沉浸模式"一段一段说"。 */
export interface AudioSegmentEvent {
  friendId: string
  messageId: string
  index: number
  audioUrl: string | null
  audioDurationMs: number | null
  text: string
  /** D112：这一段的情绪（她每段末尾给了标记） */
  mood?: string | null
}

export function useProactiveFeed(
  onEvent: (event: ProactiveEvent) => void,
  onAudioStatus?: (event: AudioStatusEvent) => void,
  onApproval?: (event: ApprovalEvent) => void,
  onAudioSegment?: (event: AudioSegmentEvent) => void,
) {
  const handler = useRef(onEvent)
  const audioHandler = useRef(onAudioStatus)
  const approvalHandler = useRef(onApproval)
  const segmentHandler = useRef(onAudioSegment)
  useEffect(() => {
    handler.current = onEvent
    audioHandler.current = onAudioStatus
    approvalHandler.current = onApproval
    segmentHandler.current = onAudioSegment
  })

  useEffect(() => {
    let socket: WebSocket | null = null
    let closed = false
    let attempt = 0
    let timer: number | null = null

    const connect = () => {
      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${scheme}://${window.location.host}/ws`)

      socket.onopen = () => {
        attempt = 0
      }
      socket.onmessage = (raw) => {
        try {
          const data = JSON.parse(raw.data) as Record<string, unknown>
          if (data.type === 'approval_request') {
            approvalHandler.current?.({
              id: String(data.id ?? ''),
              friendId: String(data.friend_id ?? ''),
              friendName: String(data.friend_name ?? ''),
              tool: String(data.tool ?? ''),
              summary: String(data.summary ?? ''),
              detail: String(data.detail ?? ''),
              expiresIn: Number(data.expires_in ?? 180),
            })
            return
          }
          if (data.type === 'audio_status') {
            // D88：只有 WS 推来的 ready 才算"刚生成的"（历史不会有 WS 事件）
            if (data.audio_status === 'ready') markFreshAudio(String(data.message_id ?? ''))
            audioHandler.current?.({
              friendId: String(data.friend_id ?? ''),
              messageId: String(data.message_id ?? ''),
              audioStatus: (data.audio_status as AudioStatusEvent['audioStatus']) ?? null,
              audioUrl: (data.audio_url as string | null) ?? null,
              audioDurationMs: (data.audio_duration_ms as number | null) ?? null,
              audioError: (data.audio_error as string | null) ?? null,
            })
            return
          }
          if (data.type === 'audio_segment') {
            // D107：第一段到了就记"新鲜" —— 普通聊天/沉浸模式据此自动开播
            if (Number(data.index ?? 0) === 0) markFreshAudio(String(data.message_id ?? ''))
            segmentHandler.current?.({
              friendId: String(data.friend_id ?? ''),
              messageId: String(data.message_id ?? ''),
              index: Number(data.index ?? 0),
              audioUrl: (data.audio_url as string | null) ?? null,
              audioDurationMs: (data.audio_duration_ms as number | null) ?? null,
              text: String(data.text ?? ''),
              mood: (data.mood as string | null) ?? null,
            })
            return
          }
          if (data.type !== 'new_message') return
          handler.current({
            friendId: String(data.friend_id ?? ''),
            friendName: String(data.friend_name ?? ''),
            message: data.message as Message,
          })
        } catch {
          // 收到看不懂的东西就忽略，别把整个页面搞崩
        }
      }
      socket.onclose = () => {
        if (closed) return
        attempt = Math.min(attempt + 1, 6)
        timer = window.setTimeout(connect, attempt * 1000)
      }
    }

    connect()
    return () => {
      closed = true
      if (timer) window.clearTimeout(timer)
      socket?.close()
    }
  }, [])
}

/** 系统通知（Windows 原生通知在浏览器里就是 Web Notifications）。 */
export function notifySystem(title: string, body: string, onClick?: () => void) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return
  try {
    const notification = new Notification(title, { body, tag: title })
    if (onClick) notification.onclick = onClick
  } catch {
    // 通知失败不影响收消息
  }
}

/**
 * 是不是跑在**桌面外壳**里（pywebview 开的窗口）。
 *
 * 桌面外壳由 `backend/desktop/app.py` 用 `?shell=1` 打开。
 * 认出它有什么用：桌面外壳有**我们自己画的弹窗**（D70），
 * 所以前端就别再去要系统通知权限了 —— 两套一起弹会重复，
 * 而且用户明确说了不要 Windows 系统通知。
 */
export function inDesktopShell(): boolean {
  try {
    return new URLSearchParams(window.location.search).has('shell')
  } catch {
    return false
  }
}

export function notificationPermission(): NotificationPermission | 'unsupported' {
  if (!('Notification' in window)) return 'unsupported'
  return Notification.permission
}
