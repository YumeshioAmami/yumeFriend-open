/**
 * 手机壳：某个朋友的**日程**（v4.0 / D243）。
 *
 * 日程 = 她和你的约定。手机上够用就行：
 *   · 没结束的列在上面，点「完成」打勾、「删除」扔掉；
 *   · 添加：一句内容 + 日期（今天/明天/后天快捷）+ 时刻（不填 = 只到日，她找机会提）；
 *   · 已结束的折起来（默认不显示）。
 */

import { useEffect, useState } from 'react'

import { api, type Schedule } from '../shared/api'

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function dateInput(offsetDays: number): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function whenText(item: Schedule): string {
  const [y, m, d] = item.date.split('-')
  const day = y && m && d ? `${Number(m)} 月 ${Number(d)} 日` : item.date
  return item.time ? `${day} ${item.time}` : `${day}（只到日）`
}

const STATE_LABEL: Record<Schedule['state'], string> = {
  pending: '等着',
  active: '她提过了',
  settled: '有结果了',
  archived: '翻篇了',
}

export default function ScheduleScreen({
  friendId,
  friendName,
  onBack,
}: {
  friendId: string
  friendName: string
  onBack: () => void
}) {
  const [items, setItems] = useState<Schedule[]>([])
  const [showEnded, setShowEnded] = useState(false)
  const [draft, setDraft] = useState('')
  const [date, setDate] = useState(() => dateInput(0))
  const [time, setTime] = useState('')
  const [owner, setOwner] = useState<Schedule['owner']>('user')
  const [hint, setHint] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      setItems(await api.listSchedules(friendId))
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }

  useEffect(() => {
    void load()
  }, [friendId])

  async function add() {
    const title = draft.trim()
    if (!title) return
    setBusy(true)
    try {
      await api.createSchedule({
        friend_id: friendId,
        title,
        date,
        time: time || null,
        owner,
      })
      setDraft('')
      setHint(time ? '加好了，到点她会提醒你' : '加好了，到那天她会找机会跟你提')
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function settle(id: string, reason: 'done' | 'dropped') {
    setBusy(true)
    try {
      await api.updateSchedule(id, { state: 'settled', settled_reason: reason })
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string) {
    setBusy(true)
    try {
      await api.deleteSchedule(id)
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const open = items.filter((item) => item.state === 'pending' || item.state === 'active')
  const ended = items.filter((item) => item.state === 'settled' || item.state === 'archived')

  return (
    <div className="min-h-dvh bg-page pb-[max(16px,env(safe-area-inset-bottom))]">
      <header className="sticky top-0 z-10 flex items-center gap-2 border-b border-line/70 bg-page/95 px-2 pb-2.5 pt-[max(8px,env(safe-area-inset-top))] backdrop-blur">
        <button
          type="button"
          onClick={onBack}
          aria-label="返回"
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-[22px] text-muted active:bg-surface-hover"
        >
          ‹
        </button>
        <div className="min-w-0 flex-1">
          <p className="text-[16px] font-medium text-ink">日程</p>
          <p className="text-[11.5px] text-muted">
            {friendName} 和你一起记的事 —— 有时刻的到点一定提醒
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowEnded((prev) => !prev)}
          className="h-11 shrink-0 rounded-2xl border border-line bg-surface px-3 text-[12.5px] text-muted active:bg-surface-hover"
        >
          {showEnded ? '只看没结束' : '看翻篇的'}
        </button>
      </header>

      {hint && <p className="px-4 pt-2 text-[11.5px] text-muted">{hint}</p>}

      <div className="px-4 pt-3">
        <div className="rounded-2xl border border-line/70 bg-surface/70 p-3">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="记一件事？（比如：9 月 24 号去买书）"
            className="h-11 w-full rounded-xl border border-line bg-surface px-3 text-[14px] text-ink outline-none focus:border-accent/60"
          />
          <div className="mt-2 flex flex-wrap gap-1.5">
            {(
              [
                [0, '今天'],
                [1, '明天'],
                [2, '后天'],
              ] as const
            ).map(([offset, label]) => (
              <button
                key={label}
                type="button"
                onClick={() => setDate(dateInput(offset))}
                className={`h-9 rounded-full border px-3 text-[12.5px] ${
                  date === dateInput(offset) ? 'border-accent text-accent' : 'border-line text-muted'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-2">
            <input
              type="date"
              value={date}
              onChange={(event) => setDate(event.target.value)}
              className="h-11 flex-1 rounded-xl border border-line bg-surface px-3 text-[13px] text-ink outline-none focus:border-accent/60"
            />
            <input
              type="time"
              value={time}
              onChange={(event) => setTime(event.target.value)}
              title="不填 = 只到日"
              className="h-11 w-[110px] rounded-xl border border-line bg-surface px-3 text-[13px] text-ink outline-none focus:border-accent/60"
            />
          </div>
          <div className="mt-2 flex items-center gap-1.5">
            {(
              [
                ['user', '我的事'],
                ['friend', '她的事'],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setOwner(key)}
                className={`h-9 rounded-full border px-3 text-[12.5px] ${
                  owner === key ? 'border-accent text-accent' : 'border-line text-muted'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            type="button"
            disabled={busy || !draft.trim()}
            onClick={() => void add()}
            className="mt-2 h-12 w-full rounded-2xl text-[14px] text-white disabled:opacity-40"
            style={{ background: 'var(--color-accent)' }}
          >
            加一条
          </button>
        </div>
      </div>

      <ul className="px-4 pt-3">
        {[...open, ...(showEnded ? ended : [])].map((item) => (
          <li key={item.id} className="flex items-center gap-3 border-b border-line/60 py-3">
            <div className="min-w-0 flex-1">
              <p
                className={`text-[15px] ${
                  item.state === 'settled' || item.state === 'archived'
                    ? 'text-faint line-through'
                    : 'text-ink'
                }`}
              >
                {item.title}
              </p>
              <p className="mt-0.5 text-[11.5px] text-faint">
                {whenText(item)} · {item.owner === 'friend' ? '她记的' : '我记的'} ·{' '}
                {item.settled_reason === 'dropped' ? '不做了' : STATE_LABEL[item.state]}
              </p>
            </div>
            {open.includes(item) && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void settle(item.id, 'done')}
                className="h-11 shrink-0 rounded-xl border border-line bg-surface px-3 text-[12.5px] text-ink active:bg-surface-hover"
              >
                完成
              </button>
            )}
            <button
              type="button"
              disabled={busy}
              onClick={() => void remove(item.id)}
              className="h-11 shrink-0 rounded-xl border border-line bg-surface px-3 text-[12.5px] text-muted active:bg-surface-hover"
            >
              删除
            </button>
          </li>
        ))}
      </ul>

      {!open.length && !ended.length && (
        <p className="px-5 py-10 text-center text-[13px] text-muted">
          还没有日程 —— 上面加一条，或者直接跟她说"记得提醒我…"。
        </p>
      )}
    </div>
  )
}
