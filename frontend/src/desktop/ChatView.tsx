/** 聊天区（frontend.md §1 的 ChatView / §2 的状态机）。 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

import Avatar from '../shared/Avatar'
import { api } from '../shared/api'
import { consumeFreshAudio } from '../shared/autoPlay'
import { shouldShowTypingBubble } from '../shared/chatRhythm'
import { CHAT_FEATURE_ENTRIES, featureMap, isFeatureOn } from '../shared/features'
import { canAutoPlay, onPlaybackChange, ownsPlayback } from '../shared/playback'
import { IconImage, IconMic } from '../shared/icons'
import LifeBadge from '../shared/LifeBadge'
import SaveImageButton from '../shared/SaveImage'
import { withWidth } from '../shared/imgUrl'
import { MarkdownText } from '../shared/MarkdownText'
import { ACCENT, stripMoodMarks } from '../shared/ui'
import { useInputHistory } from '../shared/useInputHistory'
import type { FeatureInfo, Friend, Message, Source } from '../shared/api'
import type { StreamState } from '../shared/useChat'
import type { AudioSegmentEvent } from '../shared/useWs'

const MODE_LABEL: Record<string, string> = {
  normal: '普通',
  roleplay: '入戏',
  tool: '工具',
}

function timeOf(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function fmtSeconds(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

/** 同时只放一条语音（D86 第 3 条）：新的开始播就把上一条停掉。 */
let currentAudio: HTMLAudioElement | null = null
function playExclusive(el: HTMLAudioElement): Promise<void> {
  if (currentAudio && currentAudio !== el) currentAudio.pause()
  currentAudio = el
  return el.play()
}

/**
 * 助手气泡下面的语音条（v2 / T2.0-5 / D85 / D86）。
 *
 * 四种样子：`ready` 播放条 / `pending` 正在合成 / `failed` 重新合成 /
 * 没有音频 → 淡淡的「念给我听」。**正文一个字都不收**——音频是附在文字上的。
 *
 * 自动播放（D86）：这条**刚从 `pending` 变 `ready`** 就自动播（历史里带出来的不播）；
 * 手动点「念给我听」拿到的也自动播。被浏览器拦了**静默失败**，不报错。
 */
