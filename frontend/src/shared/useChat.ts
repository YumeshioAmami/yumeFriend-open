/**
 * 聊天状态（frontend.md §2 的状态机 + §3.1 的"切朋友不打断流"）。
 *
 * 关键设计：**流不挂在某个组件上**，而是按 friendId 存在这里。
 * 所以切走再切回来，流还在继续收（frontend.md §3.1 明确要求）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { api, type Message } from './api'
import {
  BUBBLE_PACING_TIMEOUT_MS,
  bubbleGapMs,
  mergeInputTexts,
  newPendingId,
  settlePendingMessage,
  upsertPendingMessage,
} from './chatRhythm'

export type StreamStatus =
  | 'idle'
  | 'sending'
  | 'thinking'
  | 'streaming'
  | 'tool_running'
  | 'error'
  | 'partial'

export interface StreamState {
  status: StreamStatus
  content: string
  reasoning: string
  tools: { id: string; name: string; args: unknown }[]
  error: string | null
  lastUserText: string | null
  /** D110：这一轮的助手消息 id（流一开始就有）—— 分段语音靠它精确对号入座 */
  assistantId: string | null
  /** D112/D115：她**每段**给的表情 + 落在干净文本里的偏移（沉浸模式按偏移对段） */
  moods: { mood: string; at: number }[]
  /** v6.0（D298）：这一轮的多气泡内容（空数组 = 还没开始） */
  segments: string[]
  /** D345：已经展示到第几个气泡；后面的实时内容先缓冲，避免一口气刷完 */
  visibleSegmentCount: number
}

export const IDLE_STREAM: StreamState = {
  status: 'idle',
  content: '',
  reasoning: '',
  tools: [],
  error: null,
  lastUserText: null,
  assistantId: null,
  moods: [],
  segments: [],
  visibleSegmentCount: 0,
}

const PAGE_SIZE = 30

type SendOptions = { imageId?: string; previewUrl?: string; screen?: boolean }

type OutboxEntry = {
  texts: string[]
  options: SendOptions
  timer: number | null
  ready: boolean
  pendingId: string
}

type BubblePace = {
  visible: number
  rawCount: number
  timer: number | null
  waiters: (() => void)[]
}

function pendingMessage(id: string, text: string): Message {
  return {
    id,
    role: 'user',
    content: text,
    mode_at_send: null,
    is_proactive: false,
    stream_status: null,
    tools_used: null,
    audio_status: null,
    audio_url: null,
    audio_duration_ms: null,
    audio_error: null,
    image_url: null,
    image_no: null,
    sources: null,
    created_at: new Date().toISOString(),
  }
}

