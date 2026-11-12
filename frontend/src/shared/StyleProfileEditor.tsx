/** 说话方式档案（D347）：查看、手改、清空、回滚。 */

import { useEffect, useState } from 'react'

import {
  api,
  type StyleChangeInput,
  type StyleEvent,
  type StyleProfile,
  type StyleProfileResponse,
  type StyleSectionKey,
} from './api'

const SECTION_ORDER: StyleSectionKey[] = [
  'length',
  'tone',
  'diction',
  'structure',
  'situations',
  'keep',
]

const FALLBACK_LABELS: Record<StyleSectionKey, string> = {
  length: '长度',
  tone: '语气',
  diction: '用词和口癖',
  structure: '解释和结构',
  situations: '不同场景',
  keep: '做得好的地方',
}

type DraftLine = {
  id: string
  text: string
  hits: number
  isNew: boolean
}

type DraftSections = Record<StyleSectionKey, DraftLine[]>

function toDraft(profile: StyleProfile): DraftSections {
  const next = {} as DraftSections
  for (const key of SECTION_ORDER) {
    next[key] = (profile.sections[key] ?? []).map((line) => ({
      id: line.id,
      text: line.text,
      hits: line.hits,
      isNew: false,
    }))
  }
  return next
}

function newDraftId(): string {
  return `new-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
}

function eventLabel(event: StyleEvent): string {
  if (event.event_type === 'manual') return '手动编辑'
  if (event.event_type === 'reset') return '清空档案'
  if (event.event_type === 'rollback') return '回滚学习'
  return '她学到一条'
}

function eventSummary(event: StyleEvent): string {
  if (event.summary) return event.summary
  if (event.event_type === 'reset') return '清空了整份说话方式档案'
  if (event.event_type === 'rollback') return '撤销了一次学习'
  const parts = event.changes
    .map((item) => {
      const action = String(item.action ?? '')
      const section = String(item.section ?? '')
      const label = FALLBACK_LABELS[section as StyleSectionKey] ?? section
      const lesson = String(item.lesson ?? '').trim()
      return lesson ? `${label}：${lesson}` : `${label} ${action}`
    })
    .filter(Boolean)
  return parts.join('；') || '一次修改'
}

export default function StyleProfileEditor({
  friendId,
  mobile = false,
  onChanged,
}: {
  friendId: string
  mobile?: boolean
  onChanged?: () => void
}) {
  const [data, setData] = useState<StyleProfileResponse | null>(null)
  const [draft, setDraft] = useState<DraftSections>(() => toDraft({ version: 0, sections: {} as never }))
  const [events, setEvents] = useState<StyleEvent[]>([])
  const [busy, setBusy] = useState(false)
  const [hint, setHint] = useState<string | null>(null)
  const [showEvents, setShowEvents] = useState(false)
  const [confirmReset, setConfirmReset] = useState(false)

  async function load() {
    setBusy(true)
    setHint(null)
    try {
      const next = await api.getStyleProfile(friendId)
      setData(next)
      setDraft(toDraft(next.profile))
      setEvents((await api.listStyleEvents(friendId, 12)).events)
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    void load()
  }, [friendId])

  function addLine(section: StyleSectionKey) {
    setDraft((prev) => ({
      ...prev,
      [section]: [...(prev[section] ?? []), { id: newDraftId(), text: '', hits: 0, isNew: true }],
    }))
  }

  function editLine(section: StyleSectionKey, id: string, text: string) {
    setDraft((prev) => ({
      ...prev,
      [section]: (prev[section] ?? []).map((line) => (line.id === id ? { ...line, text } : line)),
    }))
  }

  function dropLine(section: StyleSectionKey, id: string) {
    setDraft((prev) => ({
      ...prev,
      [section]: (prev[section] ?? []).filter((line) => line.id !== id),
    }))
  }

  async function save() {
    if (busy) return
    const changes: StyleChangeInput[] = []
    const original = data?.profile
    for (const section of SECTION_ORDER) {
      const current = draft[section] ?? []
      const oldLines = original?.sections[section] ?? []
      const oldById = new Map(oldLines.map((line) => [line.id, line]))
      const currentIds = new Set(current.map((line) => line.id))
      for (const line of current) {
        const text = line.text.trim()
        const old = oldById.get(line.id)
        if (!text) {
          if (old) changes.push({ action: 'remove', section, target_id: line.id })
          continue
        }
        if (!old || line.isNew) {
          changes.push({ action: 'add', section, lesson: text })
        } else if (text !== old.text) {
          changes.push({ action: 'update', section, target_id: line.id, lesson: text })
        }
      }
      for (const old of oldLines) {
        if (!currentIds.has(old.id)) {
          changes.push({ action: 'remove', section, target_id: old.id })
        }
      }
    }
    if (changes.length === 0) {
      setHint('没有改动')
      return
    }
    setBusy(true)
    setHint(null)
    try {
      for (let index = 0; index < changes.length; index += 6) {
        await api.saveStyleChanges(
          friendId,
          changes.slice(index, index + 6),
          '手动编辑说话方式档案',
        )
      }
      await load()
      onChanged?.()
      setHint('说话方式档案已保存')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function rollback(event: StyleEvent) {
    if (busy) return
    setBusy(true)
    setHint(null)
    try {
      await api.rollbackStyle(friendId, event.id)
      await load()
      onChanged?.()
      setHint('已回到这次学习之前')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function reset() {
    if (busy) return
    setBusy(true)
    setHint(null)
    try {
      await api.resetStyle(friendId)
      setConfirmReset(false)
      await load()
      onChanged?.()
      setHint('档案清空了；声音样本和人格没有动')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const labels: Record<StyleSectionKey, string> = data?.labels ?? FALLBACK_LABELS
  const limits: Partial<Record<StyleSectionKey, number>> = data?.limits ?? {}
  const inputClass = mobile
    ? 'min-w-0 flex-1 rounded-xl border border-line bg-surface px-3 py-2.5 text-[14px] text-ink outline-none focus:border-accent/60'
    : 'min-w-0 flex-1 rounded-lg border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] leading-relaxed text-ink outline-none focus:border-accent/60'

  return (
    <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className={mobile ? 'text-[13px] font-medium text-ink' : 'text-[11.5px] text-ink'}>
          说话方式档案
        </span>
        <span className={mobile ? 'text-[11px] text-faint' : 'text-[10.5px] text-faint'}>
          她从你的反馈里学到的当前规则 · 版本 {data?.version ?? 0}
        </span>
      </div>
      <p className={mobile ? 'mt-1 text-[11px] leading-snug text-faint' : 'mt-1 text-[10.5px] leading-relaxed text-faint'}>
        这不是人格，也不是普通记忆。它只影响说话方式；原始反馈留在学习记录里，不会每轮重复塞给她。
      </p>
      {hint && <p className="mt-2 text-[11px] text-muted">{hint}</p>}

      <div className="mt-3 space-y-2.5">
        {SECTION_ORDER.map((section) => {
          const lines = draft[section] ?? []
          const limit = limits[section]
          return (
            <div key={section} className="rounded-xl border border-line/60 bg-page/30 p-2.5">
              <div className="flex items-center gap-2">
                <span className="text-[11.5px] font-medium text-ink">{labels[section] ?? section}</span>
                <span className="text-[10px] text-faint">
                  {lines.length}
                  {limit ? ` / ${limit}` : ''}
                </span>
                {lines.length < (limit ?? 99) && (
                  <button
                    type="button"
                    onClick={() => addLine(section)}
                    disabled={busy}
                    className="ml-auto rounded-full border border-line-strong px-2 py-0.5 text-[10.5px] text-muted hover:border-accent/60 hover:text-accent disabled:opacity-40"
                  >
                    + 加一条
                  </button>
                )}
              </div>
              <div className="mt-1.5 space-y-1.5">
                {lines.length === 0 && <p className="text-[10.5px] text-faint">（还没有）</p>}
                {lines.map((line) => (
                  <div key={line.id} className="flex items-center gap-1.5">
                    <input
                      value={line.text}
                      onChange={(event) => editLine(section, line.id, event.target.value)}
                      disabled={busy}
                      className={inputClass}
                      placeholder="一条可执行的说话规则"
                    />
                    {!line.isNew && line.hits > 1 && (
                      <span className="shrink-0 text-[10px] text-faint">×{line.hits}</span>
                    )}
                    <button
                      type="button"
                      onClick={() => dropLine(section, line.id)}
                      disabled={busy}
                      aria-label="删掉这条"
                      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[14px] text-faint hover:bg-chip hover:text-red-500 disabled:opacity-40"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="rounded-full bg-accent px-3.5 py-1.5 text-[11.5px] text-white disabled:opacity-40"
        >
          {busy ? '处理中…' : '保存修改'}
        </button>
        <button
          type="button"
          onClick={() => setShowEvents((prev) => !prev)}
          disabled={busy}
          className="rounded-full border border-line-strong px-3 py-1.5 text-[11px] text-muted hover:border-accent/60 hover:text-accent disabled:opacity-40"
        >
          学习记录（{events.length}）
        </button>
        {confirmReset ? (
          <span className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void reset()}
              disabled={busy}
              className="rounded-full border border-red-300 px-3 py-1.5 text-[11px] text-red-500 disabled:opacity-40"
            >
              确定清空
            </button>
            <button
              type="button"
              onClick={() => setConfirmReset(false)}
              disabled={busy}
              className="rounded-full px-2 py-1.5 text-[11px] text-faint"
            >
              取消
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmReset(true)}
            disabled={busy}
            className="ml-auto rounded-full px-2 py-1.5 text-[11px] text-faint hover:text-red-500 disabled:opacity-40"
          >
            清空档案
          </button>
        )}
      </div>

      {showEvents && (
        <div className="mt-3 space-y-1.5 border-t border-line/50 pt-2.5">
          {events.length === 0 && <p className="text-[10.5px] text-faint">还没有学习记录。</p>}
          {events.map((event) => (
            <div key={event.id} className="rounded-lg border border-line/50 bg-page/30 px-2.5 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[10.5px] font-medium text-muted">{eventLabel(event)}</span>
                <span className="text-[10px] text-faint">
                  v{event.before_version} → v{event.after_version}
                </span>
                <span className="ml-auto text-[10px] text-faint">
                  {new Date(event.created_at).toLocaleString('zh-CN', {
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </span>
              </div>
              <p className="mt-0.5 text-[11px] leading-snug text-muted">{eventSummary(event)}</p>
              {event.user_text && (
                <p className="mt-0.5 truncate text-[10.5px] text-faint">你当时说：{event.user_text}</p>
              )}
              <button
                type="button"
                onClick={() => void rollback(event)}
                disabled={busy}
                className="mt-1 text-[10.5px] text-accent hover:underline disabled:opacity-40"
              >
                回到这次之前
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
