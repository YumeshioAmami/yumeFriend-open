/** 记忆面板（D26 / D61）：不是小弹窗，是能扛几百上千条的浏览界面。
 *
 * 为什么重做（用户提的）：几百条记忆塞进一个小弹窗根本没法看。
 * 所以四件事：**搜、筛、排、翻** ——
 *   · 搜：按内容模糊（后端 LIKE）
 *   · 筛：按 layer 分 tab，带各层条数
 *   · 排：最近提起 / 重要度 / 写入时间
 *   · 翻：每次 50 条，「加载更多」
 * 能改内容 / 重要度 / **layer（升·降级）**、删（软删）；`tag` 不能改（D230）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { api, type Memory, type MemoryEvent } from '../shared/api'
import { ACCENT } from '../shared/ui'

const PAGE_SIZE = 50

const LAYER_ORDER: Memory['layer'][] = ['pinned', 'permanent', 'long_term', 'short_term']
const LAYER_LABEL: Record<Memory['layer'], string> = {
  pinned: '钉住',
  permanent: '永久',
  long_term: '长期',
  short_term: '短期',
}
const LAYER_HINT: Record<Memory['layer'], string> = {
  pinned: '你明确要求记住的',
  permanent: '过一年也成立的事实（名字/生日/家人/长期习惯）',
  long_term: '这阵子的状态（在学什么、在做什么、近期的计划）',
  short_term: '只在这几天有意义，7 天后自动删',
}
const TAG_LABEL: Record<Memory['tag'], string> = {
  normal: '',
  rp: 'RP',
  rp_play_style: '演法',
  notebook: '我告诉它的',
}
const SORTS: { key: 'recent' | 'importance' | 'created'; label: string }[] = [
  { key: 'recent', label: '最近提起' },
  { key: 'importance', label: '重要度' },
  { key: 'created', label: '写入时间' },
]

function day(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

function whenOf(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// D230：改动留痕怎么读（谁动的 + 动了什么）
const ACTOR_LABEL: Record<string, string> = { agent: '她', system: '系统', user: '你' }

function describeEvent(event: MemoryEvent): string {
  const before = event.before ?? {}
  const after = event.after ?? {}
  if (event.action === 'create') return '记下了一条新记忆'
  if (event.action === 'delete') return '删掉了一条记忆'
  const parts: string[] = []
  const fromLayer = before.layer
  const toLayer = after.layer
  if (typeof fromLayer === 'string' && typeof toLayer === 'string' && fromLayer !== toLayer) {
    const name = (key: string) => LAYER_LABEL[key as Memory['layer']] ?? key
    parts.push(`层：${name(fromLayer)} → ${name(toLayer)}`)
  }
  if (typeof after.content === 'string' && before.content !== after.content) {
    parts.push(after.reason === 'dedup_merge' ? '内容：去重合并（换成了新说法）' : '改了内容')
  }
  if (before.importance !== undefined && after.importance !== undefined && before.importance !== after.importance) {
    parts.push(`重要度 ${before.importance} → ${after.importance}`)
  }
  return parts.length ? parts.join(' · ') : '改了这条记忆'
}

function eventText(event: MemoryEvent): string {
  const text = (event.after ?? {}).content ?? (event.before ?? {}).content
  return typeof text === 'string' ? text : ''
}

function Row({ memory, onChanged }: { memory: Memory; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const [content, setContent] = useState(memory.content)
  const [importance, setImportance] = useState(memory.importance)
  const [busy, setBusy] = useState(false)

  async function save() {
    setBusy(true)
    try {
      await api.updateMemory(memory.id, { content, importance })
      setEditing(false)
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    if (!window.confirm('删掉这条记忆？它会真的忘记这件事。')) return
    setBusy(true)
    try {
      await api.deleteMemory(memory.id)
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  async function changeLayer(next: Memory['layer']) {
    if (next === memory.layer) return
    setBusy(true)
    try {
      await api.updateMemory(memory.id, { layer: next })
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  if (editing) {
    return (
      <div className="rounded-lg border border-accent/40 bg-surface/80 px-3 py-2">
        <textarea
          className="h-16 w-full resize-none rounded-lg border border-line px-2 py-1 text-[12.5px] leading-relaxed outline-none focus:border-accent/60"
          value={content}
          onChange={(e) => setContent(e.target.value)}
        />
        <div className="mt-1 flex items-center gap-2 text-[11px] text-muted">
          重要度
          <input
            type="range"
            min={0}
            max={1}
            step={0.1}
            value={importance}
            onChange={(e) => setImportance(Number(e.target.value))}
          />
          <span className="w-6 tabular-nums">{importance.toFixed(1)}</span>
          <button
            type="button"
            onClick={() => void save()}
            disabled={busy || !content.trim()}
            className="ml-auto rounded-full px-2.5 py-0.5 text-white disabled:opacity-40"
            style={{ background: ACCENT }}
          >
            保存
          </button>
          <button
            type="button"
            onClick={() => {
              setContent(memory.content)
              setImportance(memory.importance)
              setEditing(false)
            }}
            className="rounded-full border border-line-strong px-2.5 py-0.5"
          >
            取消
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="group flex items-start gap-2 rounded-lg border border-line/60 bg-surface/60 px-3 py-1.5 hover:border-line-strong hover:bg-surface/90">
      <p className="flex-1 whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-ink">
        {memory.content}
      </p>
      <div className="flex shrink-0 items-center gap-1.5 pt-0.5">
        <select
          value={memory.layer}
          disabled={busy}
          onChange={(e) => void changeLayer(e.target.value as Memory['layer'])}
          title={`改层（升 / 降级）：${LAYER_HINT[memory.layer]}`}
          className="rounded border border-line-strong bg-surface/90 px-1 py-0 text-[10px] text-muted outline-none disabled:opacity-40"
        >
          {LAYER_ORDER.map((key) => (
            <option key={key} value={key}>
              {LAYER_LABEL[key]}
            </option>
          ))}
        </select>
        {TAG_LABEL[memory.tag] && (
          <span className="rounded bg-chip px-1 text-[10px] text-muted">
            {TAG_LABEL[memory.tag]}
          </span>
        )}
        <span className="text-[10px] text-faint" title={`写入 ${day(memory.created_at)}`}>
          近 {day(memory.refreshed_at)}
        </span>
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="rounded px-1.5 text-[10.5px] text-faint opacity-0 transition-opacity hover:text-stone-700 group-hover:opacity-100"
        >
          改
        </button>
        <button
          type="button"
          onClick={() => void remove()}
          disabled={busy}
          className="rounded px-1.5 text-[10.5px] text-faint opacity-0 transition-opacity hover:text-red-600 group-hover:opacity-100"
        >
          删
        </button>
      </div>
    </div>
  )
}

export default function MemoryModal({
  friendId,
  friendName,
  onClose,
}: {
  friendId: string
  friendName: string
  onClose: () => void
}) {
  const [items, setItems] = useState<Memory[]>([])
  const [total, setTotal] = useState(0)
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [layer, setLayer] = useState<string>('')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<'recent' | 'importance' | 'created'>('recent')
  const [loading, setLoading] = useState(true)
  const [moreBusy, setMoreBusy] = useState(false)
  const [draft, setDraft] = useState('')
  const [draftLayer, setDraftLayer] = useState<string>('pinned')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const searchTimer = useRef<number | null>(null)
  // 2026-09-27（D192）：小纸条跟记忆放**同一个面板**（用户："可以跟记忆或者待办在一个模块儿里，
  // 然后我可以随时看"）—— 上面切一下就能看见她攒着想说的那件事。
  const [view, setView] = useState<'memories' | 'events'>('memories')
  const [events, setEvents] = useState<MemoryEvent[]>([])
  const [eventsBusy, setEventsBusy] = useState(false)

  // D230：改动留痕 —— 她 / 系统 / 你动过哪些记忆
  const loadEvents = useCallback(async () => {
    setEventsBusy(true)
    try {
      const page = await api.listMemoryEvents(friendId)
      setEvents(page.events)
    } finally {
      setEventsBusy(false)
    }
  }, [friendId])

  useEffect(() => {
    void loadEvents()
  }, [loadEvents])

  const load = useCallback(
    async (offset: number, append: boolean) => {
      const page = await api.listMemories(friendId, {
        layer: layer || undefined,
        search: search || undefined,
        sort,
        limit: PAGE_SIZE,
        offset,
      })
      setItems((prev) => (append ? [...prev, ...page.memories] : page.memories))
      setTotal(page.total)
      setCounts(page.layer_counts ?? {})
      setLoading(false)
    },
    [friendId, layer, search, sort],
  )

  useEffect(() => {
    if (searchTimer.current) window.clearTimeout(searchTimer.current)
    searchTimer.current = window.setTimeout(() => {
      setLoading(true)
      void load(0, false)
    }, 220)
    return () => {
      if (searchTimer.current) window.clearTimeout(searchTimer.current)
    }
  }, [load])

  async function add() {
    const text = draft.trim()
    if (!text || busy) return
    setBusy(true)
    try {
      const result = await api.createMemory({
        friend_id: friendId,
        content: text,
        layer: draftLayer,
      })
      setDraft('')
      setNote(result.merged ? '这件事它已经记过了，我给它合并了' : '记下了')
      await load(0, false)
    } finally {
      setBusy(false)
    }
  }

  const hasMore = items.length < total

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim/40 backdrop-blur-sm">
      <div className="flex h-[84%] w-[920px] max-w-[95%] flex-col rounded-2xl border border-line bg-page p-5 shadow-2xl">
        {/* 标题 */}
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-[15px] font-medium text-ink">
            {friendName} 记得的事
            <span className="ml-2 text-[11px] font-normal text-faint">
              共 {Object.values(counts).reduce((a, b) => a + b, 0)} 条
            </span>
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-2 py-0.5 text-faint hover:bg-chip hover:text-muted"
          >
            ✕
          </button>
        </div>

        {/* 记忆 / 改动留痕（D230）—— 小纸条已挪到「日程」面板（D245） */}
        <div className="mb-2 flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setView('memories')}
            className={`rounded-full border px-2.5 py-0.5 text-[11.5px] transition-colors ${
              view === 'memories' ? 'border-accent text-accent' : 'border-line-strong text-muted'
            }`}
          >
            记忆 {total}
          </button>
          <button
            type="button"
            onClick={() => {
              setView('events')
              void loadEvents()
            }}
            className={`rounded-full border px-2.5 py-0.5 text-[11.5px] transition-colors ${
              view === 'events' ? 'border-accent text-accent' : 'border-line-strong text-muted'
            }`}
            title="她 / 系统动过、删过哪些记忆（D230 留痕）"
          >
            改动 {events.length}
          </button>
          <span className="ml-2 text-[10.5px] text-faint">
            {view === 'events'
              ? '她 / 系统动过哪些记忆（留痕，能看出把哪条降了级）'
              : '每层是什么意思，指上去有提示'}
          </span>
        </div>

        {view === 'events' ? (
          <div className="flex-1 space-y-1.5 overflow-y-auto pr-1">
            {eventsBusy && <p className="py-10 text-center text-[12.5px] text-faint">加载中…</p>}
            {!eventsBusy && events.length === 0 && (
              <p className="py-10 text-center text-[12.5px] text-faint">
                还没有改动记录 —— 她动过记忆之后这里会出现
              </p>
            )}
            {events.map((event) => (
              <div
                key={event.id}
                className="flex items-start gap-2 rounded-xl border border-line/70 bg-surface/70 px-3 py-2"
              >
                <span className="mt-0.5 shrink-0 rounded-full border border-line-strong px-1.5 text-[10px] text-muted">
                  {ACTOR_LABEL[event.actor] ?? event.actor}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[12.5px] text-ink">{describeEvent(event)}</p>
                  {eventText(event) && (
                    <p className="mt-0.5 whitespace-pre-wrap break-words text-[11px] text-faint">
                      {eventText(event)}
                    </p>
                  )}
                  <p className="mt-0.5 text-[10px] text-faint">{whenOf(event.created_at)}</p>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <>
        {/* 搜索 + 排序 */}
        <div className="mb-2 flex items-center gap-2">
          <input
            className="flex-1 rounded-xl border border-line bg-surface/90 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/60"
            placeholder="搜内容（比如「生日」「Rust」）"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="flex shrink-0 items-center gap-1 text-[11px] text-muted">
            排
            {SORTS.map((option) => (
              <button
                key={option.key}
                type="button"
                onClick={() => setSort(option.key)}
                className={`rounded-full border px-2 py-0.5 transition-colors ${
                  sort === option.key
                    ? 'border-accent text-accent'
                    : 'border-line-strong text-muted hover:border-accent/50 hover:text-accent'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        {/* 分层 tab（带条数） */}
        <div className="mb-3 flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setLayer('')}
            className={`rounded-full border px-2.5 py-0.5 text-[11.5px] transition-colors ${
              layer === '' ? 'border-accent text-accent' : 'border-line-strong text-muted'
            }`}
          >
            全部 {Object.values(counts).reduce((a, b) => a + b, 0)}
          </button>
          {LAYER_ORDER.map((key) => (
            <button
              key={key}
              type="button"
              title={LAYER_HINT[key]}
              onClick={() => setLayer(key)}
              className={`rounded-full border px-2.5 py-0.5 text-[11.5px] transition-colors ${
                layer === key
                  ? 'border-accent text-accent'
                  : 'border-line-strong text-muted hover:border-accent/50'
              }`}
            >
              {LAYER_LABEL[key]} {counts[key] ?? 0}
            </button>
          ))}
          <span className="ml-2 text-[10.5px] text-faint">
            {layer ? LAYER_HINT[layer as Memory['layer']] : '每层是什么意思，指上去有提示'}
          </span>
        </div>

        {/* 手写一条 */}
        <div className="mb-2 flex items-center gap-2">
          <input
            className="flex-1 rounded-xl border border-line bg-surface/90 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/60"
            placeholder="往它的记事本里写一条（它下次就会记得）"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void add()
            }}
          />
          <select
            className="rounded-xl border border-line bg-surface/90 px-2 py-1.5 text-[12px] text-muted outline-none"
            value={draftLayer}
            onChange={(e) => setDraftLayer(e.target.value)}
          >
            {LAYER_ORDER.map((key) => (
              <option key={key} value={key}>
                {LAYER_LABEL[key]}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void add()}
            disabled={busy || !draft.trim()}
            className="rounded-xl px-3 py-1.5 text-[12.5px] font-medium text-white disabled:opacity-40"
            style={{ background: ACCENT }}
          >
            写入
          </button>
        </div>
        {note && <p className="mb-2 text-[11px] text-muted">{note}</p>}

        {/* 列表 */}
        <div className="flex-1 space-y-1.5 overflow-y-auto pr-1">
          {loading && <p className="py-10 text-center text-[12.5px] text-faint">加载中…</p>}
          {!loading && items.length === 0 && (
            <p className="py-10 text-center text-[12.5px] text-faint">
              {search || layer ? '这个条件下没有记忆' : '它现在还什么都不记得 —— 多聊聊，或者在上面写一条'}
            </p>
          )}
          {!loading &&
            items.map((memory) => (
              <Row key={memory.id} memory={memory} onChanged={() => void load(0, false)} />
            ))}
          {!loading && hasMore && (
            <button
              type="button"
              disabled={moreBusy}
              onClick={() => {
                setMoreBusy(true)
                void load(items.length, true).finally(() => setMoreBusy(false))
              }}
              className="w-full rounded-lg border border-dashed border-line-strong py-2 text-[11.5px] text-muted hover:border-accent/50 hover:text-accent disabled:opacity-40"
            >
              {moreBusy ? '加载中…' : `加载更多（还有 ${total - items.length} 条）`}
            </button>
          )}
          {!loading && items.length > 0 && !hasMore && (
            <p className="py-2 text-center text-[10.5px] text-faint">—— 到底了 ——</p>
          )}
        </div>
          </>
        )}
      </div>
    </div>
  )
}
