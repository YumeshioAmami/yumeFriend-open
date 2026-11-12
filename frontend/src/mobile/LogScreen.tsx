/**
 * 手机壳：**日志**（v3.0 / B7）。用户点名要它 —— "方便调试"。
 *
 * 手机上够用的那一套：级别过滤 + 关键词搜索 + 自动跟到底 + 清空 + 一键复制诊断。
 * 数据来源跟电脑上的日志窗口是同一个接口（`/api/logs` 增量读），**语义不变**。
 *
 * ⚠️ 日志里没有聊天明文、API key 也自动打码（LLM协议§9）—— 所以复制出去发给别人是安全的。
 */

import { useEffect, useRef, useState } from 'react'

import { api, type LogEntry } from '../shared/api'

const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR'] as const
type LevelFilter = (typeof LEVELS)[number]

const LEVEL_COLOR: Record<LogEntry['level'], string> = {
  DEBUG: 'text-faint',
  INFO: 'text-ink-soft',
  WARNING: 'text-amber-600',
  ERROR: 'text-accent',
  CRITICAL: 'text-accent',
}

const KEEP = 500

export default function LogScreen({ onBack }: { onBack: () => void }) {
  const [items, setItems] = useState<LogEntry[]>([])
  const [level, setLevel] = useState<LevelFilter>('ALL')
  const [query, setQuery] = useState('')
  const [follow, setFollow] = useState(true)
  const [hint, setHint] = useState<string | null>(null)
  const cursor = useRef(0)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let alive = true
    async function tick() {
      try {
        const page = await api.readLogs(cursor.current, KEEP)
        if (!alive) return
        cursor.current = page.next_seq
        if (page.items.length) {
          setItems((prev) => [...prev, ...page.items].slice(-KEEP))
        }
      } catch {
        // 日志读不到就先安静（别在日志页自己刷屏）
      }
    }
    void tick()
    const timer = window.setInterval(() => void tick(), 2500)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  const shown = items.filter((item) => {
    if (level !== 'ALL' && item.level !== level) return false
    if (!query.trim()) return true
    const needle = query.trim().toLowerCase()
    return (
      item.message.toLowerCase().includes(needle) ||
      item.where.toLowerCase().includes(needle)
    )
  })

  useEffect(() => {
    if (follow) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [shown.length, follow])

  async function copyDiagnostics() {
    setHint('正在生成诊断信息…')
    try {
      const { text } = await api.diagnostics()
      await navigator.clipboard.writeText(text)
      setHint('诊断信息已复制（不含聊天明文，可以直接发给我）')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }

  async function clearAll() {
    try {
      await api.clearLogs()
      setItems([])
      cursor.current = 0
      setHint('清空了')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }

  return (
    <div className="flex h-dvh flex-col bg-page">
      <header className="flex shrink-0 items-center gap-2 border-b border-line/70 bg-page/95 px-2 pb-2 pt-[max(8px,env(safe-area-inset-top))]">
        <button
          type="button"
          onClick={onBack}
          aria-label="返回"
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-[22px] text-muted active:bg-surface-hover"
        >
          ‹
        </button>
        <div className="min-w-0 flex-1">
          <p className="text-[16px] font-medium text-ink">日志</p>
          <p className="text-[11.5px] text-muted">没有聊天明文，key 会自动打码</p>
        </div>
        <button
          type="button"
          onClick={() => void copyDiagnostics()}
          className="h-11 shrink-0 rounded-2xl border border-line bg-surface px-3 text-[12.5px] text-ink active:bg-surface-hover"
        >
          复制诊断
        </button>
      </header>

      <div className="flex shrink-0 items-center gap-2 overflow-x-auto px-3 py-2">
        {LEVELS.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => setLevel(item)}
            className={`h-9 shrink-0 rounded-full border px-3 text-[12px] ${
              level === item ? 'border-accent text-accent' : 'border-line text-muted'
            }`}
          >
            {item}
          </button>
        ))}
        <button
          type="button"
          onClick={() => setFollow((prev) => !prev)}
          className={`h-9 shrink-0 rounded-full border px-3 text-[12px] ${
            follow ? 'border-accent text-accent' : 'border-line text-muted'
          }`}
        >
          {follow ? '跟随中' : '已暂停'}
        </button>
        <button
          type="button"
          onClick={() => void clearAll()}
          className="h-9 shrink-0 rounded-full border border-line px-3 text-[12px] text-muted"
        >
          清空
        </button>
      </div>

      <input
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="搜关键词（比如朋友名字、工具名、报错）"
        className="mx-3 mb-2 h-11 shrink-0 rounded-xl border border-line bg-surface px-3 text-[13.5px] text-ink outline-none focus:border-accent/60"
      />

      {hint && <p className="px-3 pb-1 text-[11.5px] text-muted">{hint}</p>}

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-[max(12px,env(safe-area-inset-bottom))]">
        {shown.map((item) => (
          <div key={item.seq} className="border-b border-line/40 py-1.5">
            <p className="flex items-baseline gap-2 text-[10.5px] text-faint">
              <span>{item.time.slice(11, 19)}</span>
              <span className={`font-medium ${LEVEL_COLOR[item.level]}`}>{item.level}</span>
              <span className="truncate">{item.where}</span>
            </p>
            <p className="mt-0.5 whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-ink-soft">
              {item.message}
            </p>
          </div>
        ))}
        {!shown.length && (
          <p className="py-10 text-center text-[12.5px] text-muted">
            {items.length ? '没有符合条件的日志' : '还没有日志（等一会儿，或者去聊两句）'}
          </p>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
