/** v7.12（D341）：显示电脑端生活线的当前活动。
 *
 * 只是事实展示；手机端也会显示电脑端的活动（通过服务器/局域网同步）。
 */

import { useEffect, useState } from 'react'

import { api, type LifeStatus } from './api'

export default function LifeBadge({ friendId }: { friendId: string }) {
  const [status, setStatus] = useState<LifeStatus | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const next = await api.lifeStatus(friendId)
        if (alive) setStatus(next)
      } catch {
        // 生活线读不到不吵；聊天主链路不受影响。
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 15000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [friendId])

  if (!status?.enabled) return null
  const activity = status.current_activity
  const text = activity?.title || (status.last_observation ? '刚忙完' : '生活线')
  return (
    <span
      title={status.last_observation || text}
      className="inline-flex max-w-[12rem] items-center gap-1 truncate rounded-full border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10.5px] text-accent"
    >
      <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-accent" />
      <span className="truncate">{text}</span>
    </span>
  )
}