export function useChat() {
  const [messages, setMessages] = useState<Record<string, Message[]>>({})
  const [hasMore, setHasMore] = useState<Record<string, boolean>>({})
  const [streams, setStreams] = useState<Record<string, StreamState>>({})
  const controllers = useRef(new Map<string, AbortController>())
  const outbox = useRef<Record<string, OutboxEntry>>({})
  const bubblePace = useRef<Record<string, BubblePace>>({})
  const sendNowRef = useRef<
    | ((
        friendId: string,
        text: string,
        options: SendOptions,
        skipPending?: boolean,
        pendingId?: string,
      ) => Promise<void>)
    | null
  >(null)

  const patchStream = useCallback((friendId: string, patch: Partial<StreamState>) => {
    setStreams((prev) => ({
      ...prev,
      [friendId]: { ...(prev[friendId] ?? IDLE_STREAM), ...patch },
    }))
  }, [])

  const resolveBubbleWaiters = useCallback((pace: BubblePace) => {
    const waiters = pace.waiters.splice(0)
    for (const finish of waiters) finish()
  }, [])

  const resetBubblePacing = useCallback(
    (friendId: string) => {
      const pace = bubblePace.current[friendId]
      if (pace?.timer != null) window.clearTimeout(pace.timer)
      if (pace) resolveBubbleWaiters(pace)
      bubblePace.current[friendId] = { visible: 0, rawCount: 0, timer: null, waiters: [] }
    },
    [resolveBubbleWaiters],
  )

  const observeBubbleSegments = useCallback(
    (friendId: string, rawCount: number) => {
      let pace = bubblePace.current[friendId]
      if (!pace) {
        pace = { visible: 0, rawCount: 0, timer: null, waiters: [] }
        bubblePace.current[friendId] = pace
      }
      pace.rawCount = Math.max(pace.rawCount, rawCount)

      const step = () => {
        const current = bubblePace.current[friendId]
        if (!current || current !== pace || current.timer !== null) return
        if (current.visible >= current.rawCount) {
          resolveBubbleWaiters(current)
          return
        }
        // v7.16（D352）：首泡也等 5~6 秒，让“她在想 / 在打”有真实停顿。
        const delay = bubbleGapMs()
        current.timer = window.setTimeout(() => {
          const live = bubblePace.current[friendId]
          if (!live || live !== current) return
          live.timer = null
          live.visible = Math.min(live.visible + 1, live.rawCount)
          patchStream(friendId, { visibleSegmentCount: live.visible })
          if (live.visible >= live.rawCount) resolveBubbleWaiters(live)
          else step()
        }, delay)
      }
      step()
    },
    [patchStream, resolveBubbleWaiters],
  )

  const waitForBubblePacing = useCallback((friendId: string) => {
    return new Promise<void>((resolve) => {
      const pace = bubblePace.current[friendId]
      if (!pace || pace.visible >= pace.rawCount) {
        resolve()
        return
      }
      let timer: number | null = null
      const finish = () => {
        if (timer !== null) window.clearTimeout(timer)
        resolve()
      }
      timer = window.setTimeout(finish, BUBBLE_PACING_TIMEOUT_MS)
      pace.waiters.push(finish)
    })
  }, [])

  const loadHistory = useCallback(async (friendId: string) => {
    const page = await api.listMessages(friendId, { limit: PAGE_SIZE })
    setMessages((prev) => ({ ...prev, [friendId]: page.messages }))
    setHasMore((prev) => ({ ...prev, [friendId]: page.has_more }))
  }, [])

  const loadOlder = useCallback(
    async (friendId: string) => {
      const existing = messages[friendId] ?? []
      if (existing.length === 0) return
      const page = await api.listMessages(friendId, {
        limit: PAGE_SIZE,
        before: existing[0].created_at,
      })
      setMessages((prev) => ({ ...prev, [friendId]: [...page.messages, ...(prev[friendId] ?? [])] }))
      setHasMore((prev) => ({ ...prev, [friendId]: page.has_more }))
    },
    [messages],
  )

  const sendNow = useCallback(
    async (
      friendId: string,
      text: string,
      options: SendOptions = {},
      skipPending = false,
      pendingId?: string,
    ) => {
      const trimmed = text.trim()
      if (!trimmed && !options.imageId && !options.screen) return
      if (controllers.current.has(friendId)) return // 一次只跑一条流

      const localPendingId = pendingId ?? newPendingId()
      const controller = new AbortController()
      controllers.current.set(friendId, controller)
      resetBubblePacing(friendId)
      patchStream(friendId, { ...IDLE_STREAM, status: 'sending', lastUserText: trimmed })
      if (!skipPending) {
        const pending = pendingMessage(localPendingId, trimmed)
        if (options.previewUrl) pending.image_url = options.previewUrl // 本地预览（还没上传完）
        setMessages((prev) => {
          const list = prev[friendId] ?? []
          return { ...prev, [friendId]: upsertPendingMessage(list, pending) }
        })
      }

      let finished = false
      let streamSegmentCount = 0
      try {
        for await (const event of api.streamMessage(
          friendId,
          trimmed,
          controller.signal,
          options.imageId,
          options.screen,
        )) {
          if (event.event === 'content') {
            streamSegmentCount = Math.max(streamSegmentCount, 1)
            setStreams((prev) => {
              const cur = prev[friendId] ?? IDLE_STREAM
              const segments = cur.segments.length
                ? [...cur.segments.slice(0, -1), cur.segments[cur.segments.length - 1] + event.delta]
                : [event.delta]
              return {
                ...prev,
                [friendId]: {
                  ...cur,
                  status: 'streaming',
                  content: cur.content + event.delta,
                  segments,
                },
              }
            })
            observeBubbleSegments(friendId, streamSegmentCount)
          } else if (event.event === 'segment_break') {
            streamSegmentCount += 1
            setStreams((prev) => {
              const cur = prev[friendId] ?? IDLE_STREAM
              return {
                ...prev,
                [friendId]: {
                  ...cur,
                  segments: [...cur.segments, ''],
                },
              }
            })
          } else if (event.event === 'start') {
            patchStream(friendId, { assistantId: event.assistantMessageId })
            setMessages((prev) => {
              const list = prev[friendId] ?? []
              const next = settlePendingMessage(list, localPendingId, event.userMessageId)
              return next === list ? prev : { ...prev, [friendId]: next }
            })
          } else if (event.event === 'mood') {
            setStreams((prev) => {
              const cur = prev[friendId] ?? IDLE_STREAM
              return {
                ...prev,
                [friendId]: {
                  ...cur,
                  moods: [...cur.moods, { mood: event.mood, at: event.at }],
                },
              }
            })
          } else if (event.event === 'reasoning') {
            setStreams((prev) => {
              const cur = prev[friendId] ?? IDLE_STREAM
              return {
                ...prev,
                [friendId]: { ...cur, status: 'thinking', reasoning: cur.reasoning + event.delta },
              }
            })
          } else if (event.event === 'tool_call') {
            setStreams((prev) => {
              const cur = prev[friendId] ?? IDLE_STREAM
              return {
                ...prev,
                [friendId]: {
                  ...cur,
                  status: 'tool_running',
                  tools: [...cur.tools, { id: event.id, name: event.name, args: event.args }],
                },
              }
            })
          } else if (event.event === 'error') {
            finished = true
            await loadHistory(friendId)
            patchStream(friendId, { status: 'error', error: event.message })
            break
          } else if (event.event === 'done') {
            // 后端最终切好的 segments 可能包含没有走 segment_break 的段落；
            // 先替换进实时流，再等它们按 5~6 秒节奏逐条展示，最后才刷新历史。
            const finalSegments = event.segments ?? []
            if (finalSegments.length > 0) {
              streamSegmentCount = Math.max(streamSegmentCount, finalSegments.length)
              const finalMoods = event.moods ?? []
              setStreams((prev) => {
                const cur = prev[friendId] ?? IDLE_STREAM
                return {
                  ...prev,
                  [friendId]: {
                    ...cur,
                    segments: finalSegments,
                    moods: finalMoods.length
                      ? finalMoods.map((mood) => ({ mood, at: 0 }))
                      : cur.moods,
                    visibleSegmentCount: Math.min(cur.visibleSegmentCount, finalSegments.length),
                  },
                }
              })
              observeBubbleSegments(friendId, streamSegmentCount)
            }
            finished = true
            await waitForBubblePacing(friendId)
            await loadHistory(friendId)
            patchStream(friendId, { ...IDLE_STREAM })
            break
          } else {
            finished = true
            await waitForBubblePacing(friendId)
            await loadHistory(friendId)
            patchStream(friendId, { ...IDLE_STREAM })
            break
          }
        }
        if (!finished) {
          // 流没给 done 就结束了 → 当成被打断（api.md §1.6）
          await loadHistory(friendId)
          patchStream(friendId, { status: 'partial' })
        }
      } catch (e) {
        const aborted = controller.signal.aborted
        await loadHistory(friendId).catch(() => undefined)
        patchStream(friendId, {
          status: aborted ? 'partial' : 'error',
          error: aborted ? null : e instanceof Error ? e.message : String(e),
        })
      } finally {
        controllers.current.delete(friendId)
        resetBubblePacing(friendId)
        const queued = outbox.current[friendId]
        if (queued?.ready) {
          delete outbox.current[friendId]
          const combined = mergeInputTexts(queued.texts)
          window.setTimeout(() => {
            void sendNowRef.current?.(friendId, combined, queued.options, true, queued.pendingId)
          }, 80)
        }
      }
    },
    [loadHistory, observeBubbleSegments, patchStream, resetBubblePacing, waitForBubblePacing],
  )
  sendNowRef.current = sendNow

  /**
   * v7.13（D344）：连续输入先在本机防抖 1.2 秒，合并成一轮再发。
   * 图片/看屏不走防抖（它们的附件路径要单独处理）。
   */
  const send = useCallback(
    (
      friendId: string,
      text: string,
      options: SendOptions = {},
    ) => {
      const trimmed = text.trim()
      if (!trimmed && !options.imageId && !options.screen) return
      if (options.imageId || options.screen) {
        void sendNow(friendId, trimmed, options, false)
        return
      }
      const entry =
        outbox.current[friendId] ??
        ({
          texts: [],
          options: {},
          timer: null,
          ready: false,
          pendingId: newPendingId(),
        } as OutboxEntry)
      entry.texts.push(trimmed)
      entry.options = { ...entry.options, ...options }
      entry.ready = false
      const pending = pendingMessage(entry.pendingId, mergeInputTexts(entry.texts))
      setMessages((prev) => {
        const list = prev[friendId] ?? []
        return { ...prev, [friendId]: upsertPendingMessage(list, pending) }
      })
      if (entry.timer !== null) window.clearTimeout(entry.timer)
      entry.timer = window.setTimeout(() => {
        entry.timer = null
        if (controllers.current.has(friendId)) {
          entry.ready = true
          return
        }
        const combined = mergeInputTexts(entry.texts)
        delete outbox.current[friendId]
        void sendNow(friendId, combined, entry.options, true, entry.pendingId)
      }, 1200)
      outbox.current[friendId] = entry
    },
    [sendNow],
  )

  // v3.14（D182）：停止 = ① 本地断开（立刻不看了）+ ② **告诉后端真的掐掉**。
  // 只做 ① 的话她会继续说 —— 断开现在只意味着"这一屏不看了"，不是"别说了"。
  const stop = useCallback(
    (friendId: string) => {
      const messageId = streams[friendId]?.assistantId
      controllers.current.get(friendId)?.abort()
      if (messageId) void api.stopMessage(friendId, messageId).catch(() => undefined)
    },
    [streams],
  )

  const isStreaming = useCallback(
    (friendId: string) => {
      const status = streams[friendId]?.status
      return status !== undefined && status !== 'idle' && status !== 'error' && status !== 'partial'
    },
    [streams],
  )

  /** 就地改一条消息（WS 推来"语音好了"时用——不刷新整页、不动滚动位置）。 */
  const patchMessage = useCallback(
    (friendId: string, messageId: string, patch: Partial<Message>) => {
      setMessages((prev) => {
        const list = prev[friendId]
        if (!list) return prev
        let hit = false
        const next = list.map((m) => {
          if (m.id !== messageId) return m
          hit = true
          return { ...m, ...patch }
        })
        return hit ? { ...prev, [friendId]: next } : prev
      })
    },
    [],
  )

  // 组件卸载时只断本地这条流（关窗 / 刷新）——
  // v3.14（D182）：**后端照旧把这一轮说完**（她要继续说的），下次进来历史里是全的。
  useEffect(() => {
    const running = controllers.current
    return () => {
      for (const controller of running.values()) controller.abort()
      running.clear()
    }
  }, [])

  return {
    messages,
    hasMore,
    streams,
    loadHistory,
    loadOlder,
    send,
    stop,
    isStreaming,
    patchStream,
    patchMessage,
  }
}
