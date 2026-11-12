/**
 * 手机壳的第二屏：某个朋友的聊天（v3.0 / **B4**）。
 *
 * 这一屏里有什么（用户点名要的）：
 *   · 气泡（她左边带头像 / 我右边）+ 图片（点开大图）+ 语音（▶ 回放，没文件给"重新合成"）
 *   · 输入区：文字 + 发送；**一个相机按钮**（点一下弹"拍摄 / 相册"，D164）；**按住说话**（识别完直接发，D124）
 *   · 流式：她说的时候边流边出，带一个停止按钮
 *   · 右上角「⋯」：沉浸 / 通话 / 日程 / 人格 / 知识库 / 日志 的入口
 *（沉浸 / 通话 / 日程 / 人格 / 知识库 / 日志 —— 菜单里**全是真的**，没有点不动的空按钮。）
 *  图片这件事**只在聊天里做**（D164）：发图给她、或让她画（她自己会调 `draw_image`）；
 *  手机端没有独立的"画图 / 图生图"面板。
 *
 * 复用的东西：`useChat`（流式状态机）、`api`（都在 shared 里）—— 这一屏**不重写逻辑**。
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import Avatar from '../shared/Avatar'
import CallView from '../shared/CallView'
import ImmersiveView from '../shared/ImmersiveView'
import LifeBadge from '../shared/LifeBadge'
import SaveImageButton from '../shared/SaveImage'
import StreamStatusCard from '../shared/StreamStatusCard'
import SpaceView from '../space/SpaceView'
import { IconCamera } from '../shared/icons'
import { withWidth } from '../shared/imgUrl'
import { MarkdownText } from '../shared/MarkdownText'
import { api, type FriendDetail, type Message } from '../shared/api'
import { shouldShowTypingBubble } from '../shared/chatRhythm'
import { useChat } from '../shared/useChat'
import { ACCENT, stripMoodMarks } from '../shared/ui'
import { useProactiveFeed, type AudioSegmentEvent } from '../shared/useWs'
import { useMyName } from './useMyName'

const QUICK_ENTRIES = [
  { key: 'immersive', label: '沉浸模式' },
  { key: 'call', label: '语音通话' },
  { key: 'schedule', label: '日程' },
  { key: 'persona', label: '人格 / 音色' },
  { key: 'knowledge', label: '知识库' },
  { key: 'space', label: '小世界' },
  { key: 'log', label: '日志' },
] as const

/** 跟桌面同一套口径（D146）：手机顶栏只**显示**，不做切换。 */
const MODE_LABEL: Record<string, string> = {
  normal: '普通',
  roleplay: '入戏',
  tool: '工具',
}

function modeText(friend: FriendDetail | null): string {
  if (!friend) return ''
  const label = MODE_LABEL[friend.current_mode] ?? friend.current_mode
  return friend.current_mode === 'roleplay' && friend.current_role
    ? `${label}·${friend.current_role}`
    : label
}

/**
 * Android App 的原生桥（`MainActivity.ShellBridge`）。
 *
 * 为什么有它（2026-09-21 实测）：**同一个页面在手机浏览器里录音正常，
 * 在 App 的 WebView 里却报 NotReadableError** —— 那就绕开 WebView，
 * 用原生 MediaRecorder 录，录完由原生这边上传识别，把文字回给网页。
 */
interface YfShell {
  micPermission?: () => string
  requestMic?: () => void
  startRecording?: () => boolean
  stopAndTranscribe?: () => void
  startSystemRecognition?: () => boolean
  stopSystemRecognition?: () => void
  /** v3.7（D163）：把图存进系统相册（WebView 不认 `<a download>`，只能靠原生这一条） */
  saveImage?: (url: string) => string
  /** 小世界进入时横屏，退出时恢复。 */
  setSpaceLandscape?: (enabled: boolean) => void
}

function nativeShell(): YfShell | null {
  const shell = (window as unknown as { yfShell?: YfShell }).yfShell
  return shell && typeof shell.startRecording === 'function' ? shell : null
}

