/**
 * 朋友列表的数据源（手机壳，v3.0 / B3）。
 *
 * 后端接口本来就够用（`_stats()` 已经给了 `last_message_preview` / `last_message_at` /
 * `unread_count` / `has_unfinished` / `avatar_url`），所以这里只做三件事：
 * 拉一次、能再拉一次、以及**收到 WS 的 `new_message` 时点一下重拉**。
 *
 * 为什么"重拉"而不是"本地拼"：预览文本、未读数、排序（按最后消息时间）全都是后端算的，
 * 本地拼容易和电脑端出现两套口径 —— 手机上重拉一次只是一次很小的请求。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { api, type Friend } from '../shared/api'
import { useProactiveFeed } from '../shared/useWs'

export function useFriends() {
  const [friends, setFriends] = useState<Friend[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      const rows = await api.listFriends()
      setFriends(rows)
      setError(null)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }, [])

  // WS：她主动说话 / 电脑那边回了消息 → 轻轻点一下重拉（合并 300ms 内的连发）
  useProactiveFeed(() => {
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => void refresh(), 300)
  })

  useEffect(() => {
    void refresh()
    return () => {
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [refresh])

  return { friends, loading, error, refresh }
}

/** 列表右侧那个时间：今天给 `HH:MM`、昨天给「昨天」、更早给 `MM-DD`。 */
export function listTime(iso: string | null): string {
  if (!iso) return ''
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  const now = new Date()
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  if (sameDay(at, now)) {
    return `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
  }
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(at, yesterday)) return '昨天'
  return `${String(at.getMonth() + 1).padStart(2, '0')}-${String(at.getDate()).padStart(2, '0')}`
}
