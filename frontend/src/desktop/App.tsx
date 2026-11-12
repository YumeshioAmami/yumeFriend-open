import { useCallback, useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'

import { api, type FeatureInfo, type Friend, type GameStatus, type Settings } from '../shared/api'
import ApprovalCard from './ApprovalCard'
import Avatar from '../shared/Avatar'
import CallView from '../shared/CallView'
import ChatView from './ChatView'
import CreateFriendModal from './CreateFriendModal'
import GameModeShell from './GameModeShell'
import {
  IconBell,
  IconBellOff,
  IconGear,
  IconMoon,
  IconStack,
  IconSun,
  IconSystem,
  IconTerminal,
  IconTrash,
  LogoMark,
} from '../shared/icons'
import LogWindow from './LogWindow'
import MemoryModal from './MemoryModal'
import KnowledgeModal from './KnowledgeModal'
import ImmersiveView from '../shared/ImmersiveView'
import PersonalityModal from './PersonalityModal'
import ScheduleModal from './ScheduleModal'
import SpaceView from '../space/SpaceView'
import { applyUiScale, readUiScale, saveUiScale, stepUiScale } from './scale'
import SettingsModal, { type TabId } from './SettingsModal'
import Toasts, { type Toast } from '../shared/Toasts'
import { UpdatePrompt } from './UpdatePanel'
import TutorialCenter from './TutorialCenter'
import WelcomeGuide from './WelcomeGuide'
import type { TutorialAction } from './tutorials'
import { ACCENT, presenceFilter } from '../shared/ui'
import { featureMap, isFeatureOn } from '../shared/features'
import { IDLE_STREAM, useChat } from '../shared/useChat'
import {
  inDesktopShell,
  notificationPermission,
  notifySystem,
  useProactiveFeed,
  type ApprovalEvent,
  type AudioSegmentEvent,
} from '../shared/useWs'
import { applyTheme, readThemeChoice, saveThemeChoice, type ThemeChoice } from '../shared/theme'

/**
 * 桌面外壳里有我们自己画的弹窗（D70），所以**不要再弹系统通知**——
 * 两套一起弹会重复。在浏览器里（开发模式）没有桌面弹窗，系统通知仍然有用。
 */
const DESKTOP_SHELL = inDesktopShell()

const SETTINGS_TUTORIAL: Partial<Record<TabId, string>> = {
  models: 'chat',
  embedding: 'embedding',
  voice: 'voice',
  live2d: 'visual',
  portraits: 'visual',
  sync: 'sync',
  mobile: 'sync',
  update: 'update',
}

/** 通知被系统拦住时，要告诉用户"去哪儿放行"——只报"失败了"等于没说。 */
const NOTIFY_BLOCKED_HINT =
  '通知权限被系统拦住了，浏览器/系统不会再弹框问你第二次。\n' +
  '放行方法：Windows 设置 → 系统 → 通知，把 yumeFriend（或 Microsoft Edge WebView2）允许。'

declare global {
  interface Window {
    /** 桌面外壳的弹窗点一下会调这个（见 D70）。 */
    yumeOpenFriend?: (friendId: string) => void
    /** 桌宠「游戏管理」按钮：直接切到这位朋友的全屏游戏管理页。 */
    yumeOpenGame?: (friendId: string) => void
    /** 桌面外壳的 js_api（D121）：开桌宠 / 全屏 / 报告主题（D131） */
    pywebview?: {
      api?: {
        open_pet?: (friendId?: string | null) => unknown
        toggle_fullscreen?: () => unknown
        set_theme?: (theme: string) => unknown
      }
    }
  }
}

/** 全屏（D131）：有外壳就走 pywebview 的，浏览器里退回 Fullscreen API。 */
function toggleFullscreen(): void {
  const api = window.pywebview?.api
  if (api?.toggle_fullscreen) {
    try {
      api.toggle_fullscreen()
      return
    } catch {
      /* 退回去用浏览器那套 */
    }
  }
  try {
    if (document.fullscreenElement) void document.exitFullscreen()
    else void document.documentElement.requestFullscreen().catch(() => undefined)
  } catch {
    // 全屏不可用就算了
  }
}

/** 把当前主题告诉外壳（让它把标题栏也变深，D131）。 */
function reportTheme(theme: 'light' | 'dark'): void {
  try {
    window.pywebview?.api?.set_theme?.(theme)
  } catch {
    // 没有外壳就算了
  }
}

/**
 * 开桌宠（D121）：桌宠是**原生小窗**，只有桌面外壳里有。
 * 返回 false = 在浏览器里（开发模式），调用方退回沉浸模式。
 */
function openPet(friendId: string): boolean {
  const open = window.pywebview?.api?.open_pet
  if (!open) return false
  try {
    open(friendId)
    return true
  } catch {
    return false
  }
}

const THEME_OPTIONS = [
  { value: 'light', label: '白天', Icon: IconSun },
  { value: 'dark', label: '夜间', Icon: IconMoon },
  { value: 'system', label: '跟随系统', Icon: IconSystem },
] as const

function getSubtitle(friend: Friend, speaking: boolean): string {
  if (speaking) return '正在说话…'
  if (friend.last_message_preview) return friend.last_message_preview
  return friend.presence ? '现在在线 · 想聊点什么' : '不在场'
}

/** 主题三选一：分段控件，选中块会**滑过去**（有滑动感，不是硬闪）。 */
function ThemeSwitcher({
  choice,
  onChange,
}: {
  choice: ThemeChoice
  onChange: (choice: ThemeChoice) => void
}) {
  const index = THEME_OPTIONS.findIndex((option) => option.value === choice)
  return (
    <div className="relative flex items-center gap-0.5 rounded-full border border-line/70 bg-surface/60 p-0.5">
      <span
        className="absolute left-0.5 top-0.5 h-7 w-7 rounded-full bg-accent/10 shadow-[0_1px_6px_rgba(194,105,71,0.18)] ring-1 ring-accent/25 transition-transform duration-300 ease-out"
        style={{ transform: `translateX(${Math.max(0, index) * 30}px)` }}
        aria-hidden
      />
      {THEME_OPTIONS.map(({ value, label, Icon }) => (
        <button
          key={value}
          type="button"
          title={label}
          aria-label={label}
          aria-pressed={choice === value}
          onClick={() => onChange(value)}
          className={`group relative z-10 flex h-7 w-7 items-center justify-center rounded-full transition-colors duration-200 ${
            choice === value ? 'text-accent' : 'text-faint hover:text-muted'
          }`}
        >
          <Icon size={14} className="transition-transform duration-200 group-hover:scale-110" />
        </button>
      ))}
    </div>
  )
}

/** 左下角那三个按钮：图标 + 小字，悬浮时轻轻抬起（"悬浮化"）。 */
function DockButton({
  icon,
  label,
  hint,
  active,
  dot,
  onClick,
}: {
  icon: ReactNode
  label: string
  hint: string
  active?: boolean
  dot?: 'on' | 'off'
  onClick: () => void
}) {
  return (
    <button
      type="button"
      title={hint}
      onClick={onClick}
      className={`group flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-xl px-2 py-1.5 text-[11.5px] transition-all duration-200 hover:-translate-y-px hover:bg-surface/80 hover:shadow-[0_2px_10px_rgba(0,0,0,0.07)] ${
        active ? 'text-accent' : 'text-muted hover:text-ink'
      }`}
    >
      <span className="relative flex items-center">
        {icon}
        {dot && (
          <span
            className={`absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-full ${dot === 'off' ? 'bg-faint' : ''}`}
            style={dot === 'on' ? { background: ACCENT } : undefined}
            aria-hidden
          />
        )}
      </span>
      {label}
    </button>
  )
}

export default function App() {
  const [friends, setFriends] = useState<Friend[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [showSpace, setShowSpace] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [features, setFeatures] = useState<FeatureInfo[] | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [settingsInitialTab, setSettingsInitialTab] = useState<TabId | null>(null)
  const [showTutorials, setShowTutorials] = useState(false)
  const [tutorialArticleId, setTutorialArticleId] = useState<string | null>(null)
  const [welcomeDismissed, setWelcomeDismissed] = useState(
    () => localStorage.getItem('yf.tutorial.welcome.v1') === '1',
  )
  const [welcomeOpen, setWelcomeOpen] = useState(false)
  const [showMemories, setShowMemories] = useState(false)
  const [showKnowledge, setShowKnowledge] = useState(false)
  const [approvals, setApprovals] = useState<ApprovalEvent[]>([])
  const [notifyPermission, setNotifyPermission] = useState(notificationPermission())
  const [showLogs, setShowLogs] = useState(false)
  const [showSchedules, setShowSchedules] = useState(false)
  // v5.1（D287）：游戏不是弹窗，是整窗切换的全屏壳。
  const [gameMode, setGameMode] = useState(false)
  const [gameStatus, setGameStatus] = useState<GameStatus | null>(null)
  const [editDetail, setEditDetail] = useState<Awaited<ReturnType<typeof api.getFriend>> | null>(null)
  const [recoverable, setRecoverable] = useState<{ id: string; name: string; deleted_at: string }[]>([])
  const [showDeleted, setShowDeleted] = useState(false)
  const [toasts, setToasts] = useState<Toast[]>([])
  const [themeChoice, setThemeChoice] = useState<ThemeChoice>(() => readThemeChoice())
  const [uiScale, setUiScale] = useState<number>(() => readUiScale())
  // v2.2（T2.2-2）：沉浸模式 + 分段语音（按朋友收着，切朋友不用重连）
  const [immersive, setImmersive] = useState(false)
  // D126：语音通话（不显示文字）
  const [inCall, setInCall] = useState(false)
  const [voiceSegments, setVoiceSegments] = useState<Record<string, AudioSegmentEvent[]>>({})
  const selectedIdRef = useRef<string | null>(null)
  const spaceAutoOpened = useRef(false)
  const scaleRef = useRef(uiScale)
  const toastSeq = useRef(0)

  const chat = useChat()
  selectedIdRef.current = selectedId

  useEffect(() => {
    if (spaceAutoOpened.current || friends.length === 0) return
    if (new URLSearchParams(window.location.search).get('space') !== '1') return
    spaceAutoOpened.current = true
    const target = friends.find((friend) => friend.name === '汐音') ?? friends[0]
    setSelectedId(target.id)
    setShowSpace(true)
  }, [friends])
  scaleRef.current = uiScale

  /** 不是错误、但必须说一声的提示（"缩放 130%""通知已开"）。 */
  const showInfo = useCallback((text: string) => {
    toastSeq.current += 1
    setToasts((prev) => [...prev, { id: toastSeq.current, text, kind: 'info' as const }].slice(-3))
  }, [])

  /** 出错的统一出口：横幅留痕 + 飘一条 Toast（带【查看日志】）。 */
  const fail = useCallback((message: string) => {
    setError(message)
    toastSeq.current += 1
    setToasts((prev) =>
      [...prev, { id: toastSeq.current, text: message, kind: 'error' as const }].slice(-3),
    )
  }, [])

  /**
   * 界面缩放（用户要的：手动、可控）。
   * 三种入口都走这一个函数：设置里点档位 / Ctrl+滚轮 / Ctrl+加减号。
   */
  const changeScale = useCallback(
    (next: number, announce = true) => {
      if (Math.abs(next - scaleRef.current) < 0.001) {
        if (announce) showInfo(`界面缩放已经是 ${Math.round(next * 100)}% 了`)
        return
      }
      scaleRef.current = next
      setUiScale(next)
      if (announce) showInfo(`界面缩放：${Math.round(next * 100)}%`)
    },
    [showInfo],
  )

  async function onToggleNotifications() {
    if (notifyPermission === 'unsupported') {
      showInfo('这个环境没有系统通知能力，朋友的消息还是会在窗口里出现。')
      return
    }
    if (notifyPermission === 'granted') {
      showInfo('系统通知已经开着：朋友主动找你时，窗口关着也会弹一条提醒。')
      return
    }
    if (notifyPermission === 'denied') {
      showInfo(NOTIFY_BLOCKED_HINT)
      return
    }
    try {
      const result = await Notification.requestPermission()
      setNotifyPermission(result)
      showInfo(result === 'granted' ? '系统通知已开启。' : NOTIFY_BLOCKED_HINT)
    } catch (e) {
      showInfo(`开通知失败了：${e instanceof Error ? e.message : String(e)}\n（不影响聊天，只是没有系统弹窗）`)
    }
  }

  const refresh = useCallback(async () => {
    try {
      setFriends(await api.listFriends())
      setRecoverable(await api.listRecoverable())
      setError(null)
    } catch (e) {
      fail(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [fail])

  /** A2：删朋友（软删，30 天内可恢复） */
  async function onDeleteFriend(friend: Friend, e: MouseEvent) {
    e.stopPropagation()
    if (!window.confirm(`删掉「${friend.name}」？\n30 天内可以在「已删除」里恢复。`)) return
    try {
      await api.softDelete(friend.id)
      if (selectedIdRef.current === friend.id) setSelectedId(null)
      await refresh()
    } catch (err) {
      fail(err instanceof Error ? err.message : String(err))
    }
  }

  async function onRestoreFriend(id: string) {
    try {
      await api.restoreFriend(id)
      await refresh()
    } catch (err) {
      fail(err instanceof Error ? err.message : String(err))
    }
  }

  async function onPurgeFriend(friend: { id: string; name: string }) {
    if (
      !window.confirm(
        `彻底删掉「${friend.name}」？\n它的消息、记忆、代办会一起没掉，**不可恢复**。`,
      )
    )
      return
    try {
      await api.purgeFriend(friend.id)
      await refresh()
    } catch (err) {
      fail(err instanceof Error ? err.message : String(err))
    }
  }

  async function onEditPersonality(friendId: string) {
    try {
      setEditDetail(await api.getFriend(friendId))
    } catch (err) {
      fail(err instanceof Error ? err.message : String(err))
    }
  }

  const loadSettings = useCallback(async () => {
    try {
      const next = await api.getSettings()
      setSettings(next)
      const status = await api.setupStatus()
      if (status.needs_setup) {
        setSettingsInitialTab(null)
        setShowSettings(true) // 首次配置引导（api.md §5.4）
      }
    } catch (e) {
      fail(e instanceof Error ? e.message : String(e))
    }
  }, [fail])

  const loadFeatures = useCallback(async () => {
    try {
      const result = await api.getFeatures()
      setFeatures(result.features)
    } catch {
      // 功能清单拉不到 = 按全开处理（不打扰用户；后台本身也是 fail-open）
    }
  }, [])

  useEffect(() => {
    void refresh()
    void loadSettings()
    void loadFeatures()
  }, [refresh, loadSettings, loadFeatures])

  useEffect(() => {
    if (loading || welcomeDismissed) return
    const chatReady = Boolean(settings?.purposes_enabled?.chat)
    if (!chatReady || friends.length === 0) setWelcomeOpen(true)
  }, [loading, welcomeDismissed, settings, friends.length])

  function openTutorials(articleId?: string) {
    setTutorialArticleId(articleId ?? null)
    setShowTutorials(true)
  }

  function closeWelcome() {
    localStorage.setItem('yf.tutorial.welcome.v1', '1')
    setWelcomeDismissed(true)
    setWelcomeOpen(false)
  }

  function handleTutorialAction(action: TutorialAction) {
    if (action === 'create-friend') {
      setShowTutorials(false)
      setShowCreate(true)
      return
    }
    if (action === 'resource') {
      window.open('https://www.yumeshio.cn/uploads/yumefriend/', '_blank')
      return
    }
    if (action === 'restart-welcome') {
      localStorage.removeItem('yf.tutorial.welcome.v1')
      setWelcomeDismissed(false)
      setShowTutorials(false)
      setWelcomeOpen(true)
      return
    }
    const tabMap: Record<string, TabId> = {
      'open-models': 'models',
      'open-embedding': 'embedding',
      'open-voice': 'voice',
      'open-live2d': 'live2d',
      'open-portraits': 'portraits',
      'open-sync': 'sync',
      'open-mobile': 'mobile',
      'open-update': 'update',
    }
    const nextTab = tabMap[action]
    if (!nextTab) return
    setShowTutorials(false)
    setSettingsInitialTab(nextTab)
    setShowSettings(true)
  }

  // v5.1（D287）：普通界面也要知道"游戏还在跑" —— 退出全屏壳不等于停局。
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const next = await api.gameStatus()
        if (alive) setGameStatus(next)
      } catch {
        // 游戏状态拉不到不影响聊天；下次轮询再试
      }
    }
    void tick()
    const timer = window.setInterval(() => void tick(), 4000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  // 夜间模式：把选择挂到 <html data-theme>，色板整套换掉
  useEffect(() => {
    applyTheme(themeChoice)
    saveThemeChoice(themeChoice)
    // 标题栏跟着走（D131）：system 时按系统那套判断
    reportTheme(
      themeChoice === 'system'
        ? window.matchMedia('(prefers-color-scheme: dark)').matches
          ? 'dark'
          : 'light'
        : themeChoice,
    )
    if (themeChoice !== 'system') return
    // 跟随系统时要能实时跟着变
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => applyTheme('system')
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [themeChoice])

  // 界面缩放：落盘 + 生效（本机偏好，跟后端设置无关）
  useEffect(() => {
    applyUiScale(uiScale)
    saveUiScale(uiScale)
  }, [uiScale])

  // 桌面外壳的弹窗点一下 → 跳到那个朋友（D70）
  useEffect(() => {
    window.yumeOpenFriend = (friendId: string) => {
      setSelectedId(friendId)
      // 桌宠那边可能刚说过话，主窗口这边要重新拉一遍历史（D119）
      void chat.loadHistory(friendId).catch(() => undefined)
      void refresh()
    }
    return () => {
      delete window.yumeOpenFriend
    }
  }, [chat.loadHistory, refresh])

  useEffect(() => {
    window.yumeOpenGame = (friendId: string) => {
      if (!isFeatureOn(featureMap(features), 'game')) {
        showInfo('「游戏模式」现在是关着的 —— 去设置 → 功能里打开再用')
        return
      }
      setSelectedId(friendId)
      setImmersive(false)
      setInCall(false)
      setGameMode(true)
    }
    return () => {
      delete window.yumeOpenGame
    }
  }, [features, showInfo])

  // 缩放快捷键：Ctrl+滚轮 / Ctrl+加号 / Ctrl+减号 / Ctrl+0 复位（网站那套习惯）
  useEffect(() => {
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return
      event.preventDefault()
      changeScale(stepUiScale(scaleRef.current, event.deltaY < 0 ? 1 : -1))
    }
    const onKey = (event: KeyboardEvent) => {
      if (!event.ctrlKey && !event.metaKey) return
      if (event.key === '=' || event.key === '+') {
        event.preventDefault()
        changeScale(stepUiScale(scaleRef.current, 1))
      } else if (event.key === '-' || event.key === '_') {
        event.preventDefault()
        changeScale(stepUiScale(scaleRef.current, -1))
      } else if (event.key === '0') {
        event.preventDefault()
        changeScale(1)
      }
    }
    window.addEventListener('wheel', onWheel, { passive: false })
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('wheel', onWheel)
      window.removeEventListener('keydown', onKey)
    }
  }, [changeScale])

  useEffect(() => {
    // 切朋友就退出沉浸模式（否则整屏那张脸会变成别人的，见 D105）
    setImmersive(false)
    setInCall(false) // 通话也一样，切朋友就挂断（D126）
    if (selectedId) void chat.loadHistory(selectedId)
    // 进到这个朋友，就把未读清零（frontend.md §3.3）
    if (selectedId) void api.markRead(selectedId).then(refresh).catch(() => undefined)
    // 只在切朋友时拉一次历史
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId])

  // v2.1（T2.1-5 / D100）：把"我现在开着谁"报给后端 —— 定时看屏**只给这个朋友**看。
  // 切朋友时报一次 + 每 60 秒心跳一次（后端 3 分钟收不到就算"没人"）。
  useEffect(() => {
    void api.setFocus(selectedId).catch(() => undefined)
    const timer = window.setInterval(() => {
      void api.setFocus(selectedId).catch(() => undefined)
    }, 60_000)
    return () => {
      window.clearInterval(timer)
      void api.setFocus(null).catch(() => undefined)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId])

  // 窗口重新拿到焦点 → 把当前朋友的历史拉一遍：桌宠那边刚聊过的话就同步过来了（D119）
  useEffect(() => {
    const onFocus = () => {
      const id = selectedIdRef.current
      if (id) void chat.loadHistory(id).catch(() => undefined)
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [chat.loadHistory])

  const selected = friends.find((f) => f.id === selectedId) ?? null
  const gameFriend = friends.find((f) => f.id === gameStatus?.friend_id) ?? null
  const featureById = featureMap(features)

  /** 游戏按钮：优先当前选中的朋友；没选时退回正在玩的那位，最后才拿列表第一个。 */
  const openGameMode = useCallback(() => {
    if (!isFeatureOn(featureMap(features), 'game')) {
      showInfo('「游戏模式」现在是关着的 —— 去设置 → 功能里打开再用')
      return
    }
    const targetId =
      selectedId ??
      (gameStatus?.active ? gameStatus.friend_id : null) ??
      friends[0]?.id ??
      null
    if (!targetId) {
      showInfo('先创建一个朋友，再进游戏模式。')
      return
    }
    setSelectedId(targetId)
    setImmersive(false)
    setInCall(false)
    setGameMode(true)
  }, [features, friends, gameStatus, selectedId, showInfo])

  /** 主动消息推过来了（api.md §7 / frontend.md §3.3）：
   *  · 正在看着这个朋友 → 直接进聊天流 + 清零未读（不该算未读）
   *  · 否则 → 刷新列表（未读徽标 +1）+ 弹系统通知
   */
  const onProactive = useCallback(
    async (event: { friendId: string; friendName: string; message: { content: string } }) => {
      // 本机正在跑这一轮 SSE 时，WS 的“回复已落库”只是同一条消息的完成通知；
      // 让 useChat 在气泡节奏播完后再 loadHistory，否则会先冒出完整消息、再冒等待泡。
      if (chat.isStreaming(event.friendId)) return
      const looking =
        selectedIdRef.current === event.friendId && document.visibilityState === 'visible'
      if (looking) {
        await chat.loadHistory(event.friendId)
        await api.markRead(event.friendId).catch(() => undefined)
      } else if (!DESKTOP_SHELL) {
        // 桌面外壳里不弹系统通知：内核那一步已经弹了"自己画的卡片"（D70）
        notifySystem(event.friendName, event.message.content.slice(0, 80), () => {
          window.focus()
          setSelectedId(event.friendId)
        })
      }
      await refresh()
    },
    [chat, refresh],
  )
  /** 语音合成好了（WS 推过来）→ 只改那一条消息的语音条，不重载整页。 */
  const onAudioStatus = useCallback(
    (event: { friendId: string; messageId: string; audioStatus: string | null; audioUrl: string | null; audioDurationMs: number | null; audioError: string | null }) => {
      chat.patchMessage(event.friendId, event.messageId, {
        audio_status: event.audioStatus as 'pending' | 'ready' | 'failed' | null,
        audio_url: event.audioUrl,
        audio_duration_ms: event.audioDurationMs,
        audio_error: event.audioError,
      })
    },
    [chat],
  )
  /** v2.2（D105）：她的**某一段**语音好了 → 沉浸模式排队播（普通界面不看它）。 */
  const onAudioSegment = useCallback((event: AudioSegmentEvent) => {
    if (!event.audioUrl) return
    setVoiceSegments((prev) => {
      const list = prev[event.friendId] ?? []
      const key = `${event.messageId}:${event.index}`
      // v3.11（D178）：同一段**会来两条** —— 先是"开始合成了"（能边生成边播），
      // 合成完再来一条带时长的。所以这里**替换**而不是跳过（不然进度条永远算不出总长）。
      const next = [...list.filter((item) => `${item.messageId}:${item.index}` !== key), event]
      // 只留最近的 60 段，别让内存跟着聊天天数一起涨
      return { ...prev, [event.friendId]: next.slice(-60) }
    })
  }, [])
  useProactiveFeed(
    (event) => void onProactive(event),
    onAudioStatus,
    // v2.1（D99）：她要做改动磁盘的事 → 弹卡片等用户点头
    (event) => setApprovals((prev) => [...prev.filter((item) => item.id !== event.id), event]),
    onAudioSegment,
  )

  async function onTogglePresence(friend: Friend, e: MouseEvent) {
    e.stopPropagation()
    try {
      await api.setPresence(friend.id, !friend.presence)
      await refresh()
    } catch (err) {
      fail(err instanceof Error ? err.message : String(err))
    }
  }

  async function onSwitchMode(friend: Friend, mode: string, role: string | null) {
    try {
      await api.setMode(friend.id, mode, role)
      await refresh()
    } catch (e) {
      fail(e instanceof Error ? e.message : String(e))
    }
  }

  async function handleSend(friendId: string, text: string, imageId?: string, previewUrl?: string) {
    await chat.send(friendId, text, { imageId, previewUrl })
    await refresh()
  }

  /** v2.1（T2.1-5 / D100）：看屏 = 带上截图的普通回合（输入框里已经打的字一起带上）。 */
  async function handleLookScreen(friendId: string, draft: string) {
    await chat.send(friendId, draft, { screen: true })
    await refresh()
  }

  if (gameMode && selected && isFeatureOn(featureById, 'game')) {
    const messages = chat.messages[selected.id] ?? []
    const lastAssistant = [...messages].reverse().find((message) => message.role === 'assistant')
    return (
      <div className="flex h-full flex-col">
        <GameModeShell
          friend={selected}
          settings={settings}
          stream={chat.streams[selected.id] ?? IDLE_STREAM}
          lastReply={lastAssistant?.content ?? null}
          onTalk={(text) => handleSend(selected.id, text)}
          onSettingsChanged={() => void loadSettings()}
          onExit={() => setGameMode(false)}
        />
        <Toasts
          toasts={toasts}
          onDismiss={(id) => setToasts((prev) => prev.filter((t) => t.id !== id))}
          onOpenLogs={() => setShowLogs(true)}
        />
        {showLogs && <LogWindow onClose={() => setShowLogs(false)} />}
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {/* 顶栏 */}
      <header className="z-20 flex h-12 shrink-0 items-center gap-3 border-b border-line/60 bg-surface/50 px-4 backdrop-blur-xl">
        {/* 品牌：渐变徽标 + 字标（要"有牌面"，但不是张牙舞爪） */}
        <div className="group flex select-none items-center gap-2.5">
          <span className="relative flex items-center">
            <span
              className="absolute inset-0 rounded-[10px] opacity-0 blur-md transition-opacity duration-300 group-hover:opacity-60"
              style={{ background: ACCENT }}
              aria-hidden
            />
            <LogoMark
              size={30}
              className="relative transition-transform duration-300 group-hover:scale-[1.05]"
            />
          </span>
          <span className="flex flex-col leading-none">
            <span className="text-[14.5px] font-semibold tracking-[-0.01em] text-ink">
              yumeFriend
            </span>
            <span className="mt-[3px] text-[9.5px] tracking-[0.2em] text-faint">
              AI COMPANION
            </span>
          </span>
        </div>

        {gameStatus?.active && isFeatureOn(featureById, 'game') && (
          <button
            type="button"
            onClick={() => {
              if (gameStatus.friend_id) setSelectedId(gameStatus.friend_id)
              setGameMode(true)
            }}
            title="她还在游戏里；点一下回到全屏游戏模式"
            className="group ml-1 flex items-center gap-1.5 rounded-full border border-emerald-300/35 bg-emerald-50/60 px-2.5 py-1 text-[10.5px] text-emerald-700 transition-colors hover:border-emerald-400/60 hover:bg-emerald-50 dark:bg-emerald-400/10 dark:text-emerald-200"
          >
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-50" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" />
            </span>
            游戏中
            {gameFriend && <span className="max-w-24 truncate text-emerald-600/80 dark:text-emerald-200/70">· {gameFriend.name}</span>}
          </button>
        )}

        <div className="ml-auto flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => openTutorials()}
            title="教程中心"
            aria-label="教程中心"
            className="flex h-6 w-6 items-center justify-center rounded-full border border-line/70 bg-surface/60 text-[12px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
          >
            ?
          </button>
          <button
            type="button"
            onClick={toggleFullscreen}
            title="全屏 / 退出全屏"
            aria-label="全屏"
            className="rounded-full border border-line/70 bg-surface/60 px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
          >
            全屏
          </button>
          <ThemeSwitcher choice={themeChoice} onChange={setThemeChoice} />
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* 左栏：玻璃态面板 */}
        <aside className="relative z-10 flex w-72 shrink-0 flex-col border-r border-blue-200/40 bg-surface/40 shadow-[4px_0_24px_rgba(59,130,246,0.06)] backdrop-blur-2xl">
          <div className="shrink-0 border-b border-line/40 px-3 py-2.5">
            <button
              type="button"
              onClick={() => setShowCreate((v) => !v)}
              disabled={loading}
              className="group flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-line-strong/80 bg-surface/50 px-3 py-2 text-[12px] font-medium text-muted transition-colors duration-150 hover:border-accent/60 hover:bg-accent/5 hover:text-accent disabled:opacity-40 disabled:hover:border-line-strong/80 disabled:hover:bg-surface/50 disabled:hover:text-muted"
            >
              <span
                className="flex h-4 w-4 items-center justify-center rounded-full text-[14px] leading-none transition-transform duration-150 group-hover:rotate-90"
                style={{ color: ACCENT }}
                aria-hidden
              >
                +
              </span>
              创建朋友
            </button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading && (
              <div className="flex flex-col items-center gap-3 px-4 py-12">
                <div className="h-7 w-7 animate-spin rounded-full border-2 border-line border-t-accent" />
                <p className="text-[13px] text-muted">加载中</p>
              </div>
            )}

            {!loading && friends.length === 0 && (
              <div className="px-4 py-12 text-center">
                <p className="text-[13px] text-muted">还没有朋友</p>
                <p className="mt-1 text-[11px] text-slate-400">点上面的「创建朋友」试试</p>
              </div>
            )}

            {!loading && friends.length > 0 && (
              <ul className="flex flex-col py-1">
                {friends.map((friend, idx) => {
                  const isSelected = friend.id === selectedId
                  const speaking = chat.isStreaming(friend.id)
                  return (
                    <li
                      key={friend.id}
                      className="animate-fade-up"
                      style={{
                        animationDelay: `${Math.min(idx, 8) * 30}ms`,
                        animationFillMode: 'both',
                      }}
                    >
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => setSelectedId(friend.id)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            setSelectedId(friend.id)
                          }
                        }}
                        className={`group relative flex w-full cursor-pointer items-center gap-3 px-3 py-2 text-left outline-none transition-colors duration-150 focus-visible:bg-surface-hover ${
                          isSelected ? 'bg-surface-hover' : 'hover:bg-surface-hover active:bg-surface-hover'
                        }`}
                      >
                        {isSelected && (
                          <span
                            className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-r"
                            style={{ background: ACCENT }}
                            aria-hidden
                          />
                        )}

                        {/* 头像 + 在场切换 */}
                        <div className="relative shrink-0">
                          <button
                            type="button"
                            onClick={(e) => void onTogglePresence(friend, e)}
                            aria-label={
                              friend.presence ? '在场，点击切到不在场' : '不在场，点击切到在场'
                            }
                            className={`absolute -inset-[3px] cursor-pointer rounded-full transition-shadow duration-300 ${
                              friend.presence ? '' : 'opacity-0 hover:opacity-100'
                            }`}
                            style={{
                              boxShadow: friend.presence
                                ? 'none'
                                : '0 0 0 1px rgba(120,113,108,0.4), 0 0 6px rgba(120,113,108,0.25)',
                            }}
                          />
                          <div
                            className="relative"
                            style={{
                              filter: presenceFilter(friend.presence),
                              animation: friend.presence
                                ? 'presence-pulse 3.2s ease-in-out infinite'
                                : undefined,
                            }}
                          >
                            <Avatar name={friend.name} url={friend.avatar_url} size={36} />
                          </div>
                        </div>

                        <div className="flex min-w-0 flex-1 flex-col">
                          <span
                            className={`truncate text-[13px] leading-tight transition-colors duration-150 ${
                              isSelected
                                ? 'font-medium text-ink'
                                : 'text-ink-soft group-hover:text-ink'
                            }`}
                          >
                            {friend.name}
                          </span>
                          <span
                            className={`mt-0.5 truncate text-[11px] leading-tight ${
                              speaking
                                ? 'italic text-accent'
                                : friend.presence
                                  ? 'italic text-muted'
                                  : 'text-faint'
                            }`}
                          >
                            {getSubtitle(friend, speaking)}
                          </span>
                        </div>

                        <div className="flex shrink-0 items-center gap-1.5">
                          <button
                            type="button"
                            title="删掉这个朋友（30 天内可恢复）"
                            onClick={(e) => void onDeleteFriend(friend, e)}
                            aria-label="删掉这个朋友"
                            className="flex items-center rounded px-1 text-stone-300 opacity-0 transition-opacity hover:text-red-500 group-hover:opacity-100"
                          >
                            <IconTrash size={13} />
                          </button>
                          {friend.unread_count > 0 && (
                            <span
                              className="flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] font-medium text-white"
                              style={{ background: ACCENT }}
                            >
                              {friend.unread_count > 9 ? '9+' : friend.unread_count}
                            </span>
                          )}
                          {isSelected && (
                            <span className="text-xs" style={{ color: ACCENT }} aria-hidden>
                              ›
                            </span>
                          )}
                        </div>
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>

          {/* 已删除（A2）：30 天内可恢复，或彻底删掉 */}
          {recoverable.length > 0 && (
            <div className="shrink-0 border-t border-line/40">
              <button
                type="button"
                onClick={() => setShowDeleted((v) => !v)}
                className="flex w-full items-center gap-1 px-3 py-1.5 text-[11px] text-faint hover:text-muted"
              >
                {showDeleted ? '▾' : '▸'} 已删除 {recoverable.length}
              </button>
              {showDeleted && (
                <ul className="max-h-40 overflow-y-auto pb-1">
                  {recoverable.map((item) => {
                    const daysLeft = Math.max(
                      0,
                      30 -
                        Math.floor(
                          (Date.now() - new Date(item.deleted_at ?? Date.now()).getTime()) / 86400000,
                        ),
                    )
                    return (
                      <li
                        key={item.id}
                        className="flex items-center gap-2 px-3 py-1 text-[11.5px] text-muted"
                      >
                        <span className="min-w-0 flex-1 truncate">{item.name}</span>
                        <span className="shrink-0 text-[10px] text-faint">{daysLeft} 天</span>
                        <button
                          type="button"
                          onClick={() => void onRestoreFriend(item.id)}
                          className="shrink-0 rounded px-1 text-[10.5px] text-accent hover:underline"
                        >
                          恢复
                        </button>
                        <button
                          type="button"
                          onClick={() => void onPurgeFriend(item)}
                          className="shrink-0 rounded px-1 text-[10.5px] text-faint hover:text-red-600"
                        >
                          彻底删
                        </button>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>
          )}

          <div className="flex shrink-0 items-center gap-0.5 border-t border-line/40 px-2.5 py-1.5">
            {isFeatureOn(featureById, 'game') && (
              <DockButton
                label="游戏"
                hint="打开全屏游戏模式：开局 / 设置 / 作弊 / 数据 / 日志"
                icon={<IconStack size={15} />}
                active={Boolean(gameStatus?.active)}
                dot={gameStatus?.active ? 'on' : undefined}
                onClick={openGameMode}
              />
            )}
            <DockButton
              label="设置"
              hint="按用途配模型 / 语音 / 行为 / 记忆 / 界面缩放"
              icon={<IconGear size={15} />}
              onClick={() => {
                setSettingsInitialTab(null)
                setShowSettings(true)
              }}
            />
            <DockButton
              label="日志"
              hint="后台在干什么（调试、报错时来看）"
              icon={<IconTerminal size={15} />}
              onClick={() => setShowLogs(true)}
            />
            {/* 桌面外壳有自己画的弹窗（D70），不用系统通知，这个按钮就没必要了；
                在浏览器里（开发模式）保留它 */}
            {!DESKTOP_SHELL && (
              <DockButton
                label={notifyPermission === 'granted' ? '通知已开' : '通知'}
                hint={
                  notifyPermission === 'granted'
                    ? '系统通知已开启（朋友主动找你时会弹，点一下看说明）'
                    : notifyPermission === 'denied'
                      ? '系统通知被拦住了 —— 点一下看怎么放行'
                      : '点一下开启系统通知：朋友主动找你时，窗口关着也能弹提醒'
                }
                icon={
                  notifyPermission === 'denied' || notifyPermission === 'unsupported' ? (
                    <IconBellOff size={15} />
                  ) : (
                    <IconBell size={15} />
                  )
                }
                active={notifyPermission === 'granted'}
                dot={
                  notifyPermission === 'granted'
                    ? 'on'
                    : notifyPermission === 'denied' || notifyPermission === 'unsupported'
                      ? 'off'
                      : undefined
                }
                onClick={() => void onToggleNotifications()}
              />
            )}
            {/* 用户要的：左下角这里留空白（以前写死显示当前模型名） */}
            <span className="ml-auto min-w-0 flex-1" />
          </div>
        </aside>

        {/* 右栏 */}
        <main className="relative flex min-w-0 flex-1 flex-col">
          {error && (
            <div className="border-b border-red-200 bg-red-50/80 px-4 py-2 text-[12px] text-red-700 backdrop-blur-sm">
              出错了：{error}
            </div>
          )}
          {selected ? (
            <ChatView
              key={selected.id}
              friend={selected}
              messages={chat.messages[selected.id] ?? []}
              stream={chat.streams[selected.id]}
              hasMore={chat.hasMore[selected.id] ?? false}
              showToolCalls={settings?.show_tool_calls ?? false}
              visionEnabled={settings?.purposes_enabled?.vision ?? false}
              features={features}
              onSend={(text, imageId, previewUrl) =>
                void handleSend(selected.id, text, imageId, previewUrl)
              }
              onStop={() => chat.stop(selected.id)}
              onLoadOlder={() => void chat.loadOlder(selected.id)}
              onSwitchMode={(mode, role) => void onSwitchMode(selected, mode, role)}
              onOpenMemories={() => setShowMemories(true)}
              onOpenSchedules={() => setShowSchedules(true)}
              onEditPersonality={() => void onEditPersonality(selected.id)}
              onOpenKnowledge={() => setShowKnowledge(true)}
              onLookScreen={(draft) => void handleLookScreen(selected.id, draft)}
              onOpenImmersive={() => setImmersive(true)}
              onOpenSpace={() => setShowSpace(true)}
              onOpenCall={() => setInCall(true)}
              // 桌宠是原生小窗（D121）：桌面外壳里开它；浏览器里退回沉浸模式
              onOpenPet={() => {
                if (!openPet(selected.id)) setImmersive(true)
              }}
              voiceSegments={voiceSegments[selected.id] ?? []}
            />
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-1">
              <p className="text-[13px] text-muted">从左边选一个朋友</p>
              <p className="text-[11px] text-faint">或者先创建一个</p>
            </div>
          )}
        </main>
      </div>

      {showCreate && (
        <CreateFriendModal
          onClose={() => setShowCreate(false)}
          onCreated={(id, warning) => {
            setShowCreate(false)
            void refresh()
            setSelectedId(id)
            if (warning) fail(warning)
          }}
        />
      )}

      {showSettings && settings && (
        <SettingsModal
          settings={settings}
          uiScale={uiScale}
          initialTab={settingsInitialTab ?? undefined}
          onOpenTutorial={(nextTab) => {
            setShowSettings(false)
            openTutorials(SETTINGS_TUTORIAL[nextTab] ?? 'start')
          }}
          onScaleChange={changeScale}
          onOpenGameMode={() => {
            if (!isFeatureOn(featureById, 'game')) {
              showInfo('「游戏模式」现在是关着的 —— 去设置 → 功能里打开再用')
              return
            }
            setShowSettings(false)
            openGameMode()
          }}
          onClose={() => {
            setShowSettings(false)
            setSettingsInitialTab(null)
          }}
          onSaved={() => {
            void loadSettings()
            void loadFeatures()
          }}
        />
      )}

      {showMemories && selected && (
        <MemoryModal
          friendId={selected.id}
          friendName={selected.name}
          onClose={() => setShowMemories(false)}
        />
      )}

      {/* v2.1（D99）：工具审批卡片（右下角，不挡聊天） */}
      {approvals.length > 0 && (
        <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col gap-2">
          {approvals.map((item) => (
            <ApprovalCard
              key={item.id}
              request={item}
              onDone={(id) => setApprovals((prev) => prev.filter((one) => one.id !== id))}
            />
          ))}
        </div>
      )}

      {showLogs && <LogWindow onClose={() => setShowLogs(false)} />}

      {showSchedules && selected && (
        <ScheduleModal
          friendId={selected.id}
          friendName={selected.name}
          onClose={() => setShowSchedules(false)}
        />
      )}

      {showKnowledge && selected && (
        <KnowledgeModal
          friend={selected}
          onClose={() => setShowKnowledge(false)}
          onChanged={() => void refresh()}
        />
      )}

      {editDetail && (
        <PersonalityModal
          friend={editDetail}
          onClose={() => setEditDetail(null)}
          onSaved={() => void refresh()}
        />
      )}

      <Toasts
        toasts={toasts}
        onDismiss={(id) => setToasts((prev) => prev.filter((t) => t.id !== id))}
        onOpenLogs={() => setShowLogs(true)}
      />

      <UpdatePrompt />
      <WelcomeGuide
        open={welcomeOpen}
        chatReady={Boolean(settings?.purposes_enabled?.chat)}
        friendCount={friends.length}
        onClose={closeWelcome}
        onOpenModels={() => {
          closeWelcome()
          setSettingsInitialTab('models')
          setShowSettings(true)
        }}
        onCreateFriend={() => {
          closeWelcome()
          setShowCreate(true)
        }}
      />
      <TutorialCenter
        open={showTutorials}
        initialArticleId={tutorialArticleId}
        onClose={() => setShowTutorials(false)}
        onAction={handleTutorialAction}
      />

      {/* v2.2（T2.2-2）：沉浸模式 —— **整屏**（连左栏一起盖住，D105/D106）。
          关掉它的方式只有 Esc / 右上角「退出」（别让列表把人拉出戏）。 */}
      {immersive && selected && isFeatureOn(featureById, 'immersive') && (
        <ImmersiveView
          friend={selected}
          messages={chat.messages[selected.id] ?? []}
          stream={chat.streams[selected.id]}
          segments={voiceSegments[selected.id] ?? []}
          onSend={(text) => void handleSend(selected.id, text)}
          onStop={() => chat.stop(selected.id)}
          onClose={() => setImmersive(false)}
          myName={settings?.my_name ?? '我'}
          onMyNameChange={(name) => {
            // B9：存进 DB（两端一致）；先本地更新，界面不用等请求回来
            setSettings((prev) => (prev ? { ...prev, my_name: name } : prev))
            void api.patchSettings({ my_name: name })
          }}
          onOpenCall={() => {
            setImmersive(false)
            setInCall(true)
          }}
        />
      )}

      {/* v2.3（D126）：语音通话 —— 不显示文字，只有立绘 + 一行状态 + 挂断 */}
      {inCall && selected && isFeatureOn(featureById, 'immersive') && (
        <CallView
          friend={selected}
          messages={chat.messages[selected.id] ?? []}
          stream={chat.streams[selected.id]}
          segments={voiceSegments[selected.id] ?? []}
          onSend={(text) => void handleSend(selected.id, text)}
          onStop={() => chat.stop(selected.id)}
          onClose={() => setInCall(false)}
        />
      )}

      {showSpace && selected && isFeatureOn(featureById, 'space') && (
        <SpaceView
          friend={selected}
          onClose={() => {
            setShowSpace(false)
            void chat.loadHistory(selected.id).catch(() => undefined)
          }}
        />
      )}
    </div>
  )
}