function VoiceBar({
  friendId,
  message,
  segments,
}: {
  friendId: string
  message: Message
  segments: AudioSegmentEvent[]
}) {
  const [playing, setPlaying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [currentMs, setCurrentMs] = useState(0)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [nothingToSay, setNothingToSay] = useState(false)
  const audioRef = useRef<HTMLAudioElement>(null)
  const autoPlayed = useRef(false)
  // v2.2（D107）：分段语音 —— 一段好了就先播一段（普通聊天也是这套，文字照旧整条流式）
  const parts = useMemo(
    () =>
      segments
        .filter((item) => item.messageId === message.id && item.audioUrl)
        .sort((a, b) => a.index - b.index),
    [segments, message.id],
  )
  const playlist = parts.length > 0
  const [activeIndex, setActiveIndex] = useState(0)
  const wantPlay = useRef(false)

  const status = problem
    ? 'failed'
    : message.audio_status === 'ready' && !message.audio_url
      ? 'failed'
      : message.audio_status
  const hint = problem ?? message.audio_error ?? ''
  const effective = playlist ? 'ready' : status
  const totalMs = playlist
    ? parts.reduce((sum, item) => sum + (item.audioDurationMs ?? 0), 0)
    : message.audio_duration_ms ?? 0
  const elapsedMs = playlist
    ? parts
        .slice(0, activeIndex)
        .reduce((sum, item) => sum + (item.audioDurationMs ?? 0), 0) + currentMs
    : currentMs

  /** 从某一段开始接着播（后面一段接一段，不用等整条）。 */
  function startPlayback(index = 0) {
    const el = audioRef.current
    if (!el) return
    wantPlay.current = true
    const part = parts[index]
    if (part?.audioUrl) {
      el.src = part.audioUrl
      void playExclusive(el).catch(() => undefined)
    }
    setActiveIndex(index)
  }

  // 当前段播完了 → 接着下一段；没了就收工
  function finishPart() {
    setCurrentMs(0)
    setProgress(0)
    if (playlist && activeIndex + 1 < parts.length) {
      setActiveIndex(activeIndex + 1)
      return
    }
    wantPlay.current = false
    setPlaying(false)
    setActiveIndex(0)
  }

  // 换段：音频源跟着换，正在播就继续播
  useEffect(() => {
    const el = audioRef.current
    if (!playlist || !el) return
    const part = parts[activeIndex]
    if (!part?.audioUrl) return
    if (el.getAttribute('src') !== part.audioUrl) el.src = part.audioUrl
    if (wantPlay.current) void playExclusive(el).catch(() => undefined)
  }, [activeIndex, playlist, parts])

  // v3.14（D182）：沉浸/通话开着、或窗口不在前台时**先别抢** —— 让那一屏/桌宠去放。
  // 被抢走播放权（比如刚打开沉浸模式）时，把手里的也停下来。
  useEffect(
    () =>
      onPlaybackChange(() => {
        if (ownsPlayback('chat')) return
        wantPlay.current = false
        audioRef.current?.pause()
      }),
    [],
  )

  // 第一段一好就自动播（只对"刚生成的"生效，历史里带出来的保持安静）
  useEffect(() => {
    if (!playlist || autoPlayed.current) return
    if (!canAutoPlay('chat')) return
    if (!consumeFreshAudio(message.id)) return
    autoPlayed.current = true
    startPlayback(0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playlist, parts, message.id])

  // D86 + D88：**刚生成的**就自动播；历史消息（刷新后带出来的）保持安静
  useEffect(() => {
    if (playlist || status !== 'ready' || autoPlayed.current) return
    if (!canAutoPlay('chat')) return
    if (!consumeFreshAudio(message.id)) return // 领不到 = 历史里的 → 不播
    autoPlayed.current = true
    const el = audioRef.current
    if (!el) return
    void playExclusive(el).catch(() => undefined) // 被浏览器拦了就算了，点一下就能播
  }, [status, message.id, playlist])

  async function ask(force: boolean) {
    if (busy) return
    setBusy(true)
    setProblem(null)
    setNotice(null)
    setNothingToSay(false)
    try {
      const result = await api.speakMessage(friendId, message.id, force)
      if (!result.ok) {
        setNotice(result.hint ?? '现在配不了')
        setNothingToSay(Boolean(result.nothing_to_say))
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function toggle() {
    const el = audioRef.current
    if (!el) return
    if (playing) {
      wantPlay.current = false
      el.pause()
      return
    }
    if (playlist) {
      startPlayback(activeIndex)
      return
    }
    void playExclusive(el).catch(() => setProblem('这段音频播不出来（文件可能被删了）'))
  }

  return (
    <div className="mt-1 flex items-center gap-2 pl-1">
      {effective === 'pending' && (
        <span className="flex items-center gap-1.5 rounded-full border border-line/70 bg-surface/60 px-2.5 py-1 text-[10.5px] text-faint">
          <span className="flex gap-0.5">
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="h-1 w-1 animate-bounce rounded-full bg-stone-300"
                style={{ animationDelay: `${i * 120}ms` }}
              />
            ))}
          </span>
          正在合成…
        </span>
      )}

      {effective === 'ready' && (
        <>
          <audio
            ref={audioRef}
            src={(playlist ? parts[activeIndex]?.audioUrl : message.audio_url) ?? undefined}
            preload="none"
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onEnded={finishPart}
            onTimeUpdate={(e) => {
              const el = e.currentTarget
              setCurrentMs(el.currentTime * 1000)
              const total = totalMs || (el.duration || 0) * 1000
              const done =
                playlist && total
                  ? (elapsedMs - currentMs + el.currentTime * 1000) / total
                  : total
                    ? (el.currentTime * 1000) / total
                    : 0
              setProgress(Math.min(1, Math.max(0, done)))
            }}
            onError={() => {
              setPlaying(false)
              setProblem('这段音频播不出来（文件可能被删了）')
            }}
          />
          <button
            type="button"
            onClick={toggle}
            title={playing ? '暂停' : '播放'}
            className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-line-strong/80 bg-surface/70 text-accent transition-colors hover:border-accent/60"
          >
            {playing ? (
              <svg width="9" height="9" viewBox="0 0 10 10" fill="currentColor" aria-hidden>
                <rect x="1.6" y="1.2" width="2.4" height="7.6" rx="0.7" />
                <rect x="6" y="1.2" width="2.4" height="7.6" rx="0.7" />
              </svg>
            ) : (
              <svg width="9" height="9" viewBox="0 0 10 10" fill="currentColor" aria-hidden>
                <path d="M2.4 1.4 8.4 5l-6 3.6z" />
              </svg>
            )}
          </button>
          <div className="h-1 w-24 overflow-hidden rounded-full bg-stone-200/80">
            <div
              className="h-full rounded-full"
              style={{ width: `${Math.round(progress * 100)}%`, background: ACCENT }}
            />
          </div>
          <span className="text-[10.5px] tabular-nums text-faint">
            {fmtSeconds(currentMs || message.audio_duration_ms || 0)}
          </span>
        </>
      )}

      {status === 'failed' && (
        <>
          <span className="text-[10.5px] text-faint" title={hint}>
            语音没合成出来
          </span>
          <button
            type="button"
            onClick={() => void ask(true)}
            disabled={busy}
            className="rounded-full border border-line-strong/80 px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
          >
            {busy ? '处理中…' : '重新合成'}
          </button>
        </>
      )}

      {!status && message.content.trim() !== '' && (
        <>
          {notice && <span className="text-[10.5px] text-faint">{notice}</span>}
          {!nothingToSay && (
            <button
              type="button"
              onClick={() => void ask(false)}
              disabled={busy}
              title="让她把这条念出来"
              className="rounded-full px-2 py-0.5 text-[10.5px] text-faint opacity-70 transition-opacity hover:text-accent hover:opacity-100 disabled:opacity-40"
            >
              {busy ? '处理中…' : '念给我听'}
            </button>
          )}
        </>
      )}
    </div>
  )
}

function Bubble({
  role,
  content,
  time,
  mode,
  streaming,
  interrupted,
  error,
  imageUrl,
  onOpenImage,
  sources,
  avatarUrl,
  avatarName,
  showAvatar = true,
}: {
  role: 'user' | 'assistant'
  content: string
  time?: string
  mode?: string | null
  streaming?: boolean
  interrupted?: boolean
  error?: boolean
  imageUrl?: string | null
  onOpenImage?: (url: string) => void
  sources?: Source[] | null
  /** D125：消息前面带头像（QQ 那种）——她的用她的头像，我的用占位 */
  avatarUrl?: string | null
  avatarName?: string
  showAvatar?: boolean
}) {
  const mine = role === 'user'
  // D113：历史消息里可能还留着 `**生气**：` 这种剧本标签 —— 显示时兜一层，别让用户看到
  // 2026-10-02：**流式（streaming）那段不再二次摘除** —— 服务端给的 `content` 已经是摘过标签的
  // 干净文本（MoodTagParser），前端再摘一遍纯属多余，而且"边收边摘"最容易把正文吃掉（用户实测：
  // 列表预览正常、只有流式缺字）。历史消息仍走保守规则兜底（老消息里可能还留标签）。
  const shown = mine ? content : streaming ? content : stripMoodMarks(content)
  const avatar = (
    <div className="mt-0.5 shrink-0">
      <Avatar name={avatarName || '?'} url={avatarUrl ?? undefined} size={34} />
    </div>
  )
  return (
    <div className={`flex items-start gap-2 ${mine ? 'justify-end' : 'justify-start'}`}>
      {!mine && showAvatar && avatar}
      <div className={`max-w-[70%] ${mine ? 'items-end' : 'items-start'} flex flex-col`}>
        {imageUrl && (
          <button
            type="button"
            onClick={() => onOpenImage?.(imageUrl)}
            className="mb-1 block overflow-hidden rounded-2xl border border-line/70"
            title="点开看大图"
          >
            <img
              src={withWidth(imageUrl, 1200)}
              alt="发的图"
              className="max-h-64 w-auto max-w-full object-cover"
            />
          </button>
        )}
        <div
          className={`select-text animate-fade-up whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-[13.5px] leading-relaxed ${
            mine
              ? 'rounded-br-md bg-avatar text-white'
              : error
                ? 'rounded-bl-md border border-red-200 bg-red-50 text-red-800'
                : 'rounded-bl-md border border-line/70 bg-surface/80 text-ink'
          }`}
        >
          {shown ? (
            <MarkdownText text={shown} tone={mine ? 'inverse' : 'normal'} />
          ) : streaming ? (
            ''
          ) : (
            '（空）'
          )}
          {streaming && (
            <span
              className="ml-0.5 inline-block h-3.5 w-[2px] translate-y-[2px] animate-pulse"
              style={{ background: ACCENT }}
              aria-hidden
            />
          )}
          {interrupted && <span className="ml-1 text-[11px] text-faint">（未说完）</span>}
        </div>
        {sources && sources.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {sources.map((item) => (
              <a
                key={item.url}
                href={item.url}
                target="_blank"
                rel="noreferrer"
                title={item.title}
                className="max-w-[200px] truncate rounded-full border border-line/70 bg-surface/60 px-2 py-0.5 text-[10px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
              >
                [{item.no}] {item.title}
              </a>
            ))}
          </div>
        )}
        <div
          className={`mt-1 flex items-center gap-1.5 px-1 text-[10.5px] text-faint ${
            mine ? 'justify-end' : 'justify-start'
          }`}
        >
          {time && <span>{time}</span>}
          {mode && mode !== 'normal' && (
            <span className="rounded bg-chip px-1 text-[10px] text-muted">
              {MODE_LABEL[mode] ?? mode}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * 工具调用的显示（对话框里那个蓝块）。
 *
 * 2026-09-27 修：以前只画"流式进行中"的 `stream.tools`，**聊完/刷新之后就看不见了** ——
 * 用户实测"我让小纸条记了，但没看到工具调用"，就是因为这个。
 * 现在已落库的 `message.tools_used`（JSON 字符串）也一起画。
 */
function ToolCalls({ tools }: { tools: { id: string; name: string; args?: unknown }[] }) {
  if (tools.length === 0) return null
  return (
    <div className="mt-1 max-w-[70%] rounded-xl border border-blue-200/60 bg-blue-50/60 px-3 py-1.5 font-mono text-[10.5px] text-blue-800">
      {tools.map((t) => (
        <div key={t.id} className="truncate">
          ⚙ {t.name}({JSON.stringify(t.args ?? {})})
        </div>
      ))}
    </div>
  )
}

/** `message.tools_used` 是 JSON 字符串（后端落库的原样记录）；解析失败就当没有。 */
function parseTools(
  raw: string | null | undefined,
): { id: string; name: string; args?: unknown }[] {
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.map((item, index) => {
      const row = (item ?? {}) as Record<string, unknown>
      return {
        id: String(row.id ?? `t${index}`),
        name: String(row.name ?? '?'),
        args: row.args ?? {},
      }
    })
  } catch {
    return []
  }
}


function ModePicker({
  friend,
  onSwitch,
}: {
  friend: Friend
  onSwitch: (mode: string, role: string | null) => void
}) {
  const [open, setOpen] = useState(false)
  const [role, setRole] = useState(friend.current_role ?? '')

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="rounded-full border border-line/80 bg-surface/70 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
      >
        {MODE_LABEL[friend.current_mode] ?? friend.current_mode}
        {friend.current_mode === 'roleplay' && friend.current_role ? `·${friend.current_role}` : ''}
      </button>
      {open && (
        <div className="absolute left-0 top-7 z-30 w-52 rounded-xl border border-line bg-surface/95 p-2 shadow-lg backdrop-blur">
          <div className="flex gap-1">
            {(['normal', 'roleplay', 'tool'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => {
                  onSwitch(mode, mode === 'roleplay' ? role.trim() || null : null)
                  setOpen(false)
                }}
                className={`flex-1 rounded-lg px-2 py-1 text-[11px] transition-colors ${
                  friend.current_mode === mode
                    ? 'text-white'
                    : 'text-muted hover:bg-surface-hover'
                }`}
                style={friend.current_mode === mode ? { background: ACCENT } : undefined}
              >
                {MODE_LABEL[mode]}
              </button>
            ))}
          </div>
          <input
            className="mt-1.5 w-full rounded-lg border border-line px-2 py-1 text-[11px] outline-none focus:border-accent/60"
            placeholder="入戏时扮演谁（可留空）"
            value={role}
            onChange={(e) => setRole(e.target.value)}
          />
          <p className="mt-1 px-1 text-[10px] leading-tight text-faint">
            用户强切立即生效，不弹确认（宪法§2）。入戏后要出戏，说「好了」或点这里切回普通。
          </p>
        </div>
      )}
    </div>
  )
}

export default function ChatView({
  friend,
  messages,
  stream,
  hasMore,
  showToolCalls,
  visionEnabled,
  features,
  onSend,
  onStop,
  onLoadOlder,
  onSwitchMode,
  onOpenMemories,
  onOpenSchedules,
  onEditPersonality,
  onOpenKnowledge,
  onLookScreen,
  onOpenImmersive,
  onOpenSpace,
  onOpenPet,
  onOpenCall,
  voiceSegments,
}: {
  friend: Friend
  messages: Message[]
  stream?: StreamState
  hasMore: boolean
  showToolCalls: boolean
  visionEnabled: boolean
  /** v7.20（D373）：功能开关快照；null = 还没加载好（按全开处理）。 */
  features: FeatureInfo[] | null
  onSend: (text: string, imageId?: string, previewUrl?: string) => void
  onStop: () => void
  onLoadOlder: () => void | Promise<void>
  onSwitchMode: (mode: string, role: string | null) => void
  onOpenMemories: () => void
  onOpenSchedules: () => void
  onEditPersonality: () => void
  onOpenKnowledge: () => void
  onLookScreen: (draft: string) => void
  onOpenImmersive: () => void
  /** 小世界：3D 房间原型 */
  onOpenSpace: () => void
  /** v2.2（D119）：把当前这个朋友丢进桌宠小窗 */
  onOpenPet: () => void
  /** v2.3（D126）：语音通话（不显示文字） */
  onOpenCall: () => void
  /** v2.2（D107）：这个朋友的分段语音（普通聊天也"一段好就播一段"） */
  voiceSegments: AudioSegmentEvent[]
}) {
  const [draft, setDraft] = useState('')
  const [attachment, setAttachment] = useState<{ file: File; previewUrl: string } | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [lightbox, setLightbox] = useState<string | null>(null)
  const [saveHint, setSaveHint] = useState<string | null>(null)
  // v2.1（T2.1-2）按住说话；D124 改成**识别完直接发送**（不再落进输入框）
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [speechHint, setSpeechHint] = useState<string | null>(null)
  const [docNotice, setDocNotice] = useState<string | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  // 修「点『看更早的』被拽到最新」：顶部插入旧消息时，记录插入前的滚动位置，
  // 插入后用高度差把滚动条补回原地（视觉上停在原来那条消息上）。
  const scrollBoxRef = useRef<HTMLDivElement>(null)
  const olderAnchorRef = useRef<{ top: number; height: number } | null>(null)
  // D449：桌面聊天框 ↑ 调出上一条已发送的文字（图片-only 消息不算）。
  const sentHistory = useMemo(
    () =>
      messages
        .filter((message) => message.role === 'user' && message.content.trim())
        .map((message) => message.content),
    [messages],
  )
  const inputHistory = useInputHistory(sentHistory)
  const { reset: resetInputHistory, onArrow: onInputArrow } = inputHistory
  const status = stream?.status ?? 'idle'
  const busy = status !== 'idle' && status !== 'error' && status !== 'partial'
  const featureById = useMemo(() => featureMap(features), [features])
  const canImage = isFeatureOn(featureById, 'image')
  const canKnowledge = isFeatureOn(featureById, 'knowledge')

  // 用 useLayoutEffect：在浏览器绘制前就把滚动位置补回去，避免先跳到底再弹回来。
  useLayoutEffect(() => {
    const box = scrollBoxRef.current
    const anchor = olderAnchorRef.current
    if (box && anchor) {
      olderAnchorRef.current = null
      box.scrollTop = anchor.top + (box.scrollHeight - anchor.height)
      return
    }
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [messages, stream?.content, stream?.reasoning, status])

  // 换朋友时别把上一个朋友的消息当成这个朋友的输入历史。
  useEffect(() => {
    resetInputHistory()
  }, [friend.id, resetInputHistory])

  function loadOlderKeepingScroll() {
    const box = scrollBoxRef.current
    if (box) olderAnchorRef.current = { top: box.scrollTop, height: box.scrollHeight }
    void onLoadOlder()
  }

  function attach(file: File) {
    if (!canImage) {
      setUploadError('「看图与画图」这个功能现在是关着的 —— 去设置 → 功能里打开再用')
      return
    }
    if (!file.type.startsWith('image/')) {
      setUploadError('只能发图片（PNG / JPG / WEBP）')
      return
    }
    setUploadError(null)
    setAttachment({ file, previewUrl: URL.createObjectURL(file) })
  }

  async function startRecording() {
    if (transcribing || recording) return
    setSpeechHint(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const recorder = new MediaRecorder(stream)
      chunksRef.current = []
      recorder.ondataavailable = (event) => {
        if (event.data.size) chunksRef.current.push(event.data)
      }
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop())
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        void submitAudio(blob)
      }
      recorderRef.current = recorder
      recorder.start()
      setRecording(true)
    } catch {
      setSpeechHint('拿不到麦克风权限（桌面外壳可能拦着）—— 先用浏览器打开，或者告诉我我去放行')
    }
  }

  function stopRecording() {
    const recorder = recorderRef.current
    if (recorder && recorder.state !== 'inactive') recorder.stop()
    recorderRef.current = null
    setRecording(false)
  }

  async function submitAudio(blob: Blob) {
    setTranscribing(true)
    try {
      const file = new File([blob], 'rec.webm', { type: blob.type || 'audio/webm' })
      const result = await api.transcribeAudio(file)
      if (!result.ok) {
        setSpeechHint(result.hint ?? '没识别出来')
        return
      }
      const text = (result.text ?? '').trim()
      if (!text) {
        setSpeechHint('没听清（再说一次试试）')
        return
      }
      // 用户口径（D124）：**语音输入识别完直接发** —— 不再落进输入框。
      // （代价是识别错了没机会改；用户明确说"不然这个功能意义不大"。）
      // 输入框里已经打了字的话，把它们拼在一起发出去（等于"接着说完"）。
      const merged = draft.trim() ? `${draft.trim()}${text}` : text
      setSpeechHint(`说了：${text}`)
      setDraft('')
      onSend(merged)
    } catch (e) {
      setSpeechHint(e instanceof Error ? e.message : String(e))
    } finally {
      setTranscribing(false)
    }
  }

  /** v2.1（T2.1-3）：拖进来的**非图片**文件 → 当成资料加进她的知识库（D98）。 */
  async function addDocument(file: File) {
    setDocNotice('正在读这份资料…')
    try {
      const doc = await api.uploadDocument(friend.id, file)
      setDocNotice(
        doc.status === 'ready'
          ? `《${doc.name}》已加进她的知识库（${doc.chars} 字）`
          : `《${doc.name}》${doc.error ?? '没读出文字'}`,
      )
    } catch (e) {
      setDocNotice(e instanceof Error ? e.message : String(e))
    }
  }

  async function submit() {
    const text = draft.trim()
    if ((!text && !attachment) || busy || uploading) return
    let imageId: string | undefined
    let previewUrl: string | undefined
    if (attachment) {
      setUploading(true)
      setUploadError(null)
      try {
        const info = await api.uploadImage(attachment.file)
        imageId = info.id
        previewUrl = attachment.previewUrl
      } catch (e) {
        setUploadError(e instanceof Error ? e.message : String(e))
        setUploading(false)
        return
      }
      setUploading(false)
    }
    setDraft('')
    setAttachment(null)
    resetInputHistory()
    onSend(text, imageId, previewUrl)
  }

  return (
    <div
      className="relative flex h-full flex-1 flex-col"
      onDragOver={(e) => {
        e.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragging(false)
        const file = e.dataTransfer.files?.[0]
        if (!file) return
        if (file.type.startsWith('image/')) attach(file)
        else if (canKnowledge) void addDocument(file)
        else setDocNotice('「知识库」这个功能现在是关着的 —— 去设置 → 功能里打开再发文件')
      }}
    >
      {dragging && (
        <div className="pointer-events-none absolute inset-2 z-30 flex items-center justify-center rounded-2xl border-2 border-dashed border-accent/60 bg-page/70 text-[12.5px] text-accent">
          松手就把图 / 资料给她（图进聊天，其它文件进她的知识库）
        </div>
      )}
      <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line/60 px-4">
        <Avatar name={friend.name} url={friend.avatar_url} size={32} />
        <span className="text-[13.5px] font-medium text-ink">{friend.name}</span>
        <ModePicker friend={friend} onSwitch={onSwitchMode} />
        <button
          type="button"
          onClick={onEditPersonality}
          title="改名字 / 人格"
          className="rounded-full border border-line/80 bg-surface/70 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
        >
          人格
        </button>
        <button
          type="button"
          onClick={onOpenMemories}
          className="rounded-full border border-line/80 bg-surface/70 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
        >
          记忆
        </button>
        {/* v7.20（D373）：聊天页入口由共享清单生成，关掉的功能不渲染。
            加一个新入口只改 `shared/features.ts`，不再往这里复制按钮。 */}
        {CHAT_FEATURE_ENTRIES.filter((entry) =>
          isFeatureOn(featureById, entry.featureId),
        ).map((entry) => (
          <button
            key={entry.id}
            type="button"
            title={entry.title}
            onClick={() => {
              if (entry.id === 'schedule') onOpenSchedules()
              else if (entry.id === 'knowledge') onOpenKnowledge()
              else if (entry.id === 'screen') onLookScreen(draft)
              else if (entry.id === 'immersive') onOpenImmersive()
              else if (entry.id === 'space') onOpenSpace()
              else if (entry.id === 'call') onOpenCall()
              else if (entry.id === 'pet') onOpenPet()
            }}
            className="rounded-full border border-line/80 bg-surface/70 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
          >
            {entry.label}
          </button>
        ))}
        <span className="ml-auto flex items-center gap-2 text-[11px] text-faint">
          <LifeBadge friendId={friend.id} />
          {friend.presence ? '在场' : '不在场'}
        </span>
      </header>

      <div ref={scrollBoxRef} className="min-w-0 flex-1 overflow-x-hidden overflow-y-auto px-5 py-4">
        {hasMore && (
          <div className="mb-3 flex justify-center">
            <button
              type="button"
              onClick={loadOlderKeepingScroll}
              className="rounded-full border border-line bg-surface/70 px-3 py-1 text-[11px] text-muted hover:border-accent/50 hover:text-accent"
            >
              看更早的
            </button>
          </div>
        )}

        {messages.length === 0 && status === 'idle' && (
          <div className="flex h-full flex-col items-center justify-center gap-1 text-center">
            <p className="text-[13px] text-muted">跟 {friend.name} 说点什么吧</p>
            <p className="text-[11px] text-faint">它记得你们聊过的事</p>
          </div>
        )}

        <div className="flex flex-col gap-3">
          {messages.map((m) => (
            <div key={m.id} className="flex flex-col">
              {m.role === 'assistant' && (m.segments?.length ?? 0) > 1 ? (
                <div className="flex flex-col gap-1.5">
                  {m.segments!.map((segment, index) => (
                    <Bubble
                      key={`${m.id}-${index}`}
                      role={m.role}
                      content={segment}
                      time={index === 0 ? timeOf(m.created_at) : undefined}
                      mode={m.mode_at_send}
                      interrupted={index === m.segments!.length - 1 && m.stream_status === 'partial'}
                      error={m.stream_status === 'error'}
                      imageUrl={index === 0 ? m.image_url : null}
                      onOpenImage={setLightbox}
                      sources={index === m.segments!.length - 1 ? m.sources : null}
                      avatarUrl={friend.avatar_url}
                      avatarName={friend.name}
                    />
                  ))}
                </div>
              ) : (
                <Bubble
                  role={m.role}
                  content={m.content}
                  time={timeOf(m.created_at)}
                  mode={m.mode_at_send}
                  interrupted={m.stream_status === 'partial'}
                  error={m.stream_status === 'error'}
                  imageUrl={m.image_url}
                  onOpenImage={setLightbox}
                  sources={m.sources}
                  avatarUrl={m.role === 'assistant' ? friend.avatar_url : null}
                  avatarName={m.role === 'assistant' ? friend.name : undefined}
                />
              )}
              {m.role === 'assistant' && (
                <VoiceBar friendId={friend.id} message={m} segments={voiceSegments} />
              )}
              {/* 已落库的工具调用也要看得见（不然聊完/刷新后就没影了） */}
              {showToolCalls && m.role === 'assistant' && <ToolCalls tools={parseTools(m.tools_used)} />}
            </div>
          ))}

          {status !== 'idle' && (
            <div className="flex flex-col gap-1.5">
              {status === 'thinking' && stream?.reasoning && (
                <details className="max-w-[70%] rounded-xl border border-line/70 bg-surface/60 px-3 py-1.5 text-[11px] text-muted">
                  <summary className="cursor-pointer select-none">正在思考…</summary>
                  <p className="mt-1 whitespace-pre-wrap leading-relaxed">{stream.reasoning}</p>
                </details>
              )}

              {showToolCalls && stream && stream.tools.length > 0 && (
                <ToolCalls tools={stream.tools} />
              )}

              {(status === 'streaming' || status === 'tool_running' || status === 'thinking') && (
                <div className="flex flex-col gap-1.5">
                  {(() => {
                    const rawSegments = stream?.segments?.length
                      ? stream.segments
                      : [stream?.content ?? '']
                    const visibleCount = stream?.segments?.length
                      ? stream.visibleSegmentCount
                      : rawSegments.length
                    const visibleSegments = rawSegments
                      .slice(0, visibleCount)
                      .filter((segment) => segment.trim() !== '')
                    const hasHiddenBubble = rawSegments.length > visibleCount
                    const showTyping = shouldShowTypingBubble(rawSegments.length, visibleCount)
                    return (
                      <>
                        {visibleSegments.map((segment, index) => (
                          <Bubble
                            key={`stream-${index}`}
                            role="assistant"
                            content={segment}
                            streaming={!hasHiddenBubble && index === visibleSegments.length - 1}
                            avatarUrl={friend.avatar_url}
                            avatarName={friend.name}
                          />
                        ))}
                        {showTyping && (
                          <Bubble
                            role="assistant"
                            content="正在输入中..."
                            avatarUrl={friend.avatar_url}
                            avatarName={friend.name}
                          />
                        )}
                      </>
                    )
                  })()}
                </div>
              )}

              {status === 'error' && (
                <div className="flex max-w-[70%] flex-col gap-1.5">
                  <Bubble role="assistant" content={stream?.error ?? '出错了'} error time="" avatarUrl={friend.avatar_url} avatarName={friend.name} />
                  <button
                    type="button"
                    onClick={() => stream?.lastUserText && onSend(stream.lastUserText)}
                    className="self-start rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted hover:border-accent/60 hover:text-accent"
                  >
                    重试
                  </button>
                </div>
              )}

              {status === 'partial' && (
                <p className="px-1 text-[11px] text-faint">（连接断了，这段没说完）</p>
              )}
            </div>
          )}
        </div>
        <div ref={bottomRef} />
      </div>

      <div className="shrink-0 border-t border-line/60 bg-surface/40 px-4 py-3 backdrop-blur">
        {attachment && (
          <div className="mb-2 flex items-center gap-2">
            <div className="relative">
              <img
                src={attachment.previewUrl}
                alt="待发送的图"
                className="h-16 w-16 rounded-xl border border-line/70 object-cover"
              />
              <button
                type="button"
                onClick={() => setAttachment(null)}
                className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full border border-line bg-page text-[11px] text-muted hover:text-red-500"
                title="不发这张"
              >
                ✕
              </button>
            </div>
            <span className="text-[10.5px] text-faint">
              {uploading ? '正在上传…' : '发送时会带上这张图'}
            </span>
            {!visionEnabled && (
              <span className="text-[10.5px] text-amber-600">
                现在这个模型看不了图 —— 去「设置 → 模型 → 视觉」配一个
              </span>
            )}
          </div>
        )}
        {uploadError && <p className="mb-1 text-[11px] text-red-600">{uploadError}</p>}
        {(recording || transcribing || speechHint || docNotice) && (
          <p className="mb-1 text-[11px] text-faint">
            {recording
              ? '正在听…（松手结束）'
              : transcribing
                ? '正在识别…'
                : (speechHint ?? docNotice)}
          </p>
        )}
        <div className="flex items-end gap-2">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              e.target.value = '' // 同一张图也要能再选
              if (file) attach(file)
            }}
          />
          {canImage && (
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              title="发张图（也可以直接 Ctrl+V 粘贴 / 拖进来）"
              className="flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-2xl border border-line bg-surface/80 text-muted transition-colors hover:border-accent/50 hover:text-accent"
            >
              <IconImage size={15} />
            </button>
          )}
          <button
            type="button"
            onPointerDown={(e) => {
              e.preventDefault()
              void startRecording()
            }}
            onPointerUp={(e) => {
              e.preventDefault()
              stopRecording()
            }}
            onPointerLeave={() => {
              if (recording) stopRecording()
            }}
            disabled={transcribing}
            title="按住说话（松手识别，识别完直接发出去）"
            className={`flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-2xl border transition-colors disabled:opacity-40 ${
              recording
                ? 'border-red-400 bg-red-50 text-red-500'
                : 'border-line bg-surface/80 text-muted hover:border-accent/50 hover:text-accent'
            }`}
          >
            <IconMic size={15} />
          </button>
          <textarea
            ref={textareaRef}
            className="max-h-32 min-h-[38px] flex-1 resize-none rounded-2xl border border-line bg-surface/80 px-3 py-2 text-[13.5px] leading-relaxed outline-none transition focus:border-accent/60"
            placeholder={`和 ${friend.name} 说点什么…（Enter 发送 / Shift+Enter 换行）`}
            value={draft}
            rows={1}
            onChange={(e) => {
              setDraft(e.target.value)
              resetInputHistory()
            }}
            onPaste={(e) => {
              const item = Array.from(e.clipboardData.items).find((i) =>
                i.type.startsWith('image/'),
              )
              const file = item?.getAsFile()
              if (file) {
                e.preventDefault() // 别把图的路径当文字粘进去
                attach(file)
              }
            }}
            onKeyDown={(e) => {
              if (
                onInputArrow(draft, e, (text) => {
                  setDraft(text)
                  requestAnimationFrame(() => {
                    const el = textareaRef.current
                    if (el) el.setSelectionRange(text.length, text.length)
                  })
                })
              ) {
                return
              }
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
          />
          {busy ? (
            <button
              type="button"
              onClick={onStop}
              className="h-[38px] shrink-0 rounded-2xl border border-line-strong bg-surface/70 px-4 text-[12.5px] text-muted hover:border-stone-400"
            >
              停止
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void submit()}
              disabled={(!draft.trim() && !attachment) || uploading}
              className="h-[38px] shrink-0 rounded-2xl px-4 text-[12.5px] font-medium text-white transition-opacity disabled:opacity-35"
              style={{ background: ACCENT }}
            >
              {uploading ? '上传中…' : '发送'}
            </button>
          )}
        </div>
      </div>

      {lightbox && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-6"
          onClick={() => setLightbox(null)}
          title="点一下关掉"
        >
          <div className="flex flex-col items-center gap-3" onClick={(event) => event.stopPropagation()}>
            <img
              src={withWidth(lightbox, 1800)}
              alt="大图"
              className="max-h-[80vh] max-w-full rounded-xl shadow-2xl"
            />
            <div className="flex gap-2">
              <SaveImageButton
                url={lightbox}
                onDone={setSaveHint}
                className="rounded-full border border-white/30 bg-black/50 px-4 py-1.5 text-[12.5px] text-white transition-colors hover:border-accent/70"
              />
              <button
                type="button"
                onClick={() => setLightbox(null)}
                className="rounded-full border border-white/30 bg-black/50 px-4 py-1.5 text-[12.5px] text-white transition-colors hover:border-accent/70"
              >
                关闭
              </button>
            </div>
            {saveHint && <p className="max-w-[520px] text-center text-[12px] text-white/80">{saveHint}</p>}
          </div>
        </div>
      )}
    </div>
  )
}
