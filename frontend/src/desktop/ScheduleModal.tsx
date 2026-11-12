/** 日程面板（api.md【v4.0】/ D243、D244、D245）。
 *
 * 日程不是"表"，是**她和你的约定**：母表唯一，界面 / 她（工具）/ 扫描器改的都是这一张。
 * 两条最重要的口径：
 *   · **有时刻** → 到点一定提醒你（不受在场限制，D35）；
 *   · **只到日** → 到那天不响，她会在日常/主动消息里找机会跟你提。
 *
 * D245：**小纸条也归这里**（以前塞在记忆面板里）—— 日程 + 她惦记的事，一个面板看全。
 * 这里能手动增 / 改期 / 标完成 / 删；只写 pending / settled（active / archived 归系统）。
 */

import { useCallback, useEffect, useState } from 'react'

import { api, type Schedule, type Thought } from '../shared/api'
import { ACCENT } from '../shared/ui'

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function todayInput(): string {
  const d = new Date()
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function humanDate(item: Schedule): string {
  const [y, m, d] = item.date.split('-')
  const day = y && m && d ? `${Number(m)} 月 ${Number(d)} 日` : item.date
  return item.time ? `${day} ${item.time}` : `${day}（只到日）`
}

function awayDate(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
}

const NATURE_LABEL: Record<Schedule['nature'], string> = {
  start: '开始做',
  due: '截止',
  appointment: '约好的',
  remind: '提醒',
}

const STATE_LABEL: Record<Schedule['state'], string> = {
  pending: '等着',
  active: '她提过了',
  settled: '有结果了',
  archived: '翻篇了',
}

export default function ScheduleModal({
  friendId,
  friendName,
  onClose,
}: {
  friendId: string
  friendName: string
  onClose: () => void
}) {
  const [tab, setTab] = useState<'schedules' | 'thoughts'>('schedules')
  const [items, setItems] = useState<Schedule[]>([])
  const [thoughts, setThoughts] = useState<Thought[]>([])
  const [showEnded, setShowEnded] = useState(false)
  // v4.1（D246）：筛选都要有「全部」（我的事/她的事 + 四类性质）
  const [filterOwner, setFilterOwner] = useState<'all' | Schedule['owner']>('all')
  const [filterNature, setFilterNature] = useState<'all' | Schedule['nature']>('all')
  const [title, setTitle] = useState('')
  const [date, setDate] = useState(todayInput)
  const [time, setTime] = useState('')
  const [nature, setNature] = useState<Schedule['nature']>('remind')
  const [owner, setOwner] = useState<Schedule['owner']>('user')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [editDate, setEditDate] = useState('')
  const [editTime, setEditTime] = useState('')

  const reload = useCallback(async () => {
    setItems(await api.listSchedules(friendId))
  }, [friendId])

  const loadThoughts = useCallback(async () => {
    try {
      const page = await api.listThoughts(friendId)
      setThoughts(page.thoughts)
    } catch {
      setThoughts([])
    }
  }, [friendId])

  useEffect(() => {
    void reload()
    void loadThoughts()
  }, [reload, loadThoughts])

  async function add() {
    if (!title.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await api.createSchedule({
        friend_id: friendId,
        title: title.trim(),
        date,
        time: time || null,
        nature,
        owner,
      })
      setTitle('')
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function settle(id: string, reason: 'done' | 'dropped') {
    await api.updateSchedule(id, { state: 'settled', settled_reason: reason })
    await reload()
  }

  async function reschedule(id: string) {
    await api.updateSchedule(id, { date: editDate, time: editTime || null })
    setEditing(null)
    await reload()
  }

  async function dropThought(id: string) {
    await api.dropThought(id)
    await loadThoughts()
  }

  const visible = items.filter((item) => {
    if (!showEnded && item.state !== 'pending' && item.state !== 'active') return false
    if (filterOwner !== 'all' && item.owner !== filterOwner) return false
    if (filterNature !== 'all' && item.nature !== filterNature) return false
    return true
  })

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim/40 backdrop-blur-sm">
      <div className="flex max-h-[84%] w-[620px] max-w-[94%] flex-col rounded-2xl border border-line bg-page p-5 shadow-2xl">
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-[15px] font-medium text-ink">
            {friendName} 的日程
            <span className="ml-2 text-[11px] font-normal text-faint">
              有时刻的到点一定提醒你；只到日的她找机会跟你提
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

        {/* D245：日程 / 小纸条（她惦记着想说的那件事）—— 一个面板两栏 */}
        <div className="mb-2 flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setTab('schedules')}
            className={`rounded-full border px-2.5 py-0.5 text-[11.5px] transition-colors ${
              tab === 'schedules' ? 'border-accent text-accent' : 'border-line-strong text-muted'
            }`}
          >
            日程 {items.length}
          </button>
          <button
            type="button"
            onClick={() => {
              setTab('thoughts')
              void loadThoughts()
            }}
            className={`rounded-full border px-2.5 py-0.5 text-[11.5px] transition-colors ${
              tab === 'thoughts' ? 'border-accent text-accent' : 'border-line-strong text-muted'
            }`}
          >
            小纸条 {thoughts.length}
          </button>
          <span className="ml-2 text-[10.5px] text-faint">
            {tab === 'thoughts'
              ? '她惦记着、想说还没说的话（说过了或太久就自动翻篇）'
              : '你和她的约定：我记的 / 她记的，都能改期、标完成'}
          </span>
        </div>

        {tab === 'thoughts' ? (
          <div className="flex-1 space-y-1.5 overflow-y-auto pr-1">
            {thoughts.length === 0 && (
              <p className="py-10 text-center text-[12.5px] text-faint">
                她现在没有攒着的事 —— 她说「回头想问你件事」、或者有日程到日子了，这里会出现一张小纸条
              </p>
            )}
            {thoughts.map((t) => (
              <div
                key={t.id}
                className="flex items-start gap-2 rounded-xl border border-line/70 bg-surface/70 px-3 py-2"
              >
                <span className="mt-0.5 shrink-0 rounded-full border border-line-strong px-1.5 text-[10px] text-muted">
                  {t.state === 'awaiting' ? '说了·等你回' : t.kind === 'due' ? '到点说' : '随缘'}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="whitespace-pre-wrap text-[12.5px] text-ink">{t.content}</p>
                  <p className="mt-0.5 text-[10px] text-faint">
                    {t.state === 'awaiting'
                      ? '她说过了、你还没回 —— 她不会追，等你下次开口时顺口提一句'
                      : '她会在下次心跳或合适的空档说'}
                    {t.expires_at ? ` · 到 ${awayDate(t.expires_at)} 自动放下` : ''}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void dropThought(t.id)}
                  className="shrink-0 rounded-lg border border-line-strong px-2 py-0.5 text-[11px] text-muted hover:border-accent/60 hover:text-accent"
                >
                  划掉
                </button>
              </div>
            ))}
          </div>
        ) : (
          <>
            <div className="mb-3 space-y-2">
              <input
                className="w-full rounded-xl border border-line bg-surface/90 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/60"
                placeholder="记一件事……（比如「9 月 24 号去买书」）"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void add()
                }}
              />
              <div className="flex flex-wrap items-center gap-2">
                <input
                  type="date"
                  className="rounded-xl border border-line bg-surface/90 px-2 py-1 text-[12px] outline-none focus:border-accent/60"
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                />
                <input
                  type="time"
                  title="不填 = 只到日（不响，她找机会提）"
                  className="rounded-xl border border-line bg-surface/90 px-2 py-1 text-[12px] outline-none focus:border-accent/60"
                  value={time}
                  onChange={(e) => setTime(e.target.value)}
                />
                <select
                  className="rounded-xl border border-line bg-surface/90 px-2 py-1 text-[12px] outline-none focus:border-accent/60"
                  value={nature}
                  onChange={(e) => setNature(e.target.value as Schedule['nature'])}
                >
                  {(Object.keys(NATURE_LABEL) as Schedule['nature'][]).map((key) => (
                    <option key={key} value={key}>
                      {NATURE_LABEL[key]}
                    </option>
                  ))}
                </select>
                <select
                  className="rounded-xl border border-line bg-surface/90 px-2 py-1 text-[12px] outline-none focus:border-accent/60"
                  value={owner}
                  onChange={(e) => setOwner(e.target.value as Schedule['owner'])}
                >
                  <option value="user">我的事</option>
                  <option value="friend">她的事</option>
                </select>
                <button
                  type="button"
                  onClick={() => void add()}
                  disabled={busy || !title.trim()}
                  className="ml-auto rounded-xl px-3 py-1.5 text-[12.5px] font-medium text-white disabled:opacity-40"
                  style={{ background: ACCENT }}
                >
                  加一条
                </button>
              </div>
              {error && <p className="text-[11px] text-red-600">出错了：{error}</p>}
            </div>

            <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
              <label className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={showEnded}
                  onChange={(e) => setShowEnded(e.target.checked)}
                />
                连翻篇的也显示
              </label>
              <select
                className="rounded-lg border border-line bg-surface/90 px-1.5 py-0.5 text-[11px] outline-none focus:border-accent/60"
                value={filterOwner}
                onChange={(e) => setFilterOwner(e.target.value as 'all' | Schedule['owner'])}
              >
                <option value="all">全部 · 谁记的</option>
                <option value="user">我的事</option>
                <option value="friend">她的事</option>
              </select>
              <select
                className="rounded-lg border border-line bg-surface/90 px-1.5 py-0.5 text-[11px] outline-none focus:border-accent/60"
                value={filterNature}
                onChange={(e) => setFilterNature(e.target.value as 'all' | Schedule['nature'])}
              >
                <option value="all">全部 · 性质</option>
                {(Object.keys(NATURE_LABEL) as Schedule['nature'][]).map((key) => (
                  <option key={key} value={key}>
                    {NATURE_LABEL[key]}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex-1 space-y-1.5 overflow-y-auto pr-1">
              {visible.length === 0 && (
                <p className="py-8 text-center text-[12.5px] text-faint">
                  还没有日程 —— 你也可以直接跟她说「10 分钟后提醒我喝水」
                </p>
              )}
              {visible.map((item) => (
                <div
                  key={item.id}
                  className={`group rounded-lg border px-3 py-1.5 ${
                    item.state === 'settled' || item.state === 'archived'
                      ? 'border-line/60 bg-surface-hover'
                      : 'border-line/70 bg-surface/70'
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <span className="mt-0.5 shrink-0 rounded-full border border-line-strong px-1.5 text-[10px] text-muted">
                      {item.owner === 'friend' ? '她' : '我'}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p
                        className={`text-[12.5px] leading-relaxed ${
                          item.state === 'settled' || item.state === 'archived'
                            ? 'text-faint line-through'
                            : 'text-ink'
                        }`}
                      >
                        {item.title}
                      </p>
                      <p className="text-[10.5px] text-faint">
                        {humanDate(item)} · {NATURE_LABEL[item.nature]} ·{' '}
                        {item.settled_reason === 'dropped' ? '不做了' : STATE_LABEL[item.state]}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-1 opacity-0 group-hover:opacity-100">
                      {(item.state === 'pending' || item.state === 'active') && (
                        <>
                          <button
                            type="button"
                            title="做了"
                            onClick={() => void settle(item.id, 'done')}
                            className="rounded px-1.5 text-[10.5px] text-muted hover:text-accent"
                          >
                            ✓
                          </button>
                          <button
                            type="button"
                            title="不做了"
                            onClick={() => void settle(item.id, 'dropped')}
                            className="rounded px-1.5 text-[10.5px] text-muted hover:text-accent"
                          >
                            ✕
                          </button>
                          <button
                            type="button"
                            title="改期"
                            onClick={() => {
                              setEditing(editing === item.id ? null : item.id)
                              setEditDate(item.date)
                              setEditTime(item.time ?? '')
                            }}
                            className="rounded px-1.5 text-[10.5px] text-muted hover:text-accent"
                          >
                            改期
                          </button>
                        </>
                      )}
                      <button
                        type="button"
                        onClick={() => void api.deleteSchedule(item.id).then(reload)}
                        className="rounded px-1.5 text-[10.5px] text-faint hover:text-red-600"
                      >
                        删
                      </button>
                    </div>
                  </div>
                  {editing === item.id && (
                    <div className="mt-1.5 flex items-center gap-2 pl-6">
                      <input
                        type="date"
                        className="rounded-lg border border-line bg-surface/90 px-2 py-0.5 text-[11.5px] outline-none focus:border-accent/60"
                        value={editDate}
                        onChange={(e) => setEditDate(e.target.value)}
                      />
                      <input
                        type="time"
                        className="rounded-lg border border-line bg-surface/90 px-2 py-0.5 text-[11.5px] outline-none focus:border-accent/60"
                        value={editTime}
                        onChange={(e) => setEditTime(e.target.value)}
                      />
                      <button
                        type="button"
                        onClick={() => void reschedule(item.id)}
                        className="rounded-lg border border-line-strong px-2 py-0.5 text-[11px] text-muted hover:border-accent/60 hover:text-accent"
                      >
                        存
                      </button>
                      <span className="text-[10px] text-faint">时间清空 = 改回只到日</span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