function clockTime(iso: string): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return ''
  return `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
}

function Bubble({
  message,
  friend,
  onOpenImage,
  contentOverride,
  showAvatar = true,
  hideExtras = false,
}: {
  message: Message
  friend: FriendDetail | null
  onOpenImage: (url: string) => void
  contentOverride?: string
  showAvatar?: boolean
  hideExtras?: boolean
}) {
  const mine = message.role === 'user'
  const [playing, setPlaying] = useState(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  function play() {
    if (!message.audio_url) return
    if (playing) {
      audioRef.current?.pause()
      setPlaying(false)
      return
    }
    const audio = new Audio(message.audio_url)
    audioRef.current = audio
    audio.onended = () => setPlaying(false)
    audio.onerror = () => setPlaying(false)
    void audio.play().then(() => setPlaying(true)).catch(() => setPlaying(false))
  }

  const text = stripMoodMarks(contentOverride ?? message.content)

  return (
    <div className={`flex w-full gap-2 ${mine ? 'flex-row-reverse' : 'flex-row'}`}>
      {!mine && showAvatar && (
        <Avatar name={friend?.name ?? '她'} url={friend?.avatar_url} size={34} className="mt-1" />
      )}
      <div className={`flex max-w-[78%] flex-col ${mine ? 'items-end' : 'items-start'}`}>
        {!hideExtras && message.image_url && (
          <button type="button" onClick={() => onOpenImage(message.image_url!)} className="mb-1">
            <img
              // v3.1：手机端聊天图要压（原图 0.5~1.5 MB → 气泡里几十 KB）
              src={withWidth(message.image_url, 900)}
              alt=""
              className="max-h-[260px] w-auto rounded-2xl border border-line/70 object-cover"
            />
          </button>
        )}

        {text && (
          <div
            className={`select-text whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-[15px] leading-relaxed ${
              mine
                ? 'rounded-br-md text-white'
                : 'rounded-bl-md border border-line/70 bg-surface text-ink'
            }`}
            style={mine ? { background: ACCENT } : undefined}
          >
            <MarkdownText text={text} tone={mine ? 'inverse' : 'normal'} />
          </div>
        )}

        {/* v3.2（D144）：**刷新之后**从历史里带出来的失败/打断也要有表现 ——
            不然手机上只看到一块空白（用户报的就是这个）。 */}
        {!text && !mine && message.stream_status === 'error' && (
          <div className="rounded-2xl rounded-bl-md border border-red-400/50 bg-red-500/10 px-3 py-2 text-[13px] text-red-500">
            这次没答上 —— 原因在「日志」里（右上角 ⋯ → 日志）
          </div>
        )}
        {text && !mine && message.stream_status === 'partial' && (
          <span className="mt-0.5 px-1 text-[10.5px] text-amber-500">（这段被打断了，没说完）</span>
        )}

        {!hideExtras && message.sources && message.sources.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {message.sources.map((source) => (
              <a
                key={source.no}
                href={source.url}
                target="_blank"
                rel="noreferrer"
                className="rounded-full border border-line/70 px-2 py-0.5 text-[11px] text-muted"
              >
                [{source.no}] {source.title.slice(0, 16)}
              </a>
            ))}
          </div>
        )}

        {!hideExtras && message.audio_url && (
          <button
            type="button"
            onClick={play}
            className="mt-1 flex h-9 items-center gap-1.5 rounded-full border border-line bg-surface/80 px-3 text-[12.5px] text-muted active:bg-surface-hover"
          >
            <span>{playing ? '⏸' : '▶'}</span>
            <span>{message.audio_duration_ms ? `${(message.audio_duration_ms / 1000).toFixed(1)}s` : '语音'}</span>
          </button>
        )}
        {!hideExtras && !message.audio_url && message.audio_status === 'failed' && (
          <p className="mt-1 text-[11px] text-faint">这条语音没合成成功</p>
        )}

        {!hideExtras && (
          <span className="mt-0.5 px-1 text-[10.5px] text-faint">{clockTime(message.created_at)}</span>
        )}
      </div>
    </div>
  )
}

export default function ChatScreen({
  friendId,
  onBack,
  embedded = false,
  onOpenKnowledge,
  onOpenLogs,
  onOpenPersona,
  onOpenSchedules,
}: {
  friendId: string
  onBack: () => void
  /** 平板两栏里用（D200）：右边不显示"返回"箭头（左边就是朋友列表） */
  embedded?: boolean
  onOpenKnowledge: () => void
  onOpenLogs: () => void
  onOpenPersona: () => void
  onOpenSchedules: () => void
}) {
  const [friend, setFriend] = useState<FriendDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [lightbox, setLightbox] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [sttMode, setSttMode] = useState<'cloud' | 'system'>('cloud')
  const [hint, setHint] = useState<string | null>(null)
  const [presenceBusy, setPresenceBusy] = useState(false)
  // v3.7（D166）：选完的图**先挂在这儿**（缩略图先看着），按发送时才跟着文字一起发出去
  const [attachment, setAttachment] = useState<{ file: File; previewUrl: string } | null>(null)
  const [uploading, setUploading] = useState(false)
  const [saveHint, setSaveHint] = useState<string | null>(null)
  // v3.0（B5）：沉浸 / 通话 —— 进来就是整屏，退出回到这屏。
  // 也支持直接进：`/m/#chat=<id>&immersive=1` / `&call=1`（快捷方式、将来推送都能用）
  const [immersive, setImmersive] = useState(() => /(?:^#|&)immersive=1/.test(window.location.hash))
  const [inCall, setInCall] = useState(() => /(?:^#|&)call=1/.test(window.location.hash))
  const [showSpace, setShowSpace] = useState(false)
  const [spaceEnabled, setSpaceEnabled] = useState(true)
  // 分段语音（沉浸的"一段一段说"和通话都靠它）：跟桌面同一套语义，保留最近 60 段
  const [voiceSegments, setVoiceSegments] = useState<AudioSegmentEvent[]>([])

  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const albumRef = useRef<HTMLInputElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  // 修「点『看更早的』被拽到最新」：顶部插入旧消息时按高度差补回滚动位置（同桌面端）。
  const scrollBoxRef = useRef<HTMLDivElement>(null)
  const olderAnchorRef = useRef<{ top: number; height: number } | null>(null)
  // 挂在输入区的那张图**同时**放一份 ref：语音/沉浸/通话那几条路是"长命闭包"
  // （回调注册时就把当时的值关进去了），只读 state 会漏掉后挂的图（D166）。
  const attachmentRef = useRef<{ file: File; previewUrl: string } | null>(null)

  const { messages, streams, loadHistory, loadOlder, send, stop, isStreaming } = useChat()
  // B9：沉浸模式里"我"叫什么 —— 存 DB，跟电脑一致
  const { myName, save: saveMyName } = useMyName()
  const list = messages[friendId] ?? []
  const stream = streams[friendId]
  const streaming = isStreaming(friendId)

  useEffect(() => {
    api
      .getFriend(friendId)
      .then(setFriend)
      .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : String(exc)))
    void loadHistory(friendId)
  }, [friendId, loadHistory])

  // v3.1：**把她所有情绪的立绘偷偷先下好**。
  // 用户实测"沉浸模式根本没体验"——主要是因为每换一个情绪才现拉图。
  // v3.2（D145）：改拉**原图**（用户："还是太模糊"）—— 一次拉、之后吃缓存，不再压。
  useEffect(() => {
    if (!friend) return
    let cancelled = false
    // 1) **先热连接**：一个极小的请求把 TCP/TLS 建起来，后面的图就能复用同一条连接
    //    （第一次那种"卡"很大一部分是握手 —— 用户实测："一开始卡，之后就快了"）
    void fetch('/api/health', { cache: 'no-store' }).catch(() => undefined)

    // 2) **兜底那张排最前**（她没给情绪时用的「正常/平静」）——进沉浸立刻有图，
    //    而不是排在 20 张后面干等
    const emotions = friend.portrait_emotions ?? []
    const queue = [
      ...new Set(['正常', '平静', ...emotions.filter((item) => item !== '正常' && item !== '平静')]),
    ].filter((item) => item !== undefined)

    // 3) 并发 4 路预取（别把网络/中继挤满）
    const workers = Array.from({ length: 4 }, async () => {
      while (!cancelled && queue.length) {
        const emotion = queue.shift()
        if (!emotion) break
        await new Promise<void>((resolve) => {
          const image = new Image()
          image.onload = () => resolve()
          image.onerror = () => resolve()
          image.src = `/api/friends/${friend.id}/portraits/${encodeURIComponent(emotion)}`
        })
      }
    })
    void Promise.all(workers)
    return () => {
      cancelled = true
    }
  }, [friend])

  // v3.8（T3.8-1）：**进聊天就把这个朋友的未读清零** —— 桌面端一进聊天也这么干
  // （App.tsx 里 `markRead(selectedId)`）。以前手机端压根没调过这个接口 → 红点点不掉。
  useEffect(() => {
    void api.markRead(friendId).catch(() => undefined)
  }, [friendId])

  // 电脑那边收到的消息（比如她在电脑上回你）也要出现在这屏里
  useProactiveFeed((event) => {
    if (event.friendId !== friendId) return
    // 本机流式回合自己会在节奏播完后刷新；WS 完成通知不能抢在前面把完整消息塞进来。
    if (isStreaming(friendId)) return
    void loadHistory(friendId)
    // 我正看着这个聊天 → 她这条不算未读（跟桌面一个口径）
    void api.markRead(friendId).catch(() => undefined)
  }, undefined, undefined, (event) => {
    if (event.friendId !== friendId || !event.audioUrl) return
    setVoiceSegments((prev) => {
      const key = `${event.messageId}:${event.index}`
      // v3.11（D178）：同一段会来两条（"开始合成" → "好了带时长"）→ **替换**，别跳过
      const next = [...prev.filter((item) => `${item.messageId}:${item.index}` !== key), event]
      return next.slice(-60)
    })
  })

  useLayoutEffect(() => {
    const box = scrollBoxRef.current
    const anchor = olderAnchorRef.current
    if (box && anchor) {
      olderAnchorRef.current = null
      box.scrollTop = anchor.top + (box.scrollHeight - anchor.height)
      return
    }
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [list.length, stream?.content, streaming])

  function loadOlderKeepingScroll() {
    const box = scrollBoxRef.current
    if (box) olderAnchorRef.current = { top: box.scrollTop, height: box.scrollHeight }
    void loadOlder(friendId)
  }

  // 原生录音的识别结果回来了（Android App 那条路）
  useEffect(() => {
    void api.speechStatus().then((status) => setSttMode(status.mode)).catch(() => undefined)
  }, [])

  useEffect(() => {
    void api
      .getFeatures()
      .then((result) => {
        const item = result.features.find((feature) => feature.id === 'space')
        setSpaceEnabled(item?.enabled ?? true)
      })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    if (!showSpace) return
    const shell = (window as unknown as { yfShell?: YfShell }).yfShell
    shell?.setSpaceLandscape?.(true)
    const orientation = (
      window.screen as Screen & {
        orientation?: {
          lock?: (value: string) => Promise<void>
          unlock?: () => void
        }
      }
    ).orientation
    try {
      void orientation?.lock?.('landscape').catch(() => undefined)
    } catch {
      // 普通浏览器通常要求全屏才允许锁定；失败就让用户自己转屏。
    }
    return () => {
      shell?.setSpaceLandscape?.(false)
      try {
        orientation?.unlock?.()
      } catch {
        // ignore
      }
    }
  }, [showSpace])

  useEffect(() => {
    if (!nativeShell()) return
    const target = window as unknown as {
      yfOnTranscribed?: (result: { ok?: boolean; text?: string; hint?: string }) => void
    }
    target.yfOnTranscribed = (result) => {
      setTranscribing(false)
      if (!result?.ok) {
        setHint(result?.hint ?? '没识别出来')
        return
      }
      const text = (result.text ?? '').trim()
      if (!text) {
        setHint('没听清（再说一次试试）')
        return
      }
      setHint(`说了：${text}`)
      // D124：识别完直接发（输入框里已打的字拼在前面）
      void sendNow(draft ? `${draft.trim()}${text}` : text)
    }
    return () => {
      delete target.yfOnTranscribed
    }
  }, [draft])

  /**
   * 把「文字 +（可选）挂在输入区的图」作为**一条消息**发出去（D166）。
   *
   * 为什么不能"选完就发"：那样先选图 = 一条只有图的消息、回头再打字 = 又一条只有字的消息，
   * 等于两轮对话（用户实测报的）。桌面一直是"挂预览 → 发送时上传 → 文字+图一条"，
   * 手机现在跟它同一条口径。
   */
  async function sendNow(text: string) {
    const trimmed = text.trim()
    const pending = attachmentRef.current
    if ((!trimmed && !pending) || uploading) return
    let imageId: string | undefined
    let previewUrl: string | undefined
    if (pending) {
      setUploading(true)
      setHint(null)
      try {
        const uploaded = await api.uploadImage(pending.file)
        imageId = uploaded.id
        previewUrl = pending.previewUrl
      } catch (exc) {
        setHint(exc instanceof Error ? exc.message : String(exc))
        setUploading(false)
        return
      }
      setUploading(false)
    }
    setHint(null)
    setDraft('')
    setPending(null)
    await send(friendId, trimmed, imageId ? { imageId, previewUrl } : {})
  }

  async function togglePresence() {
    if (!friend || presenceBusy) return
    setPresenceBusy(true)
    setError(null)
    try {
      setFriend(await api.setPresence(friend.id, !friend.presence))
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setPresenceBusy(false)
    }
  }

  /** 挂/摘输入区那张图（state 给界面看，ref 给长命闭包看 —— D166）。 */
  function setPending(next: { file: File; previewUrl: string } | null) {
    attachmentRef.current = next
    setAttachment(next)
  }

  /** 相机/相册选完 → **只挂到输入区**，不发（D166）。 */
  function attach(file: File | undefined) {
    if (!file) return
    if (!looksLikeImage(file)) {
      setHint('这个文件看着不是图片（要 PNG / JPG / WEBP）')
      return
    }
    setHint(null)
    setPending({ file, previewUrl: URL.createObjectURL(file) })
  }

  /**
   * 手机相册选出来的图，WebView 有时给的 `type` 是**空的**（甚至 `application/octet-stream`）
   * —— 那也会走到这里。所以 type 不可信时退回看文件名后缀（2026-09-27，D215）。
   */
  function looksLikeImage(file: File): boolean {
    if (file.type.startsWith('image/')) return true
    const unsure = file.type === '' || file.type === 'application/octet-stream'
    return unsure && /\.(png|jpe?g|webp|gif|bmp|heic|heif)$/i.test(file.name)
  }

  async function startRecording() {
    if (recording || transcribing) return
    setHint(null)
    // App 里走原生录音（WebView 的 getUserMedia 在这台手机上起不来）
    const shell = nativeShell()
    if (shell) {
      if (shell.micPermission?.() !== 'granted') {
        shell.requestMic?.()
        setHint('系统还没给这个 App 麦克风权限 —— 弹窗里点「允许」，然后重新按住')
        return
      }
      if (sttMode === 'system' && shell.startSystemRecognition?.()) {
        setRecording(true)
        return
      }
      if (!shell.startRecording?.()) {
        setHint('原生录音起不来（可能被别的 App 占着麦克风）—— 把别的录音/通话 App 关掉再试')
        return
      }
      setRecording(true)
      return
    }
    // 手机上最常见的两种失败：
    //   NotAllowedError  → 权限没给（第一次要点"允许"）
    //   NotReadableError → 系统没把麦克风交出来：① 被别的 App 占着（语音助手/录音/微信通话）
    //                      ② 权限被 ROM 撤销/设成"每次询问" ③ 上一次录音没释放
    // 所以这里**失败后等半秒再试一次**（③ 那种重试就好），并把原因说清楚。
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const media = await navigator.mediaDevices.getUserMedia({ audio: true })
        const recorder = new MediaRecorder(media)
        chunksRef.current = []
        recorder.ondataavailable = (event) => {
          if (event.data.size) chunksRef.current.push(event.data)
        }
        recorder.onstop = () => {
          media.getTracks().forEach((track) => track.stop())
          const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
          void transcribe(blob)
        }
        recorderRef.current = recorder
        recorder.start()
        setRecording(true)
        return
      } catch (exc) {
        const name = exc instanceof Error ? exc.name : ''
        if (name === 'NotReadableError' && attempt === 0) {
          await new Promise((resolve) => window.setTimeout(resolve, 500))
          continue
        }
        setHint(
          name === 'NotReadableError'
            ? '麦克风被系统挡着（不是权限页那种"允许"）：先看看有没有别的 App 正在用麦克风（语音助手 / 录音 / 微信通话），把本 App 从后台划掉重开；OPPO 还要在 设置→应用→yumeFriend→权限 里把「麦克风/录音」设成允许（别选"每次询问"）'
            : '拿不到麦克风 —— 在手机上第一次用要点一次"允许"',
        )
        return
      }
    }
  }

  function stopRecording() {
    const shell = nativeShell()
    if (shell) {
      if (sttMode === 'system' && shell.stopSystemRecognition) {
        setRecording(false)
        setTranscribing(true)
        setHint('正在识别…')
        shell.stopSystemRecognition()
        return
      }
      // 原生这条路：交给它"停止 + 上传识别"，结果从 window.yfOnTranscribed 回来
      setRecording(false)
      setTranscribing(true)
      setHint('正在识别…')
      shell.stopAndTranscribe?.()
      return
    }
    const recorder = recorderRef.current
    if (recorder && recorder.state !== 'inactive') recorder.stop()
    recorderRef.current = null
    setRecording(false)
  }

  async function transcribe(blob: Blob) {
    setTranscribing(true)
    try {
      const file = new File([blob], 'rec.webm', { type: blob.type || 'audio/webm' })
      const result = await api.transcribeAudio(file)
      if (!result.ok) {
        setHint(result.hint ?? '没识别出来')
        return
      }
      const text = (result.text ?? '').trim()
      if (!text) {
        setHint('没听清（再说一次试试）')
        return
      }
      setHint(`说了：${text}`)
      // D124：识别完**直接发**（输入框里已经打的字拼在前面）
      await sendNow(draft ? `${draft.trim()}${text}` : text)
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setTranscribing(false)
    }
  }

  return (
    <div className="relative flex h-dvh flex-col bg-page">
      <header className="flex shrink-0 items-center gap-2 border-b border-line/70 bg-page/95 px-2 pb-2 pt-[max(8px,env(safe-area-inset-top))]">
        {!embedded && (
          <button
            type="button"
            onClick={onBack}
            aria-label="返回"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-[22px] text-muted active:bg-surface-hover"
          >
            ‹
          </button>
        )}
        <Avatar name={friend?.name ?? '…'} url={friend?.avatar_url} size={34} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[16px] font-medium text-ink">{friend?.name ?? '正在打开…'}</p>
          <p className="flex items-center gap-1 text-[11.5px] text-muted">
            {error ? (
              '打不开'
            ) : (
              <button
                type="button"
                onClick={() => void togglePresence()}
                disabled={!friend || presenceBusy}
                aria-label={friend?.presence ? '在线，点击切到不在线' : '不在线，点击切到在线'}
                className="inline-flex items-center gap-1 rounded-full border border-line/70 px-1.5 py-0.5 text-[10.5px] text-muted active:bg-surface-hover disabled:opacity-50"
              >
                <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                    friend?.presence ? 'bg-emerald-500' : 'bg-stone-400'
                  }`}
                />
                {friend?.presence ? '在线' : '不在线'}
              </button>
            )}
            {friend && !error && (
              <>
                <span className="text-faint">·</span>
                {/* v3.2（D146）：现在是什么模式（只读） */}
                <span
                  className="rounded-full border border-line/70 px-1.5 text-[10.5px]"
                  style={friend.current_mode !== 'normal' ? { color: ACCENT } : undefined}
                >
                  {modeText(friend)}
                </span>
                <LifeBadge friendId={friend.id} />
              </>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setMenuOpen(true)}
          aria-label="更多"
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-[20px] text-muted active:bg-surface-hover"
        >
          ⋯
        </button>
      </header>

      {/* v3.2（D144）：手机端"出错 / 被打断 / 她在调工具 / 她在想"都摊开来说（D134）。
          v3.5：沉浸/通话打开时**不渲染** —— 那张卡是 z-50，会压在沉浸（z-40）上面，
          手机上就变成"她在想"冒出两个（用户实测）。 */}
      {!immersive && !inCall && (
        <StreamStatusCard stream={stream} className="left-3 top-16 max-w-[88vw]" />
      )}

      <div ref={scrollBoxRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {list.length > 0 && (
          <button
            type="button"
            onClick={loadOlderKeepingScroll}
            className="mx-auto mb-2 block rounded-full border border-line/70 px-3 py-1 text-[11.5px] text-muted"
          >
            看更早的
          </button>
        )}
        <div className="flex flex-col gap-3">
          {list.map((message) =>
            message.role === 'assistant' && (message.segments?.length ?? 0) > 1 ? (
              <div key={message.id} className="flex flex-col gap-1.5">
                {message.segments!.map((segment, index) => (
                  <Bubble
                    key={`${message.id}-${index}`}
                    message={message}
                    friend={friend}
                    onOpenImage={setLightbox}
                    contentOverride={segment}
                    hideExtras={index !== 0}
                  />
                ))}
              </div>
            ) : (
              <Bubble
                key={message.id}
                message={message}
                friend={friend}
                onOpenImage={setLightbox}
              />
            ),
          )}
          {streaming && (
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
                      <div key={`stream-${index}`} className="flex gap-2">
                        <Avatar name={friend?.name ?? '她'} url={friend?.avatar_url} size={34} className="mt-1" />
                        <div className="max-w-[78%] whitespace-pre-wrap break-words rounded-2xl rounded-bl-md border border-line/70 bg-surface px-3.5 py-2 text-[15px] leading-relaxed text-ink">
                          <MarkdownText text={segment} />
                          {!hasHiddenBubble && index === visibleSegments.length - 1 && (
                            <span className="ml-0.5 inline-block h-3.5 w-[2px] translate-y-[2px] animate-pulse bg-accent" />
                          )}
                        </div>
                      </div>
                    ))}
                    {showTyping && (
                      <div className="flex gap-2">
                        <Avatar name={friend?.name ?? '她'} url={friend?.avatar_url} size={34} className="mt-1" />
                        <div className="rounded-2xl rounded-bl-md border border-line/70 bg-surface px-3.5 py-2 text-[15px] leading-relaxed text-ink">
                          正在输入中...
                        </div>
                      </div>
                    )}
                  </>
                )
              })()}
            </div>
          )}
        </div>
        <div ref={bottomRef} />
      </div>

      {hint && (
        <p className="shrink-0 px-3 pb-1 text-[11.5px] text-muted">{hint}</p>
      )}

      <div className="shrink-0 border-t border-line/70 bg-surface/60 px-2 pb-[max(8px,env(safe-area-inset-bottom))] pt-2">
        {/* v3.7（D166）：挂在输入区的图（还没发）—— 按发送时和文字**一条消息**一起走 */}
        {attachment && (
          <div className="mb-2 flex items-center gap-2 px-1">
            <div className="relative">
              <img
                src={attachment.previewUrl}
                alt="待发送的图"
                className="h-14 w-14 rounded-xl border border-line object-cover"
              />
              <button
                type="button"
                onClick={() => setPending(null)}
                aria-label="不发这张"
                className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full border border-line bg-page text-[11px] text-muted active:bg-surface-hover"
              >
                ✕
              </button>
            </div>
            <span className="text-[11px] text-faint">
              {uploading ? '正在上传…' : '发送时会带上这张图'}
            </span>
          </div>
        )}
        <div className="flex items-end gap-1.5">
          {/* v3.7（D164）：**一个**相机按钮 —— 点一下由系统给"拍摄 / 相册"二选一 */}
          <button
            type="button"
            onClick={() => albumRef.current?.click()}
            disabled={uploading}
            aria-label="发图（拍摄 / 相册）"
            title="发张图：拍摄，或从相册里选"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl border border-line bg-surface text-muted active:bg-surface-hover disabled:opacity-40"
          >
            {uploading ? '…' : <IconCamera size={19} />}
          </button>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            rows={1}
            placeholder={transcribing ? '正在识别…' : '说点什么…'}
            className="max-h-[120px] min-h-11 flex-1 resize-none rounded-2xl border border-line bg-surface px-3 py-2.5 text-[15px] text-ink outline-none focus:border-accent/60"
          />
          {streaming ? (
            <button
              type="button"
              onClick={() => stop(friendId)}
              className="h-11 shrink-0 rounded-2xl border border-line bg-surface px-3.5 text-[13.5px] text-muted active:bg-surface-hover"
            >
              停止
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void sendNow(draft)}
              disabled={(!draft.trim() && !attachment) || uploading}
              className="h-11 shrink-0 rounded-2xl px-3.5 text-[13.5px] text-white disabled:opacity-40"
              style={{ background: ACCENT }}
            >
              {uploading ? '上传中…' : '发送'}
            </button>
          )}
        </div>

        <button
          type="button"
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId)
            void startRecording()
          }}
          onPointerUp={stopRecording}
          onPointerCancel={stopRecording}
          className={`mt-2 h-12 w-full rounded-2xl border text-[14px] ${
            recording
              ? 'border-accent bg-accent/12 text-accent'
              : 'border-line bg-surface text-muted active:bg-surface-hover'
          }`}
        >
          {transcribing ? '正在识别…' : recording ? '松手就发（说话中…）' : '按住说话'}
        </button>
      </div>

      {/* 发图：**一个**按钮 + **一个** input（D164）。
          注意**不加 `capture`** —— 带了它系统就只给相机、不给相册；
          不带 capture，手机上才会弹"拍摄 / 相册"那个系统选择器（壳里的 onShowFileChooser）。 */}
      <input
        ref={albumRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = '' // 同一张图也要能再选一次
          void attach(file)
        }}
      />

      {lightbox && (
        <div className="fixed inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-black/85 p-4">
          <img
            src={withWidth(lightbox, 1800)}
            alt=""
            onClick={() => setLightbox(null)}
            className="max-h-[80dvh] max-w-full object-contain"
          />
          <div className="flex gap-2">
            <SaveImageButton
              url={lightbox}
              onDone={setSaveHint}
              className="rounded-full border border-white/30 bg-black/50 px-4 py-2 text-[13px] text-white"
            />
            <button
              type="button"
              onClick={() => setLightbox(null)}
              className="rounded-full border border-white/30 bg-black/50 px-4 py-2 text-[13px] text-white"
            >
              关闭
            </button>
          </div>
          {saveHint && <p className="text-[12px] text-white/80">{saveHint}</p>}
        </div>
      )}

      {menuOpen && (
        <div className="fixed inset-0 z-40 flex items-end bg-black/40" onClick={() => setMenuOpen(false)}>
          <div
            className="w-full rounded-t-3xl border-t border-line bg-surface px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-4"
            onClick={(event) => event.stopPropagation()}
          >
            <p className="mb-3 text-[13px] text-muted">快捷入口</p>
            <div className="grid grid-cols-2 gap-2">
              {QUICK_ENTRIES.filter((entry) => entry.key !== 'space' || spaceEnabled).map((entry) => (
                <button
                  key={entry.key}
                  type="button"
                  onClick={() => {
                    setMenuOpen(false)
                    // 这几个入口**都是真的**（不再有"某批再做"的占位）
                    switch (entry.key) {
                      case 'immersive':
                        setImmersive(true)
                        break
                      case 'call':
                        setInCall(true)
                        break
                      case 'knowledge':
                        onOpenKnowledge()
                        break
                      case 'log':
                        onOpenLogs()
                        break
                      case 'persona':
                        onOpenPersona()
                        break
                      case 'schedule':
                        onOpenSchedules()
                        break
                      case 'space':
                        setShowSpace(true)
                        break
                    }
                  }}
                  className="flex h-12 items-center justify-center rounded-2xl border border-line bg-surface/80 px-3.5 text-[14px] text-ink active:bg-surface-hover"
                >
                  {entry.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* 沉浸 / 通话：**跟桌面同一份组件**（shared），只传 variant="mobile"（D140） */}
      {immersive && friend && (
        <ImmersiveView
          variant="mobile"
          friend={friend}
          messages={list}
          stream={stream}
          segments={voiceSegments}
          onSend={(text) => void sendNow(text)}
          onStop={() => stop(friendId)}
          onClose={() => setImmersive(false)}
          myName={myName}
          onMyNameChange={saveMyName}
          onOpenCall={() => {
            setImmersive(false)
            setInCall(true)
          }}
        />
      )}
      {inCall && friend && (
        <CallView
          variant="mobile"
          friend={friend}
          messages={list}
          stream={stream}
          segments={voiceSegments}
          onSend={(text) => void sendNow(text)}
          onStop={() => stop(friendId)}
          onClose={() => setInCall(false)}
        />
      )}
      {showSpace && friend && (
        <SpaceView
          variant="mobile"
          friend={friend}
          onClose={() => setShowSpace(false)}
        />
      )}
    </div>
  )
}
