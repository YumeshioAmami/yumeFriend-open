/**
 * 全屏游戏模式（v5.1-P1 / D287）。
 *
 * 这里不是"游戏里的画面"，而是**她的游戏后台**：
 * 开局、看状态、管理游戏设置、看数据 / 事件 / 日志。
 * 普通聊天在进入后完全不渲染；退出这里也不会停游戏。
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import {
  api,
  type Friend,
  type GameCheatConfig,
  type GameBlueprint,
  type GameCreativeRecord,
  type GameProfile,
  type GameReflexConfig,
  type GameReflexMode,
  type GameSkill,
  type GameSkillFailure,
  type GameSnapshot,
  type GameStatus,
  type PurposeConfig,
  type Settings,
  type UsageSummary,
} from '../shared/api'
import Avatar from '../shared/Avatar'
import {
  IconBlueprint,
  IconBook,
  IconDatabase,
  IconGamepad,
  IconGear,
  IconLogout,
  IconPlay,
  IconSend,
  IconShield,
  IconSpark,
  IconStack,
  IconStop,
  IconTerminal,
} from '../shared/icons'
import { ACCENT } from '../shared/ui'
import type { StreamState } from '../shared/useChat'
import LogWindow from './LogWindow'

type TabId =
  | 'overview'
  | 'match'
  | 'settings'
  | 'cheat'
  | 'skills'
  | 'blueprints'
  | 'data'
  | 'memory'
  | 'logs'

const TABS: { id: TabId; label: string; hint: string; Icon: typeof IconStack }[] = [
  { id: 'overview', label: '总览', hint: '这一局现在怎么样', Icon: IconStack },
  { id: 'match', label: '对局', hint: '进服 / 停局 / 重连参数', Icon: IconGamepad },
  { id: 'settings', label: '设置', hint: '游戏快脑与说话节拍', Icon: IconGear },
  { id: 'cheat', label: '作弊', hint: '难度 profile 与逐项作弊', Icon: IconShield },
  { id: 'skills', label: '技能', hint: '她学会的做事方法', Icon: IconSpark },
  { id: 'blueprints', label: '蓝图', hint: '建造与自由创作', Icon: IconBlueprint },
  { id: 'data', label: '数据', hint: '背包 / 附近 / 任务 / 事件', Icon: IconDatabase },
  { id: 'memory', label: '记忆', hint: '本局摘要与一起玩过', Icon: IconBook },
  { id: 'logs', label: '日志', hint: '后台在干什么', Icon: IconTerminal },
]

const MODE_LABELS: Record<string, string> = {
  follow: '跟随',
  chat: '聊天',
  free: '自由',
}

const MODE_HINTS: Record<string, string> = {
  follow: '跟着你',
  chat: '待在你附近',
  free: '自己找事做',
}

const PLAN_STATUS_LABELS: Record<string, string> = {
  executing: '执行中',
  paused: '暂停',
  cancelled: '已取消',
  completed: '已完成',
}

const TASK_STATUS_LABELS: Record<string, string> = {
  pending: '待做',
  executing: '正在做',
  success: '完成',
  failed: '失败',
}

const QUICK_COMMANDS = [
  { id: 'come', label: '过来' },
  { id: 'follow', label: '跟着我' },
  { id: 'free', label: '自己玩' },
  { id: 'stop', label: '停一下' },
  { id: 'plan', label: '看计划' },
] as const

const PROFILE_META: {
  id: GameProfile
  label: string
  hint: string
  preset: Record<string, boolean>
}[] = [
  {
    id: 'survival',
    label: '生存',
    hint: '正常采集、合成、受伤',
    preset: {},
  },
  {
    id: 'assisted',
    label: '辅助',
    hint: '治疗 / 喂食 / 时间 / 天气',
    preset: { heal: true, feed: true, time: true, weather: true },
  },
  {
    id: 'operator',
    label: '管理员',
    hint: '命令 / 给物 / 传送 / 秒挖',
    preset: {
      command: true,
      give: true,
      teleport: true,
      gamemode: true,
      time: true,
      weather: true,
      heal: true,
      feed: true,
      instant_mine: true,
      reveal_map: true,
    },
  },
  {
    id: 'creative',
    label: '创造',
    hint: '全部能力，包括飞行与无敌',
    preset: {
      command: true,
      give: true,
      teleport: true,
      gamemode: true,
      time: true,
      weather: true,
      heal: true,
      feed: true,
      instant_mine: true,
      fly: true,
      no_damage: true,
      reveal_map: true,
    },
  },
]

const ABILITY_META: { id: string; label: string; hint: string; risky?: boolean }[] = [
  { id: 'command', label: '任意命令', hint: '执行一条服务器命令；权限不够会被服务器拒绝', risky: true },
  { id: 'give', label: '给物品', hint: '直接给自己物品', risky: true },
  { id: 'teleport', label: '传送', hint: '传送到指定坐标' },
  { id: 'gamemode', label: '切换游戏模式', hint: '生存 / 创造 / 冒险 / 旁观' },
  { id: 'time', label: '时间', hint: 'day / noon / night / midnight' },
  { id: 'weather', label: '天气', hint: 'clear / rain / thunder' },
  { id: 'heal', label: '治疗', hint: '瞬间治疗 + 回饱食' },
  { id: 'feed', label: '喂食', hint: '给自己回饱食' },
  { id: 'instant_mine', label: '秒挖方块', hint: '把指定坐标变成空气并掉落', risky: true },
  { id: 'fly', label: '飞行', hint: '给自己 mayfly；创造/旁观模式才稳' },
  { id: 'no_damage', label: '无敌', hint: '长时间抗性提升' },
  { id: 'reveal_map', label: '照亮视野', hint: '夜视；不是矿透' },
]

const REFLEX_LABELS: Record<string, string> = {
  self_preservation: '自保',
}

const CONNECTION_KEY = 'yumefriend.game.connection'

function readConnection(): { host: string; port: string; username: string } {
  try {
    const raw = localStorage.getItem(CONNECTION_KEY)
    if (raw) {
      const data = JSON.parse(raw) as Record<string, unknown>
      return {
        host: String(data.host || '127.0.0.1'),
        port: String(data.port || '51856'),
        username: String(data.username || 'Xiyin_'),
      }
    }
  } catch {
    // 本地偏好坏了就用默认值，不影响进游戏
  }
  return { host: '127.0.0.1', port: '51856', username: 'Xiyin_' }
}

function saveConnection(host: string, port: string, username: string): void {
  try {
    localStorage.setItem(CONNECTION_KEY, JSON.stringify({ host, port, username }))
  } catch {
    // 存不了下次就重新填，不是错误
  }
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`
}

function eventLine(event: { type: string; payload: Record<string, unknown> }): string {
  const payload = event.payload || {}
  if (event.type === 'chat') return `${payload.from ?? 'TA'}：${payload.text ?? ''}`
  if (event.type === 'death') return '她死了'
  if (event.type === 'damage') return `受伤 ${payload.amount ?? '?'}`
  if (event.type === 'task_done') {
    return `任务 ${payload.task_id ?? ''} ${payload.ok ? '完成' : '失败'}`
  }
  if (event.type === 'player_near') return `${payload.player ?? '玩家'} 出现`
  if (event.type === 'error') return `出错：${payload.message ?? ''}`
  if (event.type === 'cheat') {
    return `作弊 · ${payload.action ?? '?'} ${payload.ok ? '成功' : '失败'}（source=cheat）`
  }
  if (event.type === 'reflex') {
    return `反射 · ${payload.mode ?? '?'} · ${payload.action ?? '?'}${
      payload.detail ? `（${payload.detail}）` : ''
    }`
  }
  return event.type
}

function Pill({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode
  tone?: 'neutral' | 'ok' | 'warn' | 'accent'
  title?: string
}) {
  const cls =
    tone === 'ok'
      ? 'border-emerald-400/25 bg-emerald-400/10 text-emerald-200'
      : tone === 'warn'
        ? 'border-amber-400/30 bg-amber-400/10 text-amber-200'
        : tone === 'accent'
          ? 'border-accent/35 bg-accent/10 text-accent'
          : 'border-line/80 bg-surface/60 text-muted'
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] ${cls}`}
    >
      {children}
    </span>
  )
}

function Section({
  title,
  hint,
  action,
  children,
  className = '',
}: {
  title: string
  hint?: string
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={`rounded-2xl border border-line/70 bg-surface/55 p-3.5 shadow-[0_10px_28px_rgba(0,0,0,0.08)] ${className}`}
    >
      <div className="mb-2 flex items-start gap-2">
        <div className="min-w-0">
          <h3 className="text-[12.5px] font-medium text-ink">{title}</h3>
          {hint && <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">{hint}</p>}
        </div>
        {action && <div className="ml-auto shrink-0">{action}</div>}
      </div>
      {children}
    </section>
  )
}

function EmptyHint({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-line-strong/70 bg-page/20 px-3 py-5 text-center text-[11.5px] leading-relaxed text-faint">
      {children}
    </div>
  )
}

function ProgressRail({ label, value }: { label: string; value: number | null }) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-[10.5px] text-faint">
        <span>{label}</span>
        <span>{value == null ? '—' : `${Math.round(value)}%`}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-chip/80">
        <div
          className="h-full rounded-full transition-[width] duration-300"
          style={{
            width: `${Math.max(0, Math.min(100, value ?? 0))}%`,
            background: ACCENT,
          }}
        />
      </div>
    </div>
  )
}

function GameSettingsPanel({
  settings,
  onChanged,
}: {
  settings: Settings | null
  onChanged: () => void
}) {
  const [chatInterval, setChatInterval] = useState(
    settings?.game_chat_interval_seconds ?? 90,
  )
  const [fast, setFast] = useState<PurposeConfig>(() => ({
    ...(settings?.purposes?.game_fast ?? {}),
  }))
  const [busy, setBusy] = useState(false)
  const [test, setTest] = useState<{ state: 'idle' | 'busy' | 'ok' | 'fail'; text?: string }>({
    state: 'idle',
  })
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reflexes, setReflexes] = useState<GameReflexConfig | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void api.gameReflexes().then(setReflexes).catch(() => undefined)
    }, 0)
    return () => window.clearTimeout(timer)
  }, [])

  const mode = fast.mode === 'api' ? 'api' : 'local'

  function patchFast(patch: PurposeConfig) {
    setFast((prev) => ({ ...prev, ...patch }))
  }

  function patchReflex(name: string, patch: Partial<GameReflexMode>) {
    setReflexes((prev) =>
      prev
        ? {
            ...prev,
            modes: {
              ...prev.modes,
              [name]: { ...(prev.modes[name] || {}), ...patch } as GameReflexMode,
            },
          }
        : prev,
    )
  }

  async function save() {
    if (busy) return
    setBusy(true)
    setMessage(null)
    setError(null)
    try {
      const { api_key_set: _drop, ...draft } = fast
      const clean: Record<string, unknown> = { ...draft }
      if (!String(clean.api_key ?? '').trim()) delete clean.api_key
      await api.patchSettings({
        game_chat_interval_seconds: Math.max(0, Math.min(3600, Number(chatInterval) || 0)),
        purposes: { game_fast: clean },
      })
      if (reflexes) {
        await api.setGameReflexes({ modes: reflexes.modes })
      }
      setMessage('游戏设置已保存')
      onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function testFast() {
    setTest({ state: 'busy' })
    try {
      const { api_key_set: _drop, ...draft } = fast
      const result = await api.testPurpose('game_fast', draft as Record<string, unknown>)
      setTest({
        state: result.ok ? 'ok' : 'fail',
        text: result.ok ? `通了 · ${result.latency_ms ?? '?'} ms` : result.error ?? '连不上',
      })
    } catch (e) {
      setTest({ state: 'fail', text: e instanceof Error ? e.message : String(e) })
    }
  }

  return (
    <div className="grid gap-3 xl:grid-cols-2">
      <Section
        title="她多久主动说一句"
        hint="只在游戏进行中生效。想让她安静就填 0。"
      >
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={0}
            max={3600}
            value={chatInterval}
            onChange={(e) => setChatInterval(Number(e.target.value))}
            className="w-28 rounded-xl border border-line bg-page/50 px-3 py-1.5 text-[12.5px] text-ink outline-none focus:border-accent/60"
          />
          <span className="text-[11.5px] text-muted">秒</span>
        </div>
        <p className="mt-2 text-[10.5px] leading-relaxed text-faint">
          她不会按节拍机械汇报；只是偶尔自己开个话头。真正的动作推进仍由任务完成后续跑。
        </p>
      </Section>

      <Section
        title="游戏快脑"
        hint="简单下一步优先问本地模型；不配就自动用聊天模型，不额外收费。"
        action={
          <Pill tone={fast.base_url && fast.model ? 'ok' : 'neutral'}>
            {fast.base_url && fast.model ? '已配置' : '跟随聊天'}
          </Pill>
        }
      >
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            onClick={() => patchFast({ mode: 'local' })}
            className={`rounded-full border px-2.5 py-0.5 text-[10.5px] ${
              mode === 'local'
                ? 'border-accent/60 bg-accent/10 text-accent'
                : 'border-line-strong text-muted hover:text-accent'
            }`}
          >
            本地 Ollama
          </button>
          <button
            type="button"
            onClick={() => patchFast({ mode: 'api' })}
            className={`rounded-full border px-2.5 py-0.5 text-[10.5px] ${
              mode === 'api'
                ? 'border-accent/60 bg-accent/10 text-accent'
                : 'border-line-strong text-muted hover:text-accent'
            }`}
          >
            云端 API
          </button>
          <button
            type="button"
            onClick={() =>
              patchFast({
                mode: 'local',
                base_url: 'http://127.0.0.1:11434/v1',
                model: 'qwen2.5:3b-instruct',
                proxy: 'none',
              })
            }
            className="rounded-full border border-line-strong px-2.5 py-0.5 text-[10.5px] text-faint hover:border-accent/50 hover:text-accent"
          >
            一键填本地 Ollama
          </button>
        </div>

        <div className="mt-2 space-y-1.5">
          <label className="flex items-center gap-2">
            <span className="w-[66px] shrink-0 text-right text-[10.5px] text-faint">服务地址</span>
            <input
              value={fast.base_url ?? ''}
              onChange={(e) => patchFast({ base_url: e.target.value })}
              placeholder="http://127.0.0.1:11434/v1"
              className="min-w-0 flex-1 rounded-lg border border-line bg-page/50 px-2 py-1 text-[11.5px] text-ink outline-none focus:border-accent/60"
            />
          </label>
          <label className="flex items-center gap-2">
            <span className="w-[66px] shrink-0 text-right text-[10.5px] text-faint">模型名</span>
            <input
              value={fast.model ?? ''}
              onChange={(e) => patchFast({ model: e.target.value })}
              placeholder="qwen2.5:3b-instruct"
              className="min-w-0 flex-1 rounded-lg border border-line bg-page/50 px-2 py-1 text-[11.5px] text-ink outline-none focus:border-accent/60"
            />
          </label>
          <label className="flex items-center gap-2">
            <span className="w-[66px] shrink-0 text-right text-[10.5px] text-faint">密钥</span>
            <input
              type="password"
              value={fast.api_key ?? ''}
              onChange={(e) => patchFast({ api_key: e.target.value })}
              placeholder={fast.api_key_set ? '已保存 · 留空不改' : '本地模型通常不用填'}
              className="min-w-0 flex-1 rounded-lg border border-line bg-page/50 px-2 py-1 text-[11.5px] text-ink outline-none focus:border-accent/60"
            />
          </label>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void testFast()}
            disabled={test.state === 'busy'}
            className="rounded-lg border border-line-strong px-2.5 py-1 text-[10.5px] text-muted hover:border-accent/60 hover:text-accent disabled:opacity-40"
          >
            {test.state === 'busy' ? '测试中…' : '测试连接'}
          </button>
          {test.text && (
            <span
              className={`text-[10.5px] ${
                test.state === 'ok' ? 'text-emerald-300' : 'text-red-300'
              }`}
            >
              {test.text}
            </span>
          )}
        </div>
      </Section>

      <Section
        title="反射 mode"
        hint="身体本能，不调 LLM；优先级高的先跑。自保会在落水时找岸、着火/岩浆时脱身。"
        className="xl:col-span-2"
        action={<Pill>{reflexes?.active_modes?.length ?? 0} 个启用</Pill>}
      >
        {!reflexes ? (
          <EmptyHint>反射配置加载中…</EmptyHint>
        ) : (
          <div className="space-y-1.5">
            {Object.entries(reflexes.modes).map(([name, config]) => (
              <div
                key={name}
                className={`grid grid-cols-[minmax(92px,0.9fr)_48px_70px_86px] items-center gap-2 rounded-xl border px-2.5 py-1.5 ${
                  config.enabled
                    ? 'border-line/60 bg-page/25'
                    : 'border-line/40 bg-page/10 opacity-55'
                }`}
              >
                <label className="flex items-center gap-1.5 text-[11px] text-ink-soft">
                  <input
                    type="checkbox"
                    checked={config.enabled}
                    onChange={(e) => patchReflex(name, { enabled: e.target.checked })}
                  />
                  {REFLEX_LABELS[name] || name}
                </label>
                <label className="text-[9.5px] text-faint">
                  优先级
                  <input
                    type="number"
                    min={0}
                    max={999}
                    value={config.priority}
                    onChange={(e) =>
                      patchReflex(name, { priority: Number(e.target.value) || 0 })
                    }
                    className="mt-0.5 w-full rounded-md border border-line bg-page/40 px-1 py-0.5 text-[10px] text-ink outline-none focus:border-accent/60"
                  />
                </label>
                <label className="flex items-center gap-1 text-[9.5px] text-muted">
                  <input
                    type="checkbox"
                    checked={config.interrupts}
                    onChange={(e) =>
                      patchReflex(name, { interrupts: e.target.checked })
                    }
                  />
                  可打断
                </label>
                <label className="text-[9.5px] text-faint">
                  冷却（秒）
                  <input
                    type="number"
                    min={0}
                    max={300}
                    step={0.1}
                    value={config.cooldown}
                    onChange={(e) =>
                      patchReflex(name, { cooldown: Number(e.target.value) || 0 })
                    }
                    className="mt-0.5 w-full rounded-md border border-line bg-page/40 px-1 py-0.5 text-[10px] text-ink outline-none focus:border-accent/60"
                  />
                </label>
              </div>
            ))}
          </div>
        )}
      </Section>

      <div className="flex flex-wrap items-center gap-2 xl:col-span-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy}
          className="rounded-xl px-4 py-1.5 text-[12px] font-medium text-white disabled:opacity-40"
          style={{ background: ACCENT }}
        >
          {busy ? '保存中…' : '保存游戏设置'}
        </button>
        {message && <span className="text-[11px] text-emerald-300">{message}</span>}
        {error && <span className="text-[11px] text-red-300">保存失败：{error}</span>}
      </div>
    </div>
  )
}

export default function GameModeShell({
  friend,
  settings,
  stream,
  lastReply,
  onTalk,
  onSettingsChanged,
  onExit,
}: {
  friend: Friend
  settings: Settings | null
  stream: StreamState
  lastReply: string | null
  onTalk: (text: string) => Promise<void>
  onSettingsChanged: () => void
  onExit: () => void
}) {
  const [tab, setTab] = useState<TabId>('overview')
  const [status, setStatus] = useState<GameStatus | null>(null)
  const [snapshot, setSnapshot] = useState<GameSnapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [commandText, setCommandText] = useState('')
  // 真实时长每秒刷新；初值 0，首帧最多显示 0:00，不在 render 里读系统时钟。
  const [clock, setClock] = useState(0)
  const [connection, setConnection] = useState(readConnection)
  const [cheatConfig, setCheatConfig] = useState<GameCheatConfig | null>(null)
  const [rememberCheats, setRememberCheats] = useState(true)
  const [cheatBusy, setCheatBusy] = useState(false)
  const [skills, setSkills] = useState<GameSkill[]>([])
  const [skillFailures, setSkillFailures] = useState<GameSkillFailure[]>([])
  const [skillBusy, setSkillBusy] = useState<string | null>(null)
  const [skillNote, setSkillNote] = useState<string | null>(null)
  const [blueprints, setBlueprints] = useState<GameBlueprint[]>([])
  const [creativeRecords, setCreativeRecords] = useState<GameCreativeRecord[]>([])
  const [blueprintBusy, setBlueprintBusy] = useState<string | null>(null)
  const [blueprintNote, setBlueprintNote] = useState<string | null>(null)
  const [usage, setUsage] = useState<UsageSummary | null>(null)

  const mine = Boolean(status?.active && status.friend_id === friend.id)
  const other = Boolean(status?.active && status.friend_id && status.friend_id !== friend.id)
  const events = status?.recent_events ?? []
  const currentPlan = status?.current_plan ?? null
  const cheatEvents = events.filter(
    (event) => event.type === 'cheat' || event.payload?.source === 'cheat',
  )
  const runningTasks = snapshot?.tasks?.filter((task) => task.status === 'running') ?? []

  const elapsed = useMemo(() => {
    if (!mine || !status?.started_at) return null
    return Math.max(0, clock / 1000 - status.started_at)
  }, [clock, mine, status?.started_at])

  const replyText =
    stream.status !== 'idle' && stream.content
      ? stream.content
      : stream.status === 'thinking'
        ? '她正在想…'
        : stream.status === 'tool_running'
          ? '她正在动手…'
          : lastReply || ''

  useEffect(() => {
    const root = document.documentElement
    const previous = root.dataset.gameMode
    root.dataset.gameMode = 'on'
    return () => {
      if (previous == null) delete root.dataset.gameMode
      else root.dataset.gameMode = previous
    }
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onExit()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onExit])

  useEffect(() => {
    if (!mine) return
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [mine])

  const refreshStatus = useCallback(async () => {
    try {
      const next = await api.gameStatus()
      setStatus(next)
      if (next.active && next.friend_id === friend.id) {
        try {
          setSnapshot(await api.gameSnapshot())
        } catch {
          setSnapshot(null)
        }
      } else {
        setSnapshot(null)
      }
    } catch {
      // 面板不因为一次轮询失败就清空：后端重启时会自己恢复
    }
  }, [friend.id])

  useEffect(() => {
    // 从定时器里发第一次请求：effect 本身不直接 setState，避免无意义的同步级联渲染。
    const first = window.setTimeout(() => void refreshStatus(), 0)
    const timer = window.setInterval(() => void refreshStatus(), 1800)
    return () => {
      window.clearTimeout(first)
      window.clearInterval(timer)
    }
  }, [refreshStatus])

  useEffect(() => {
    if (tab !== 'overview') return
    const load = () => {
      void api.usageSummary(1, 0).then(setUsage).catch(() => undefined)
    }
    const first = window.setTimeout(load, 0)
    const timer = window.setInterval(load, 15000)
    return () => {
      window.clearTimeout(first)
      window.clearInterval(timer)
    }
  }, [tab])

  const loadCheatConfig = useCallback(async () => {
    try {
      setCheatConfig(await api.gameCheats())
    } catch {
      // 作弊配置拉不到不影响游戏本体；页面上会显示未加载
    }
  }, [])

  useEffect(() => {
    const first = window.setTimeout(() => void loadCheatConfig(), 0)
    return () => window.clearTimeout(first)
  }, [loadCheatConfig])

  const loadSkills = useCallback(async () => {
    try {
      const data = await api.gameSkills(friend.id)
      setSkills(data.skills)
      setSkillFailures(data.failures)
    } catch {
      // 技能书拉不到不影响对局
    }
  }, [friend.id])

  useEffect(() => {
    const first = window.setTimeout(() => void loadSkills(), 0)
    return () => window.clearTimeout(first)
  }, [loadSkills])

  const loadBlueprints = useCallback(async () => {
    try {
      const data = await api.gameBlueprints(friend.id)
      setBlueprints(data.blueprints)
      setCreativeRecords(data.creative)
    } catch {
      // 蓝图页拉不到不影响对局
    }
  }, [friend.id])

  useEffect(() => {
    const first = window.setTimeout(() => void loadBlueprints(), 0)
    return () => window.clearTimeout(first)
  }, [loadBlueprints])

  async function start() {
    if (busy) return
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      saveConnection(connection.host, connection.port, connection.username)
      await api.gameStart({
        friend_id: friend.id,
        game_kind: 'minecraft',
        host: connection.host.trim(),
        port: Number(connection.port),
        username: connection.username.trim(),
      })
      await refreshStatus()
      await loadCheatConfig()
      setNote('她开始进服了')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    if (busy) return
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      await api.gameStop()
      await refreshStatus()
      await loadCheatConfig()
      setNote('这一局已经停了')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function sendTalk() {
    const text = commandText.trim()
    if (!text || !mine || stream.status !== 'idle') return
    setCommandText('')
    setError(null)
    try {
      await onTalk(text)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function runQuick(id: (typeof QUICK_COMMANDS)[number]['id']) {
    if (!mine) return
    setError(null)
    setNote(null)
    try {
      if (id === 'plan') {
        setTab('overview')
        setNote('当前计划就在总览里')
        return
      }
      if (id === 'come' || id === 'follow') {
        await api.gameCommand('follow')
        setNote('叫她跟着你了')
      } else if (id === 'free') {
        await api.gameCommand('mode', { mode: 'free' })
        setNote('切到自由活动了')
      } else if (id === 'stop') {
        await api.gameCommand('stop')
        setNote('让她先停一下')
      }
      await refreshStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function applyCheat(patch: {
    profile?: GameProfile
    master_enabled?: boolean
    abilities?: Record<string, boolean>
  }) {
    if (cheatBusy) return
    setCheatBusy(true)
    setError(null)
    setNote(null)
    try {
      const next = await api.setGameCheats({ ...patch, persist: rememberCheats })
      setCheatConfig(next)
      if (next.master_enabled && next.enabled_abilities.length > 0) {
        setNote(`作弊已生效：${next.enabled_abilities.join(' / ')}`)
      } else {
        setNote('作弊已关闭；game_cheat 会立即从她的工具里消失')
      }
      await refreshStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCheatBusy(false)
    }
  }

  function chooseProfile(profile: GameProfile) {
    const meta = PROFILE_META.find((item) => item.id === profile)
    if (!meta) return
    const abilities: Record<string, boolean> = {}
    for (const item of ABILITY_META) abilities[item.id] = Boolean(meta.preset[item.id])
    void applyCheat({
      profile,
      master_enabled: profile !== 'survival',
      abilities,
    })
  }

  function toggleAbility(id: string, enabled: boolean) {
    if (!cheatConfig) return
    void applyCheat({
      abilities: { ...cheatConfig.abilities, [id]: enabled },
    })
  }

  function toggleCheatMaster(enabled: boolean) {
    if (!cheatConfig) return
    if (!enabled) {
      void applyCheat({ master_enabled: false })
      return
    }
    const enabledAny = Object.values(cheatConfig.abilities).some(Boolean)
    if (enabledAny) {
      void applyCheat({ master_enabled: true })
      return
    }
    const meta = PROFILE_META.find((item) => item.id === 'assisted')!
    const abilities: Record<string, boolean> = {}
    for (const item of ABILITY_META) abilities[item.id] = Boolean(meta.preset[item.id])
    void applyCheat({ profile: 'assisted', master_enabled: true, abilities })
  }

  async function toggleSkill(skill: GameSkill) {
    setSkillBusy(skill.id)
    setSkillNote(null)
    try {
      await api.setGameSkillEnabled(skill.id, friend.id, !skill.enabled)
      await loadSkills()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSkillBusy(null)
    }
  }

  async function deleteSkill(skill: GameSkill) {
    if (skill.source !== 'learned') return
    if (!window.confirm(`删掉技能「${skill.description || skill.name}」？`)) return
    setSkillBusy(skill.id)
    setSkillNote(null)
    try {
      await api.deleteGameSkill(skill.id, friend.id)
      await loadSkills()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSkillBusy(null)
    }
  }

  async function runSkill(skill: GameSkill) {
    if (!mine) {
      setError('先让她进游戏，才能试跑技能。')
      return
    }
    setSkillBusy(skill.id)
    setSkillNote(null)
    setError(null)
    try {
      const result = await api.runGameSkill(skill.id, friend.id)
      setSkillNote(
        result.ok
          ? `试跑成功：${skill.name}`
          : `试跑失败（${result.reason || 'error'}）：${result.error}`,
      )
      await loadSkills()
      await refreshStatus()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSkillBusy(null)
    }
  }

  async function buildBlueprint(blueprint: GameBlueprint) {
    if (!mine) {
      setError('先让她进游戏，才能开始建造。')
      return
    }
    setBlueprintBusy(blueprint.id)
    setBlueprintNote(null)
    setError(null)
    try {
      const result = await api.buildGameBlueprint(blueprint.id, friend.id)
      const gap = result.gap as Record<string, number> | undefined
      if (gap && Object.keys(gap).length > 0) {
        setBlueprintNote(
          `已排好计划：先补材料（${Object.entries(gap)
            .map(([name, count]) => `${name}×${count}`)
            .join('、')}），再按蓝图建造。`,
        )
      } else {
        setBlueprintNote(`已开始建造：${blueprint.name}`)
      }
      await refreshStatus()
      await loadBlueprints()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBlueprintBusy(null)
    }
  }

  async function rateCreative(record: GameCreativeRecord, rating: number) {
    setBlueprintBusy(record.id)
    try {
      await api.rateCreative(record.id, friend.id, rating)
      await loadBlueprints()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBlueprintBusy(null)
    }
  }

  function switchTab(next: TabId) {
    setTab(next)
    setError(null)
    setNote(null)
  }

  const currentTab = TABS.find((item) => item.id === tab) ?? TABS[0]
  const profile = cheatConfig?.profile ?? (status?.profile as GameProfile | undefined) ?? 'survival'
  const profileLabel =
    cheatConfig?.profile_labels?.[profile] ??
    PROFILE_META.find((item) => item.id === profile)?.label ??
    '生存'
  const cheatActive =
    cheatConfig?.active ?? Boolean(status?.cheat_enabled)
  const gameFriendName =
    other && status?.friend_id
      ? status.friend_id === friend.id
        ? friend.name
        : '另一个朋友'
      : friend.name

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-page text-ink">
      <header className="relative z-20 flex h-[58px] shrink-0 items-center gap-3 border-b border-line/70 bg-surface/72 px-4 backdrop-blur-xl">
        <span
          className="absolute inset-x-0 bottom-0 h-px opacity-70"
          style={{ background: 'linear-gradient(90deg, transparent, var(--color-accent), transparent)' }}
          aria-hidden
        />
        <Avatar name={friend.name} url={friend.avatar_url} size={34} />
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-[14px] font-semibold text-ink">{friend.name}</h1>
            <Pill tone="accent">游戏模式</Pill>
          </div>
          <p className="mt-0.5 text-[10.5px] text-faint">
            {mine
              ? `${status?.state ?? 'running'} · ${MODE_LABELS[status?.mode ?? ''] ?? status?.mode ?? '—'}`
              : other
                ? '现在是她另一位朋友在对局里'
                : '还没进游戏'}
          </p>
        </div>

        <div className="ml-3 hidden flex-wrap items-center gap-1.5 lg:flex">
          <Pill tone={mine ? 'ok' : other ? 'warn' : 'neutral'}>
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                mine ? 'bg-emerald-300' : other ? 'bg-amber-300' : 'bg-faint'
              }`}
              aria-hidden
            />
            {mine ? '已连接' : other ? `${gameFriendName} 在玩` : '未连接'}
          </Pill>
          <Pill>Minecraft · {snapshot?.version ?? '版本待探测'}</Pill>
          <Pill
            tone={cheatActive ? 'warn' : 'accent'}
            title={cheatActive ? '作弊已由你手动开启' : '当前难度 profile'}
          >
            profile · {profileLabel}
            {cheatActive ? ' · 作弊开' : ''}
          </Pill>
        </div>

        <div className="ml-auto flex items-center gap-3">
          <div className="hidden text-right sm:block">
            <p className="text-[10px] text-faint">本局时长</p>
            <p className="font-mono text-[12px] text-ink-soft">
              {elapsed == null ? '—' : formatDuration(elapsed)}
            </p>
          </div>
          <div className="hidden text-right md:block">
            <p className="text-[10px] text-faint">本局费用</p>
            <p className="text-[12px] text-ink-soft">—</p>
          </div>
          <button
            type="button"
            onClick={() => setTab('overview')}
            className="flex items-center gap-1.5 rounded-xl border border-line-strong px-2.5 py-1.5 text-[11.5px] text-muted transition-colors hover:border-accent/60 hover:text-accent"
            title="回到游戏管理主页面"
          >
            <IconStack size={14} />
            游戏管理
          </button>
          <button
            type="button"
            onClick={onExit}
            className="flex items-center gap-1.5 rounded-xl border border-line-strong px-2.5 py-1.5 text-[11.5px] text-muted transition-colors hover:border-accent/60 hover:text-accent"
            title="退出游戏模式（不会停游戏）"
          >
            <IconLogout size={14} />
            退出
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[196px] shrink-0 flex-col border-r border-line/70 bg-surface/38">
          <div className="flex-1 overflow-y-auto p-2">
            {TABS.map(({ id, label, hint, Icon }) => {
              const active = tab === id
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => switchTab(id)}
                  className={`group relative mb-0.5 flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left transition-all duration-200 ${
                    active
                      ? 'bg-surface/90 text-ink shadow-[0_3px_16px_rgba(0,0,0,0.12)]'
                      : 'text-muted hover:bg-surface/60 hover:text-ink'
                  }`}
                >
                  {active && (
                    <span
                      className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-r"
                      style={{ background: ACCENT }}
                      aria-hidden
                    />
                  )}
                  <Icon
                    size={15}
                    className={active ? 'text-accent' : 'text-faint group-hover:text-muted'}
                  />
                  <span className="min-w-0">
                    <span className="block text-[12px] font-medium">{label}</span>
                    <span className="mt-0.5 block truncate text-[9.5px] text-faint">{hint}</span>
                  </span>
                  {id === 'overview' && mine && (
                    <span className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-300" />
                  )}
                </button>
              )
            })}
          </div>
          <div className="border-t border-line/60 px-3 py-2.5">
            <p className="text-[9.5px] leading-relaxed text-faint">
              这是游戏后台，不是游戏画面。退出这里，她仍会继续玩。
            </p>
          </div>
        </aside>

        <main className="relative min-w-0 flex-1 overflow-y-auto">
          <div
            className="pointer-events-none absolute inset-x-0 top-0 h-56 opacity-70"
            style={{
              background:
                'radial-gradient(70% 100% at 18% 0%, rgba(212,134,79,0.14), transparent 70%)',
            }}
            aria-hidden
          />
          <div className="relative mx-auto max-w-[1220px] px-4 py-4">
            <div className="mb-3 flex flex-wrap items-end gap-2">
              <div>
                <h2 className="text-[18px] font-semibold tracking-[-0.02em] text-ink">
                  {currentTab.label}
                </h2>
                <p className="mt-0.5 text-[11px] text-faint">{currentTab.hint}</p>
              </div>
              {other && (
                <Pill tone="warn" title="同一时间只允许一局">
                  另一局正在进行 · 停掉后才能开新局
                </Pill>
              )}
              {note && <Pill tone="accent">{note}</Pill>}
            </div>

            {error && (
              <div className="mb-3 rounded-xl border border-red-400/25 bg-red-500/10 px-3 py-2 text-[11.5px] text-red-200">
                出错了：{error}
              </div>
            )}

            {tab === 'overview' && (
              <div className="grid gap-3 xl:grid-cols-2">
                <Section
                  title="这一局"
                  hint={mine ? '连接、身体状态、当前常态模式' : '还没有进行中的对局'}
                  action={
                    mine ? (
                      <button
                        type="button"
                        onClick={() => void stop()}
                        disabled={busy}
                        className="flex items-center gap-1 rounded-lg border border-line-strong px-2 py-1 text-[10.5px] text-muted hover:border-red-400/50 hover:text-red-300 disabled:opacity-40"
                      >
                        <IconStop size={12} />
                        停局
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setTab('match')}
                        className="flex items-center gap-1 rounded-lg border border-accent/40 bg-accent/10 px-2 py-1 text-[10.5px] text-accent"
                      >
                        <IconPlay size={12} />
                        去进服
                      </button>
                    )
                  }
                >
                  {mine ? (
                    <div className="space-y-2.5">
                      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                        <Metric label="血量" value={snapshot?.player?.health ?? '—'} />
                        <Metric label="饥饿" value={snapshot?.player?.food ?? '—'} />
                        <Metric label="X" value={snapshot?.player?.x?.toFixed?.(1) ?? '—'} />
                        <Metric label="Y / Z" value={
                          snapshot?.player
                            ? `${snapshot.player.y ?? '—'} / ${snapshot.player.z ?? '—'}`
                            : '—'
                        } />
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        <Pill tone="ok">{status?.state ?? 'running'}</Pill>
                        <Pill>
                          {MODE_LABELS[status?.mode ?? ''] ?? status?.mode ?? '—'}
                          {MODE_HINTS[status?.mode ?? '']
                            ? ` · ${MODE_HINTS[status?.mode ?? '']}`
                            : ''}
                        </Pill>
                        {snapshot?.following && <Pill>跟着 {snapshot.following}</Pill>}
                        {runningTasks.length > 0 && (
                          <Pill tone="accent">正在做 {runningTasks.length} 件事</Pill>
                        )}
                      </div>
                      {runningTasks.length > 0 && (
                        <div className="space-y-1">
                          {runningTasks.map((task) => {
                            const progress =
                              typeof task.progress?.current === 'number' &&
                              typeof task.progress?.target === 'number' &&
                              task.progress.target > 0
                                ? (task.progress.current / task.progress.target) * 100
                                : null
                            return (
                              <div
                                key={task.task_id}
                                className="rounded-xl border border-line/60 bg-page/30 px-2.5 py-1.5"
                              >
                                <div className="mb-1 flex items-center justify-between gap-2 text-[10.5px] text-muted">
                                  <span className="truncate">
                                    {task.action ?? task.task_id}
                                    {task.progress?.current != null && task.progress?.target != null
                                      ? ` · ${task.progress.current}/${task.progress.target}`
                                      : ''}
                                  </span>
                                  <span className="shrink-0 text-faint">running</span>
                                </div>
                                {progress != null && (
                                  <ProgressRail label="" value={progress} />
                                )}
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </div>
                  ) : (
                    <EmptyHint>
                      {other
                        ? '现在是另一个朋友在玩。要开新一局，先去「对局」把那一局停掉。'
                        : '还没进服。去「对局」填地址和端口，然后让她进来。'}
                    </EmptyHint>
                  )}
                </Section>

                <Section title="她现在的计划" hint="plan 是目标；task 是一个个具体动作">
                  {currentPlan ? (
                    <div className="space-y-2.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Pill tone={currentPlan.status === 'executing' ? 'ok' : 'warn'}>
                          {PLAN_STATUS_LABELS[currentPlan.status] ?? currentPlan.status}
                        </Pill>
                        <Pill>{currentPlan.id}</Pill>
                      </div>
                      <p className="rounded-xl border border-line/60 bg-page/30 px-2.5 py-2 text-[11.5px] leading-relaxed text-ink-soft">
                        {String(currentPlan.expected || currentPlan.condition || '—')}
                      </p>
                      {Boolean(currentPlan.condition) && (
                        <p className="text-[10.5px] leading-relaxed text-faint">
                          前置：{String(currentPlan.condition)}
                        </p>
                      )}
                      <ol className="space-y-1.5">
                        {currentPlan.tasks.map((task) => (
                          <li
                            key={`${currentPlan.id}-${task.index}`}
                            className="flex items-start gap-2 rounded-xl border border-line/50 bg-page/25 px-2.5 py-1.5 text-[11px] text-muted"
                          >
                            <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-chip/80 text-[9.5px] text-faint">
                              {task.index}
                            </span>
                            <span className="min-w-0 flex-1 leading-relaxed">
                              <span className="text-ink-soft">{task.action}</span>
                              {Object.keys(task.args ?? {}).length > 0 && (
                                <span className="ml-1 text-faint">
                                  {JSON.stringify(task.args)}
                                </span>
                              )}
                            </span>
                            <span
                              className={
                                task.status === 'success'
                                  ? 'shrink-0 text-emerald-300'
                                  : task.status === 'failed'
                                    ? 'shrink-0 text-red-300'
                                    : task.status === 'executing'
                                      ? 'shrink-0 text-accent'
                                      : 'shrink-0 text-faint'
                              }
                            >
                              {TASK_STATUS_LABELS[task.status] ?? task.status}
                            </span>
                          </li>
                        ))}
                      </ol>
                    </div>
                  ) : status?.plan || (status?.plan_steps?.length ?? 0) > 0 ? (
                    <div className="space-y-2.5">
                      {status?.plan && (
                        <p className="rounded-xl border border-line/60 bg-page/30 px-2.5 py-2 text-[11.5px] leading-relaxed text-ink-soft">
                          {status.plan}
                        </p>
                      )}
                      {(status?.plan_steps ?? []).length > 0 ? (
                        <ol className="space-y-1.5">
                          {status?.plan_steps?.map((step, index) => (
                            <li key={`${index}-${step}`} className="flex gap-2 text-[11.5px] text-muted">
                              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-chip/80 text-[9.5px] text-faint">
                                {index + 1}
                              </span>
                              <span className="leading-relaxed">{step}</span>
                            </li>
                          ))}
                        </ol>
                      ) : null}
                    </div>
                  ) : (
                    <EmptyHint>
                      {mine ? '她还没把计划写下来。自由模式里她会先定一个 2~5 步的小计划。' : '进游戏后，她的计划会出现在这里。'}
                    </EmptyHint>
                  )}
                </Section>

                <Section title="快脑" hint="简单判断先走本地；接不住才升级聊天模型">
                  {status?.brain ? (
                    <div className="space-y-2">
                      <div className="flex flex-wrap gap-1.5">
                        <Pill tone={status.brain.lane === 'local' ? 'ok' : 'warn'}>
                          {status.brain.lane === 'local'
                            ? `本地 ${status.brain.model ?? ''}`
                            : status.brain.lane === 'local->deepseek'
                              ? '本地没接住 → DeepSeek'
                              : status.brain.lane ?? '未知'}
                        </Pill>
                        {status.brain.action && <Pill>{status.brain.action}</Pill>}
                        {typeof status.brain.latency_ms === 'number' && (
                          <Pill>{status.brain.latency_ms} ms</Pill>
                        )}
                      </div>
                      <p className="text-[10.5px] leading-relaxed text-faint">
                        今日真实用量（全部对话 / 游戏）：
                        {usage
                          ? `${usage.totals.calls.toLocaleString('zh-CN')} 次请求 · 输入 ${usage.totals.input_tokens.toLocaleString('zh-CN')} · 输出 ${usage.totals.output_tokens.toLocaleString('zh-CN')} · 推理 ${usage.totals.reasoning_tokens.toLocaleString('zh-CN')}`
                          : '还没有用量记录'}
                      </p>
                    </div>
                  ) : (
                    <EmptyHint>还没有快脑决策。她在自由模式里需要选下一步时才会用到。</EmptyHint>
                  )}
                </Section>

                <Section title="最近发生" hint="事件流只保留最近的，完整日志去「日志」页">
                  <EventList events={events.slice(-8).reverse()} />
                </Section>
              </div>
            )}

            {tab === 'match' && (
              <div className="grid gap-3 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]">
                <Section
                  title={mine ? '当前对局' : other ? '另一局正在进行' : '让她进服'}
                  hint="Java 版 / 离线模式；先在游戏里按 Esc →「对局域网开放」"
                  action={
                    mine ? (
                      <Pill tone="ok">已连接</Pill>
                    ) : other ? (
                      <Pill tone="warn">占用中</Pill>
                    ) : (
                      <Pill>未连接</Pill>
                    )
                  }
                >
                  {other ? (
                    <EmptyHint>
                      现在是另一个朋友在玩。同一时间只允许一局；先去把她那一局停掉。
                    </EmptyHint>
                  ) : mine ? (
                    <div className="space-y-3">
                      <div className="grid gap-2 sm:grid-cols-2">
                        <Metric label="会话" value={status?.state ?? 'running'} />
                        <Metric
                          label="适配器"
                          value={status?.adapter_alive ? '进程活着' : '进程不在'}
                        />
                        <Metric label="世界版本" value={snapshot?.version ?? '探测中'} />
                        <Metric
                          label="玩家的游戏名"
                          value={snapshot?.player?.name ?? connection.username}
                        />
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <button
                          type="button"
                          onClick={() => setTab('overview')}
                          className="rounded-xl border border-line-strong px-3 py-1.5 text-[11.5px] text-muted hover:border-accent/60 hover:text-accent"
                        >
                          回总览看状态
                        </button>
                        <button
                          type="button"
                          onClick={() => void stop()}
                          disabled={busy}
                          className="flex items-center gap-1 rounded-xl border border-red-400/30 bg-red-500/10 px-3 py-1.5 text-[11.5px] text-red-200 hover:border-red-400/60 disabled:opacity-40"
                        >
                          <IconStop size={13} />
                          {busy ? '停局中…' : '停掉这一局'}
                        </button>
                      </div>
                      <p className="text-[10.5px] leading-relaxed text-faint">
                        退出全屏游戏模式不会停局；只有这里的「停掉这一局」会真的结束。
                      </p>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      <label className="block">
                        <span className="mb-1 block text-[10.5px] text-muted">地址</span>
                        <input
                          value={connection.host}
                          onChange={(e) =>
                            setConnection((prev) => ({ ...prev, host: e.target.value }))
                          }
                          className="w-full rounded-xl border border-line bg-page/40 px-3 py-2 text-[12.5px] text-ink outline-none focus:border-accent/60"
                        />
                      </label>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <label className="block">
                          <span className="mb-1 block text-[10.5px] text-muted">端口</span>
                          <input
                            value={connection.port}
                            onChange={(e) =>
                              setConnection((prev) => ({ ...prev, port: e.target.value }))
                            }
                            inputMode="numeric"
                            className="w-full rounded-xl border border-line bg-page/40 px-3 py-2 text-[12.5px] text-ink outline-none focus:border-accent/60"
                          />
                        </label>
                        <label className="block">
                          <span className="mb-1 block text-[10.5px] text-muted">
                            她的游戏名（英文 / 数字 / 下划线）
                          </span>
                          <input
                            value={connection.username}
                            onChange={(e) =>
                              setConnection((prev) => ({ ...prev, username: e.target.value }))
                            }
                            className="w-full rounded-xl border border-line bg-page/40 px-3 py-2 text-[12.5px] text-ink outline-none focus:border-accent/60"
                          />
                        </label>
                      </div>
                      <button
                        type="button"
                        onClick={() => void start()}
                        disabled={busy}
                        className="flex w-full items-center justify-center gap-2 rounded-xl py-2.5 text-[13px] font-medium text-white disabled:opacity-40"
                        style={{ background: ACCENT }}
                      >
                        <IconPlay size={15} />
                        {busy ? '进服中…' : '让她进来'}
                      </button>
                    </div>
                  )}
                </Section>

                <Section title="连接说明" hint="出问题时先看这几条">
                  <ul className="space-y-2 text-[11px] leading-relaxed text-muted">
                    <li>· Minecraft 必须是 Java 版，服务器要允许离线模式玩家。</li>
                    <li>· 单机世界要先对局域网开放；端口填游戏里显示的那个。</li>
                    <li>· 同一台电脑通常填 <code className="text-ink-soft">127.0.0.1</code>。</li>
                    <li>· 她进服后按 Esc 不会暂停世界；这是正常现象。</li>
                    <li>· 如果她卡在低处，先让她试试快捷指令「自己玩」再「过来」。</li>
                  </ul>
                </Section>
              </div>
            )}

            {tab === 'settings' && (
              <GameSettingsPanel
                key={settings?.updated_at ?? 'loading'}
                settings={settings}
                onChanged={onSettingsChanged}
              />
            )}

            {tab === 'cheat' && (
              <div className="grid gap-3 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
                <Section
                  title="难度 profile"
                  hint="profile 只是预设，切完还能逐项改；只有你能切换"
                  action={<Pill tone="accent">profile · {profileLabel}</Pill>}
                >
                  <div className="grid grid-cols-2 gap-2">
                    {PROFILE_META.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        disabled={cheatBusy}
                        onClick={() => chooseProfile(item.id)}
                        className={`rounded-xl border px-2.5 py-2 text-left transition-colors disabled:opacity-40 ${
                          profile === item.id
                            ? 'border-accent/50 bg-accent/10'
                            : 'border-line/60 bg-page/20 hover:border-accent/35 hover:bg-page/35'
                        }`}
                      >
                        <div className="flex items-center gap-1.5">
                          <span className="text-[11.5px] text-ink">{item.label}</span>
                          {profile === item.id && <Pill>当前</Pill>}
                        </div>
                        <p className="mt-1 text-[9.5px] leading-relaxed text-faint">
                          {item.hint}
                        </p>
                      </button>
                    ))}
                  </div>
                  <label className="mt-3 flex items-start gap-2 rounded-xl border border-line/60 bg-page/20 px-2.5 py-2 text-[11px] text-muted">
                    <input
                      type="checkbox"
                      className="mt-[2px] accent-[var(--color-accent)]"
                      checked={rememberCheats}
                      onChange={(e) => setRememberCheats(e.target.checked)}
                    />
                    <span>
                      记住这份配置
                      <span className="mt-0.5 block text-[10px] leading-relaxed text-faint">
                        勾上 = 下次开局继续生效；不勾 = 只影响当前这一局。
                      </span>
                    </span>
                  </label>
                </Section>

                <Section
                  title="作弊能力"
                  hint="总开关默认关；LLM 不能自己打开。关掉后 game_cheat 立即从工具里消失。"
                  action={
                    <Pill tone={cheatActive ? 'warn' : 'neutral'}>
                      {cheatActive ? '工具已出现' : '全关'}
                    </Pill>
                  }
                >
                  <label className="flex items-center justify-between gap-3 rounded-xl border border-line/70 bg-page/25 px-3 py-2">
                    <span>
                      <span className="block text-[12px] font-medium text-ink">总开关</span>
                      <span className="mt-0.5 block text-[10px] text-faint">
                        只有你手动打开，她才会看到 `game_cheat`
                      </span>
                    </span>
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-[var(--color-accent)]"
                      checked={Boolean(cheatConfig?.master_enabled)}
                      disabled={cheatBusy || !cheatConfig}
                      onChange={(e) => toggleCheatMaster(e.target.checked)}
                    />
                  </label>

                  <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                    {ABILITY_META.map((item) => {
                      const enabled = Boolean(cheatConfig?.abilities?.[item.id])
                      return (
                        <label
                          key={item.id}
                          className={`flex cursor-pointer items-start gap-2 rounded-xl border px-2.5 py-2 transition-colors ${
                            enabled
                              ? 'border-accent/35 bg-accent/10'
                              : 'border-line/50 bg-page/20 hover:border-line-strong'
                          }`}
                        >
                          <input
                            type="checkbox"
                            className="mt-[2px] accent-[var(--color-accent)]"
                            checked={enabled}
                            disabled={cheatBusy || !cheatConfig}
                            onChange={(e) => toggleAbility(item.id, e.target.checked)}
                          />
                          <span className="min-w-0">
                            <span className="flex items-center gap-1.5 text-[11px] text-ink-soft">
                              {item.label}
                              {item.risky && (
                                <span className="rounded-full border border-amber-400/25 bg-amber-400/10 px-1.5 text-[9px] text-amber-200">
                                  高影响
                                </span>
                              )}
                            </span>
                            <span className="mt-0.5 block text-[9.5px] leading-relaxed text-faint">
                              {item.hint}
                            </span>
                          </span>
                        </label>
                      )
                    })}
                  </div>
                  <p className="mt-2.5 rounded-xl border border-amber-400/20 bg-amber-400/5 px-2.5 py-2 text-[10.5px] leading-relaxed text-amber-200/80">
                    作弊会绕过正常生存过程。所有调用都会写审计事件（source=cheat），数据页能查；
                    服务器还必须允许这个玩家执行对应命令（单人世界要开作弊 / OP）。
                  </p>
                </Section>
              </div>
            )}

            {tab === 'skills' && (
              <div className="grid gap-3 xl:grid-cols-[minmax(0,1.25fr)_minmax(0,0.75fr)]">
                <Section
                  title="技能书"
                  hint="内置技能全局；学会的技能只属于她；只有通过 checks 的 plan 才会入库"
                  action={
                    <Pill tone={skills.length > 0 ? 'ok' : 'neutral'}>
                      {skills.length} 个
                    </Pill>
                  }
                >
                  {skillNote && (
                    <div className="mb-2 rounded-lg border border-accent/25 bg-accent/5 px-2.5 py-1.5 text-[10.5px] text-accent">
                      {skillNote}
                    </div>
                  )}
                  {skills.length === 0 ? (
                    <EmptyHint>
                      还没有技能。她完成一个带 checks 且全部通过的 plan 后，会自动学会。
                    </EmptyHint>
                  ) : (
                    <div className="space-y-2">
                      {skills.map((skill) => {
                        const stats = skill.stats || {}
                        const busy = skillBusy === skill.id
                        return (
                          <div
                            key={skill.id}
                            className={`rounded-xl border px-3 py-2.5 ${
                              skill.enabled
                                ? 'border-line/70 bg-page/25'
                                : 'border-line/40 bg-page/10 opacity-60'
                            }`}
                          >
                            <div className="flex flex-wrap items-start gap-2">
                              <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  <IconSpark size={13} className="text-accent" />
                                  <span className="text-[12px] font-medium text-ink">
                                    {skill.description || skill.name}
                                  </span>
                                  <Pill tone={skill.source === 'builtin' ? 'neutral' : 'accent'}>
                                    {skill.source === 'builtin' ? '内置 · 全局' : '她学会的'}
                                  </Pill>
                                  {!skill.enabled && <Pill>已禁用</Pill>}
                                </div>
                                <p className="mt-1 text-[10.5px] leading-relaxed text-muted">
                                  {skill.name} · {skill.steps.length} 步 ·{' '}
                                  {skill.postconditions.length} 条后置条件
                                </p>
                                <p className="mt-0.5 text-[9.5px] text-faint">
                                  运行 {Number(stats.runs || 0)} · 成功{' '}
                                  {Number(stats.successes || 0)} · 失败{' '}
                                  {Number(stats.failures || 0)}
                                </p>
                              </div>
                              <div className="flex shrink-0 flex-wrap items-center gap-1">
                                <button
                                  type="button"
                                  onClick={() => void runSkill(skill)}
                                  disabled={busy || !skill.enabled || !mine}
                                  className="rounded-lg border border-accent/35 bg-accent/10 px-2 py-1 text-[10px] text-accent hover:border-accent/60 disabled:opacity-35"
                                >
                                  {busy ? '跑…' : '试跑'}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => void toggleSkill(skill)}
                                  disabled={busy}
                                  className="rounded-lg border border-line/70 px-2 py-1 text-[10px] text-muted hover:border-accent/50 hover:text-accent disabled:opacity-35"
                                >
                                  {skill.enabled ? '禁用' : '启用'}
                                </button>
                                {skill.source !== 'builtin' && (
                                  <button
                                    type="button"
                                    onClick={() => void deleteSkill(skill)}
                                    disabled={busy}
                                    className="rounded-lg border border-line/70 px-2 py-1 text-[10px] text-faint hover:border-red-400/50 hover:text-red-300 disabled:opacity-35"
                                  >
                                    删除
                                  </button>
                                )}
                              </div>
                            </div>
                            {skill.postconditions.length > 0 && (
                              <p className="mt-1.5 truncate font-mono text-[9px] text-faint">
                                验收：
                                {skill.postconditions
                                  .map((item) => {
                                    if (item && typeof item === 'object') {
                                      const record = item as Record<string, unknown>
                                      return String(record.type || 'check')
                                    }
                                    return String(item)
                                  })
                                  .join(' + ')}
                              </p>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </Section>

                <div className="space-y-3">
                  <Section
                    title="失败笔记"
                    hint="失败复盘单独留档；不会混进技能检索"
                    action={<Pill>{skillFailures.length}</Pill>}
                  >
                    {skillFailures.length === 0 ? (
                      <EmptyHint>还没有失败笔记。</EmptyHint>
                    ) : (
                      <div className="max-h-64 space-y-1.5 overflow-y-auto">
                        {skillFailures.slice(0, 8).map((item) => (
                          <div
                            key={item.id}
                            className="rounded-lg border border-line/50 bg-page/20 px-2.5 py-1.5"
                          >
                            <div className="flex items-center gap-1.5">
                              <Pill tone="warn">{item.reason}</Pill>
                              {item.plan_id && (
                                <span className="truncate font-mono text-[9px] text-faint">
                                  {item.plan_id}
                                </span>
                              )}
                            </div>
                            <p className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-muted">
                              {item.detail || '没有细节'}
                            </p>
                          </div>
                        ))}
                      </div>
                    )}
                  </Section>
                  <Section title="检索规则" hint="控制开销，不把整本技能书塞进提示词">
                    <ul className="space-y-2 text-[11px] leading-relaxed text-muted">
                      <li>· 只按当前目标检索 top-k（≤5）。</li>
                      <li>· 关键词优先；向量模型已加载时再叠语义分。</li>
                      <li>· 内置技能全局；学会的技能按朋友隔离。</li>
                      <li>· `game_skill` 不调 LLM，按步骤跑完并做 checks。</li>
                      <li>· 试跑需要她正在游戏里；失败会写进失败笔记。</li>
                    </ul>
                  </Section>
                </div>
              </div>
            )}

            {tab === 'blueprints' && (
              <div className="grid gap-3 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
                <Section
                  title="蓝图建造"
                  hint="可验证：逐层放块，再用真实 block_at 校验；生存模式先补材料"
                  action={<Pill>{blueprints.length} 个设计</Pill>}
                >
                  {blueprintNote && (
                    <div className="mb-2 rounded-lg border border-accent/25 bg-accent/5 px-2.5 py-1.5 text-[10.5px] text-accent">
                      {blueprintNote}
                    </div>
                  )}
                  {status?.blueprint_job && (
                    <div className="mb-2 rounded-xl border border-line/60 bg-page/25 px-2.5 py-2">
                      <div className="flex items-center justify-between gap-2 text-[10.5px]">
                        <span className="text-ink-soft">
                          正在建：{String(status.blueprint_job.name ?? '蓝图')}
                        </span>
                        <Pill tone={status.blueprint_job.state === 'failed' ? 'warn' : 'accent'}>
                          {String(status.blueprint_job.state ?? 'running')}
                        </Pill>
                      </div>
                      <div className="mt-1 flex gap-2 text-[9.5px] text-faint">
                        <span>放了 {Number(status.blueprint_job.placed || 0)}</span>
                        <span>查了 {Number(status.blueprint_job.checked || 0)}</span>
                        <span>失败 {Number(status.blueprint_job.failures || 0)}</span>
                      </div>
                      <div className="mt-1.5">
                        <ProgressRail
                          label=""
                          value={
                            Number(status.blueprint_job.layers || 0) > 0
                              ? (Number(status.blueprint_job.layer || 0) /
                                  Number(status.blueprint_job.layers || 1)) *
                                100
                              : null
                          }
                        />
                      </div>
                    </div>
                  )}
                  {blueprints.length === 0 ? (
                    <EmptyHint>还没有蓝图设计。让她先用 `game_build(action="design")` 存一份。</EmptyHint>
                  ) : (
                    <div className="space-y-2">
                      {blueprints.map((blueprint) => {
                        const raw = blueprint.blueprint || {}
                        const requirements =
                          (raw.requirements as Record<string, number> | undefined) || {}
                        return (
                          <div
                            key={blueprint.id}
                            className="rounded-xl border border-line/60 bg-page/25 px-3 py-2.5"
                          >
                            <div className="flex flex-wrap items-start gap-2">
                              <div className="min-w-0 flex-1">
                                <div className="flex flex-wrap items-center gap-1.5">
                                  <IconBlueprint size={13} className="text-accent" />
                                  <span className="text-[12px] font-medium text-ink">
                                    {blueprint.name}
                                  </span>
                                  <Pill tone={blueprint.source === 'builtin' ? 'neutral' : 'accent'}>
                                    {blueprint.source === 'builtin' ? '内置模板' : '她的设计'}
                                  </Pill>
                                </div>
                                <p className="mt-1 text-[10.5px] leading-relaxed text-muted">
                                  {blueprint.description}
                                </p>
                                <p className="mt-1 text-[9.5px] text-faint">
                                  材料：
                                  {Object.entries(requirements)
                                    .map(([name, count]) => `${name}×${count}`)
                                    .join('、') || '—'}
                                </p>
                              </div>
                              <button
                                type="button"
                                onClick={() => void buildBlueprint(blueprint)}
                                disabled={blueprintBusy === blueprint.id || !mine}
                                className="shrink-0 rounded-lg border border-accent/35 bg-accent/10 px-2.5 py-1.5 text-[10.5px] text-accent hover:border-accent/60 disabled:opacity-35"
                              >
                                {blueprintBusy === blueprint.id ? '排计划…' : '按蓝图建造'}
                              </button>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </Section>

                <div className="space-y-3">
                  <Section
                    title="自由创作"
                    hint="不判失败；只记录设计、前后 diff 和你的评分"
                    action={<Pill>{creativeRecords.length}</Pill>}
                  >
                    {creativeRecords.length === 0 ? (
                      <EmptyHint>
                        让先用 `game_build(action="creative_start", brief?, design?, size?)`
                        记下设计，完成后用 `creative_finish` 收尾。不能直接乱挖乱放。
                      </EmptyHint>
                    ) : (
                      <div className="max-h-72 space-y-1.5 overflow-y-auto">
                        {creativeRecords.slice(0, 10).map((record) => (
                          <div
                            key={record.id}
                            className="rounded-xl border border-line/60 bg-page/25 px-2.5 py-2"
                          >
                            <div className="flex items-center gap-1.5">
                              <span className="min-w-0 flex-1 truncate text-[11px] text-ink-soft">
                                {record.brief}
                              </span>
                              <Pill>{record.state}</Pill>
                            </div>
                            <p className="mt-1 text-[9.5px] text-faint">
                              改动了 {(record.diff || []).length} 格
                              {record.rating ? ` · 你的评分 ${record.rating}/5` : ' · 还没评分'}
                            </p>
                            <div className="mt-1 flex items-center gap-1">
                              {[1, 2, 3, 4, 5].map((rating) => (
                                <button
                                  key={rating}
                                  type="button"
                                  onClick={() => void rateCreative(record, rating)}
                                  disabled={blueprintBusy === record.id}
                                  className={`flex h-5 w-5 items-center justify-center rounded-md border text-[10px] ${
                                    (record.rating || 0) >= rating
                                      ? 'border-accent/50 bg-accent/15 text-accent'
                                      : 'border-line/60 text-faint hover:border-accent/40'
                                  }`}
                                  title={`${rating} 分`}
                                >
                                  {rating}
                                </button>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </Section>
                  <Section title="创作规则" hint="陪玩，不是 benchmark">
                    <ul className="space-y-2 text-[11px] leading-relaxed text-muted">
                      <li>· 创作前必须有草图 / 调色板 / 结构描述 / 蓝图之一。</li>
                      <li>· 蓝图不通过 block_at 校验就算失败，会进复盘。</li>
                      <li>· 自由创作不出现“任务失败”，只看 diff 和你的口味。</li>
                      <li>· 生存模式先补材料；作弊开启 give 才允许直接给料。</li>
                    </ul>
                  </Section>
                </div>
              </div>
            )}

            {tab === 'data' && (
              <div className="grid gap-3 xl:grid-cols-2">
                <Section title="背包与手持" hint="她实际拥有的东西">
                  {snapshot?.inventory && snapshot.inventory.length > 0 ? (
                    <div className="space-y-2">
                      {snapshot.inventory_empty_slots != null && (
                        <p className="text-[10.5px] text-faint">
                          背包空位：{snapshot.inventory_empty_slots}
                          {snapshot.inventory_full ? '（满了）' : ''}
                        </p>
                      )}
                      <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
                        {(snapshot.inventory_by_item ?? snapshot.inventory).map((item, index) => (
                          <div
                            key={`${item.item}-${index}`}
                            className="flex items-center justify-between rounded-lg border border-line/60 bg-page/25 px-2 py-1.5"
                          >
                            <span className="truncate font-mono text-[10.5px] text-ink-soft">
                              {item.item}
                            </span>
                            <span className="ml-2 shrink-0 text-[10.5px] text-faint">
                              ×{item.count}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <EmptyHint>{mine ? '背包快照还是空的。' : '进游戏后显示背包。'}</EmptyHint>
                  )}
                </Section>

                <Section title="附近" hint="她看得见的实体 / 玩家 / 掉落物">
                  {(snapshot?.nearby?.length ?? 0) > 0 ||
                  (snapshot?.dropped_items?.length ?? 0) > 0 ? (
                    <ul className="space-y-1.5">
                      {(snapshot?.nearby ?? []).map((item, index) => (
                        <li
                          key={`${index}-${item.name}`}
                          className="flex items-center justify-between rounded-lg border border-line/60 bg-page/25 px-2 py-1.5 text-[11px]"
                        >
                          <span className="text-ink-soft">{item.name}</span>
                          <span className="text-faint">
                            {item.distance == null ? '—' : `${item.distance} 格`}
                          </span>
                        </li>
                      ))}
                      {(snapshot?.dropped_items ?? []).map((item, index) => (
                        <li
                          key={`drop-${index}-${item.name}`}
                          className="flex items-center justify-between rounded-lg border border-line/60 bg-page/25 px-2 py-1.5 text-[11px]"
                        >
                          <span className="text-ink-soft">
                            掉落 · {item.name} ×{item.count}
                          </span>
                          <span className="text-faint">{item.distance} 格</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <EmptyHint>{mine ? '附近暂时没有可报告的目标。' : '进游戏后显示附近。'}</EmptyHint>
                  )}
                </Section>

                <Section title="任务" hint="同一时间只跑一个真实任务">
                  {Object.keys(status?.tasks ?? {}).length > 0 ||
                  (snapshot?.tasks?.length ?? 0) > 0 ? (
                    <div className="space-y-1.5">
                      {Object.entries(status?.tasks ?? {})
                        .slice(-8)
                        .reverse()
                        .map(([taskId, task]) => (
                          <div
                            key={taskId}
                            className="flex items-center justify-between rounded-lg border border-line/60 bg-page/25 px-2 py-1.5 text-[10.5px]"
                          >
                            <span className="truncate text-ink-soft">
                              {task.action ?? taskId}
                            </span>
                            <span className="ml-2 shrink-0 text-faint">{task.status ?? '—'}</span>
                          </div>
                        ))}
                      {status?.last_task && (
                        <p className="pt-1 text-[10.5px] leading-relaxed text-faint">
                          上一次：
                          {String(status.last_task.action ?? '—')} ·{' '}
                          {String(status.last_task.status ?? '—')}
                        </p>
                      )}
                    </div>
                  ) : (
                    <EmptyHint>现在没有任务。</EmptyHint>
                  )}
                </Section>

                <Section title="事件流" hint="最近 12 条事实">
                  <EventList events={events.slice(-12).reverse()} />
                </Section>

                <Section
                  title="作弊审计"
                  hint="source=cheat；成功和失败都留档"
                  className="xl:col-span-2"
                >
                  {cheatEvents.length > 0 ? (
                    <div className="space-y-1.5">
                      {cheatEvents
                        .slice(-12)
                        .reverse()
                        .map((event) => {
                          const payload = event.payload || {}
                          const commands = Array.isArray(payload.commands)
                            ? payload.commands.join(' ; ')
                            : ''
                          return (
                            <div
                              key={event.id}
                              className="rounded-xl border border-line/60 bg-page/25 px-2.5 py-2 text-[10.5px]"
                            >
                              <div className="flex flex-wrap items-center gap-1.5">
                                <Pill tone={payload.ok ? 'ok' : 'warn'}>
                                  {String(payload.action ?? 'cheat')}
                                </Pill>
                                <Pill>source=cheat</Pill>
                                {!payload.ok && Boolean(payload.error) && (
                                  <span className="text-red-300">{String(payload.error)}</span>
                                )}
                              </div>
                              {commands && (
                                <p className="mt-1 break-all font-mono text-[9.5px] leading-relaxed text-faint">
                                  /{commands}
                                </p>
                              )}
                            </div>
                          )
                        })}
                    </div>
                  ) : (
                    <EmptyHint>还没有作弊记录。</EmptyHint>
                  )}
                </Section>
              </div>
            )}

            {tab === 'memory' && (
              <div className="grid gap-3 xl:grid-cols-2">
                <Section title="本局摘要" hint="一局结束后才会提炼，交给下一局参考">
                  <EmptyHint>
                    摘要会在这一局结束后生成。P1 只提供位置；真正的「本局摘要 / 最近一起玩过」展示随后接入。
                  </EmptyHint>
                </Section>
                <Section title="最近一起玩过" hint="只读她的游戏记忆">
                  <EmptyHint>
                    游戏记忆按朋友完全隔离。这里不会显示其他朋友的任何记忆。
                  </EmptyHint>
                </Section>
              </div>
            )}

            {tab === 'logs' && (
              <div className="h-[calc(100vh-150px)] min-h-[420px] overflow-hidden rounded-2xl border border-line/70">
                <LogWindow embedded />
              </div>
            )}
          </div>
        </main>
      </div>

      <footer className="relative z-20 shrink-0 border-t border-line/70 bg-surface/78 px-4 py-2 backdrop-blur-xl">
        <div className="mx-auto max-w-[1220px]">
          {(replyText || stream.error) && (
            <div className="mb-1.5 flex min-h-5 items-center gap-1.5 text-[10.5px]">
              <span className="shrink-0 text-faint">{stream.status === 'idle' ? '她：' : '正在回：'}</span>
              <span className={`truncate ${stream.error ? 'text-red-300' : 'text-muted'}`}>
                {stream.error || replyText}
              </span>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[220px] flex-1">
              <input
                value={commandText}
                onChange={(e) => setCommandText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.nativeEvent.isComposing) void sendTalk()
                }}
                disabled={!mine}
                placeholder={mine ? '对她说一句…' : '她还没进游戏'}
                className="w-full rounded-xl border border-line bg-page/45 py-2 pl-3 pr-10 text-[12.5px] text-ink outline-none transition-colors placeholder:text-faint focus:border-accent/60 disabled:opacity-40"
              />
              <button
                type="button"
                onClick={() => void sendTalk()}
                disabled={!mine || !commandText.trim() || stream.status !== 'idle'}
                title="发送"
                className="absolute right-1.5 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-lg text-accent transition-colors hover:bg-accent/10 disabled:opacity-30"
              >
                <IconSend size={15} />
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-1">
              {QUICK_COMMANDS.map((command) => (
                <button
                  key={command.id}
                  type="button"
                  disabled={!mine}
                  onClick={() => void runQuick(command.id)}
                  className="rounded-lg border border-line/80 bg-surface/50 px-2.5 py-1.5 text-[10.5px] text-muted transition-colors hover:border-accent/50 hover:text-accent disabled:opacity-35"
                >
                  {command.label}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={() => setTab('logs')}
              title="打开日志"
              className="flex items-center gap-1 rounded-lg border border-line/80 bg-surface/50 px-2.5 py-1.5 text-[10.5px] text-muted hover:border-accent/50 hover:text-accent"
            >
              <IconTerminal size={13} />
              日志
            </button>
          </div>
        </div>
      </footer>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-xl border border-line/60 bg-page/25 px-2.5 py-1.5">
      <p className="text-[9.5px] text-faint">{label}</p>
      <p className="mt-0.5 truncate font-mono text-[12px] text-ink-soft">{value}</p>
    </div>
  )
}

function EventList({
  events,
}: {
  events: { id: string; type: string; payload: Record<string, unknown>; created_at?: string }[]
}) {
  if (events.length === 0) return <EmptyHint>还没有事件。</EmptyHint>
  return (
    <ul className="max-h-56 space-y-1 overflow-y-auto pr-1">
      {events.map((event) => (
        <li
          key={event.id}
          className="flex gap-2 rounded-lg px-1.5 py-1 text-[10.5px] leading-relaxed text-muted hover:bg-page/30"
        >
          <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent/60" aria-hidden />
          <span className="min-w-0 break-words">{eventLine(event)}</span>
        </li>
      ))}
    </ul>
  )
}
