/**
 * 设置 → 功能（v7.20 / D373）。
 *
 * 一张清单，按分类列出所有静态功能模块：
 * · 核心功能锁死（聊天不能关）；
 * · 其它功能点一下就开/关，后台会同步撤下/挂上它自己的工具、提示词和定时任务。
 *
 * 这里**不做二次确认弹窗**（D2：不用弹窗做体验决策）；开关本身就是可逆的。
 */

import { useEffect, useState } from 'react'

import { api, type FeatureInfo } from '../shared/api'
import { FEATURE_CATEGORY_HINTS, FEATURE_CATEGORY_ORDER } from '../shared/features'

export default function FeatureCenter({ onChanged }: { onChanged: () => void }) {
  const [items, setItems] = useState<FeatureInfo[] | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const result = await api.getFeatures()
        if (alive) setItems(result.features)
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      alive = false
    }
  }, [])

  async function toggle(item: FeatureInfo) {
    if (item.core || busyId) return
    setBusyId(item.id)
    setError(null)
    try {
      const result = await api.setFeature(item.id, !item.enabled)
      setItems((prev) =>
        prev ? prev.map((row) => (row.id === item.id ? result.feature : row)) : prev,
      )
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyId(null)
    }
  }

  if (error && items === null) {
    return <p className="text-[12px] text-rose-500">{error}</p>
  }
  if (items === null) {
    return <p className="text-[12px] text-faint">正在读取功能清单…</p>
  }

  const categories = [
    ...FEATURE_CATEGORY_ORDER.filter((category) => items.some((item) => item.category === category)),
    ...Array.from(new Set(items.map((item) => item.category))).filter(
      (category) => !FEATURE_CATEGORY_ORDER.includes(category as never),
    ),
  ]

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-[12px] font-medium text-ink">功能</h3>
        <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
          关掉一个功能 = 它不再往聊天里塞工具和提示词，也不再跑自己的后台任务。
          数据不会删；下次打开还在。
        </p>
      </div>

      {error && <p className="text-[11.5px] text-rose-500">{error}</p>}

      {categories.map((category) => (
        <section key={category}>
          <div className="flex items-baseline gap-2">
            <h4 className="text-[11.5px] font-medium text-ink">{category}</h4>
            {FEATURE_CATEGORY_HINTS[category] && (
              <span className="text-[10.5px] text-faint">{FEATURE_CATEGORY_HINTS[category]}</span>
            )}
          </div>
          <div className="mt-1.5 space-y-1.5">
            {items
              .filter((item) => item.category === category)
              .map((item) => (
                <div
                  key={item.id}
                  className="flex items-start gap-3 rounded-xl border border-line/70 bg-surface/60 px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[12px] font-medium text-ink">{item.name}</span>
                      {item.core && (
                        <span className="rounded-full border border-line px-1.5 py-px text-[9.5px] text-faint">
                          核心
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
                      {item.description}
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={item.enabled}
                    aria-label={`${item.name}开关`}
                    disabled={item.core || busyId === item.id}
                    onClick={() => void toggle(item)}
                    className={`mt-0.5 flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors ${
                      item.enabled
                        ? 'border-accent/70 bg-accent/25'
                        : 'border-line-strong bg-chip'
                    } ${item.core ? 'cursor-not-allowed opacity-60' : ''}`}
                  >
                    <span
                      className={`h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform ${
                        item.enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'
                      }`}
                    />
                  </button>
                </div>
              ))}
          </div>
        </section>
      ))}
    </div>
  )
}
