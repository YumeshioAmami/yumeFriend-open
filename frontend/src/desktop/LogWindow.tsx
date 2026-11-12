/** 日志窗口（frontend.md §5.2 / D31）——目标是"不开 DevTools 也能自己排查问题"。

 * 每 2 秒拉增量（日志不是高频，不上 SSE）
 * 级别筛选 + 关键词搜索 + 清空 + 导出/复制 + 暂停滚动 + 一键诊断
 *
 * 隐私：**工具调用的参数会记**（调试必需，D31 的刻意例外）；
 * 聊天正文不记；API key 后端已经打码。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { api, type LogEntry } from '../shared/api'
import { ACCENT } from '../shared/ui'

const LEVELS = ['全部', 'DEBUG', 'INFO', 'WARNING', 'ERROR'] as const
const LEVEL_COLOR: Record<string, string> = {
  DEBUG: 'text-stone-500',
  INFO: 'text-stone-400',
  WARNING: 'text-amber-400',
  ERROR: 'text-red-400',
  CRITICAL: 'text-red-400 font-medium',
}

export default function LogWindow({
  onClose,
  embedded = false,
}: {
  onClose?: () => void
  /** v5.1（D287）：嵌进全屏游戏模式时不再自己盖一层模态。 */
  embedded?: boolean
}) {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [level, setLevel] = useState<(typeof LEVELS)[number]>('全部')
  const [search, setSearch] = useState('')
  const [paused, setPaused] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const sinceRef = useRef(0)
  const pausedRef = useRef(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    pausedRef.current = paused
  }, [paused])

  const poll = useCallback(async () => {
    if (pausedRef.current) return
    try {
      const page = await api.readLogs(sinceRef.current)
      if (page.items.length > 0) {
        sinceRef.current = page.next_seq
        // 轮询可能重叠（例如页签刚打开时上一次请求还没回来）：
        // 同一条日志不能 append 两次，否则 React 会报 duplicate key。
        setEntries((prev) => {
          const seen = new Set(prev.map((entry) => entry.seq))
          const fresh = page.items.filter((entry) => !seen.has(entry.seq))
          return [...prev, ...fresh].slice(-2000)
        })
      } else if (page.next_seq > sinceRef.current) {
        sinceRef.current = page.next_seq
      }
    } catch {
      // 拉日志失败就算了，别让它把界面搞崩
    }
  }, [])

  useEffect(() => {
    void poll()
    const timer = window.setInterval(() => void poll(), 2000)
    return () => window.clearInterval(timer)
  }, [poll])

  const shown = useMemo(() => {
    const keyword = search.trim()
    return entries.filter(
      (e) =>
        (level === '全部' || e.level === level) &&
        (!keyword || e.message.includes(keyword) || e.where.includes(keyword)),
    )
  }, [entries, level, search])

  useEffect(() => {
    if (!paused) bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [shown.length, paused])

  function asText(): string {
    return shown
      .map((e) => `${e.time} [${e.level}] ${e.where} ${e.message}`)
      .join('\n')
  }

  async function exportLogs() {
    const text = asText()
    try {
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `yumefriend-log-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.txt`
      link.click()
      URL.revokeObjectURL(url)
      setNote('已导出到下载目录')
    } catch {
      await navigator.clipboard.writeText(text).catch(() => undefined)
      setNote('导出失败，已复制到剪贴板')
    }
  }

  async function copyDiagnostics() {
    try {
      const { text } = await api.diagnostics()
      await navigator.clipboard.writeText(text)
      setNote('诊断信息已复制 —— 直接贴给我就能定位（省得你再描述一遍）')
    } catch {
      setNote('复制失败（浏览器可能不给剪贴板权限）')
    }
  }

  return (
    <div
      className={
        embedded
          ? 'h-full w-full'
          : 'fixed inset-0 z-40 flex items-center justify-center bg-scrim/40 backdrop-blur-sm'
      }
    >
      <div
        className={
          embedded
            ? 'flex h-full w-full flex-col bg-[#1c1917] p-4'
            : 'flex h-[82%] w-[900px] max-w-[94%] flex-col rounded-2xl border border-line bg-[#1c1917] p-4 shadow-2xl'
        }
      >
        <div className="mb-2 flex items-center gap-2">
          <h2 className="text-[14px] font-medium text-stone-200">日志</h2>
          <span className="text-[11px] text-stone-400">
            {shown.length} / {entries.length} 条{paused && ' · 已暂停（画面冻结）'}
          </span>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="ml-auto rounded-lg px-2 py-0.5 text-stone-400 hover:bg-stone-700/50 hover:text-stone-300"
            >
              ✕
            </button>
          )}
        </div>

        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          {LEVELS.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setLevel(item)}
              className={`rounded-full px-2.5 py-0.5 text-[11px] transition-colors ${
                level === item ? 'text-white' : 'text-stone-500 hover:text-stone-200'
              }`}
              style={level === item ? { background: ACCENT } : { background: 'rgba(255,255,255,0.06)' }}
            >
              {item}
            </button>
          ))}
          <input
            className="ml-1 w-40 rounded-lg border border-stone-700 bg-stone-800/80 px-2 py-1 text-[11.5px] text-stone-200 outline-none focus:border-accent"
            placeholder="搜关键词…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button
            type="button"
            onClick={() => setPaused((v) => !v)}
            className="rounded-lg bg-surface/5 px-2 py-1 text-[11px] text-stone-300 hover:bg-surface/10"
          >
            {paused ? '▶ 继续滚动' : '⏸ 暂停滚动'}
          </button>
          <button
            type="button"
            onClick={() => void exportLogs()}
            className="rounded-lg bg-surface/5 px-2 py-1 text-[11px] text-stone-300 hover:bg-surface/10"
          >
            ⬇ 导出
          </button>
          <button
            type="button"
            onClick={() => void copyDiagnostics()}
            className="rounded-lg bg-surface/5 px-2 py-1 text-[11px] text-stone-300 hover:bg-surface/10"
          >
            📋 复制诊断信息
          </button>
          <button
            type="button"
            onClick={() => {
              void api.clearLogs().then(() => {
                sinceRef.current = 0
                setEntries([])
                setNote('已清空内存缓冲（日志文件没动）')
              })
            }}
            className="rounded-lg bg-surface/5 px-2 py-1 text-[11px] text-red-300 hover:bg-red-500/20"
          >
            清空
          </button>
        </div>
        {note && <p className="mb-1 text-[11px] text-accent">{note}</p>}

        <div className="flex-1 overflow-y-auto rounded-lg bg-black/30 p-2 font-mono text-[11px] leading-relaxed">
          {shown.length === 0 && (
            <p className="py-8 text-center text-stone-400">这里还没有日志</p>
          )}
          {shown.map((entry) => (
            <div key={entry.seq} className="flex gap-2 py-0.5">
              <span className="shrink-0 text-stone-400">{entry.time.slice(11)}</span>
              <span className={`w-14 shrink-0 ${LEVEL_COLOR[entry.level] ?? 'text-stone-500'}`}>
                {entry.level}
              </span>
              <span className="shrink-0 text-stone-400">{entry.where.split(':').pop()}</span>
              <span className="whitespace-pre-wrap break-all text-stone-300">{entry.message}</span>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  )
}
