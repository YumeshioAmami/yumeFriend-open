/**
 * 「连接断了」横幅（2026-09-27 / D196）。
 *
 * 手机在外面走流量时，组网/信号一抖，页面就可能"请求发出去没人回"。
 * 请求层现在会自己超时 + 重试；这个横幅负责**让人看得见**，并给一个立刻恢复的抓手
 * （"重新加载" = 最土但最可靠的自愈）。
 */

import { useEffect, useState } from 'react'

import { isOffline, markOnline, subscribeNetStatus } from './netStatus'

/** 手机壳的原生桥（只有 App 里有；浏览器里没有）。 */
type Shell = { openSetup?: () => void }

export default function NetworkBanner() {
  const [down, setDown] = useState(isOffline())
  const shell = (window as unknown as { yfShell?: Shell }).yfShell
  const canOpenSetup = typeof shell?.openSetup === 'function'

  useEffect(() => subscribeNetStatus(setDown), [])

  // 回到前台 / 系统说网络回来了 → 探一下健康检查；通了就把横幅收掉
  useEffect(() => {
    const probe = () => {
      if (!isOffline()) return
      void fetch('/api/health', { cache: 'no-store' })
        .then((response) => {
          if (response.ok) markOnline()
        })
        .catch(() => undefined)
    }
    const wake = () => {
      if (document.visibilityState === 'visible') probe()
    }
    window.addEventListener('online', wake)
    document.addEventListener('visibilitychange', wake)
    return () => {
      window.removeEventListener('online', wake)
      document.removeEventListener('visibilitychange', wake)
    }
  }, [])

  if (!down) return null
  return (
    <div className="fixed inset-x-0 top-0 z-50 flex items-center justify-center gap-2 bg-amber-500/95 px-3 py-1.5 text-[12px] text-white shadow">
      <span>连接断了，正在自动重试…</span>
      {canOpenSetup && (
        <button
          type="button"
          onClick={() => shell?.openSetup?.()}
          className="rounded-md bg-white/25 px-2 py-0.5 font-medium"
        >
          换地址
        </button>
      )}
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="rounded-md bg-white/25 px-2 py-0.5 font-medium"
      >
        重新加载
      </button>
    </div>
  )
}
