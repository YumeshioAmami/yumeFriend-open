/**
 * 语音通话（D126）：**不显示文字**的对话界面。
 *
 * 用户要的样子：中间一个大立绘（没立绘就用大头像），铺沉浸模式那张背景，
 * 其它什么都没有 —— 只有一行状态字（`阿月 正在说话` / `我在听…`）和一个挂断。
 *
 * 三个要点：
 *  1. **她说话** → 播她的分段语音；播完**自动**回到"听你说"（不用点）。
 *  2. **我在听** → 麦克风一直开着，用**音量检测（VAD）**判断我有没有说话；
 *     说完一小段就切下来做本地识别。
 *  3. ⚠️ **识别出来是空的/只有"嗯嗯啊啊" → 直接丢掉、继续等**，
 *     绝不把空话发给 AI（否则她会问"你怎么不说话"，用户原话：那就太垃圾了）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import Avatar from './Avatar'
import { withWidth } from './imgUrl'
import { api } from './api'
import { motionClassOf, playMoodSound, preloadMoodSounds } from './moodSound'
import { setSurfaceActive } from './playback'
import StreamStatusCard from './StreamStatusCard'
import { ACCENT, stripMoodMarks } from './ui'
import type { Friend, Message } from './api'
import type { StreamState } from './useChat'
import type { AudioSegmentEvent } from './useWs'

/** 音量阈值（0~1）：超过它算"我在说话" */
const SPEECH_LEVEL = 0.06
/** 连续多久超过阈值才算真的开口（滤掉咳嗽/碰麦） */
const SPEECH_HOLD_MS = 150
/** 安静多久算"这一段说完了" */
// 2026-10-02（用户要求）：说完之后留 **10 秒** 再判"这段说完了" —— 人要想事情，
// 之前 0.9 秒太赶（"说话会很着急"）。要立刻结束就按那个「说完了」的按钮。
const SILENCE_MS = 10000
/** 一段最长录多久（免得我一直说个没完） */
const MAX_SEGMENT_MS = 30000
/** 这些"语气词"单独出现时当没说 */
const FILLERS = new Set(['嗯', '啊', '呃', '哦', '噢', '唉', '诶', '哈', '喂', '那个'])

type Phase = 'listening' | 'thinking' | 'speaking' | 'hint'

/**
 * Android App 的原生通话录音（`MainActivity.ShellBridge`）：
 * **连续录音 + 原生音量检测（VAD）自动分段 + 上传识别**，文字用回调送回来。
 *
 * 为什么要它：手机浏览器里能录音，但 App 的 WebView 里 `getUserMedia` 起不来
 * （NotReadableError，2026-09-21 实测）→ 通话整段搬到原生。
 * 桌面 / 浏览器没有这个桥，照旧走 WebAudio 那套。
 */
interface YfCallShell {
  callStart?: () => boolean
  callPause?: () => void
  callResume?: () => void
  callStop?: () => void
}

function nativeCallShell(): YfCallShell | null {
  const shell = (window as unknown as { yfShell?: YfCallShell }).yfShell
  return shell && typeof shell.callStart === 'function' ? shell : null
}

/** 一段"空音频"（合法 wav、没声音）：手机上先用它解锁自动播放（variant='mobile' 才用）。 */
const SILENT_WAV =
  'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA='

function meaningful(text: string): string | null {
  const trimmed = (text || '').trim()
  const bare = trimmed.replace(/[，。！？、,.!?~…\s]/g, '')
  if (bare.length < 2) return null
  if (FILLERS.has(bare)) return null
  return trimmed
}

export default function CallView({
  friend,
  messages,
  stream,
  segments,
  onSend,
  onStop,
  onClose,
  variant = 'desktop',
}: {
  friend: Friend
  messages: Message[]
  stream?: StreamState
  segments: AudioSegmentEvent[]
  onSend: (text: string) => void
  onStop: () => void
  onClose: () => void
  /** v3.0（D140）：手机壳复用同一份；只换手机需要的那点东西（版式不变） */
  variant?: 'desktop' | 'mobile'
}) {
  const mobile = variant === 'mobile'
  const [phase, setPhase] = useState<Phase>('listening')
  const [hint, setHint] = useState<string | null>(null)
  const [heard, setHeard] = useState<string | null>(null)
  const [audioPlaying, setAudioPlaying] = useState(false)
  const [audioTick, setAudioTick] = useState(0) // 催一下「该播下一段了」
  // 正在播的那一段（D134）：立绘的表情/动作/音效都跟着它走
  const [speakingSegment, setSpeakingSegment] = useState<AudioSegmentEvent | null>(null)

  const audioRef = useRef<HTMLAudioElement | null>(null)
  const audioUnlocked = useRef(false)
  const audioPlayingRef = useRef(false) // 给 playNext 读最新值（闭包会过期）
  const queueRef = useRef<AudioSegmentEvent[]>([])
  const playedRef = useRef<Set<string>>(new Set())

  // v3.14（D182）：通话开着就由**这一屏**出声（聊天窗口那条语音条会自觉静音）。
  useEffect(() => {
    setSurfaceActive('call', true)
    return () => setSurfaceActive('call', false)
  }, [])
  const mediaRef = useRef<MediaStream | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const rafRef = useRef<number | null>(null)
  const recordingRef = useRef(false)
  const loudSinceRef = useRef<number | null>(null)
  const quietSinceRef = useRef<number | null>(null)
  const segmentStartRef = useRef(0)
  const phaseRef = useRef<Phase>('listening')
  phaseRef.current = phase

  const busy =
    stream !== undefined &&
    stream.status !== 'idle' &&
    stream.status !== 'error' &&
    stream.status !== 'partial'

  const lastAssistant = useMemo(
    () =>
      [...messages]
        .reverse()
        .find((item) => item.role === 'assistant' && item.content.trim().length > 0),
    [messages],
  )

  // 她的表情跟着**正在播的那一段**走（D134）：立绘换图 + 小动作 + 一声轻音效
  const failed = stream?.status === 'error'
  const callMood = speakingSegment?.mood ?? lastAssistant?.mood ?? ''
  const hasPortrait = (friend.portrait_emotions?.length ?? 0) > 0
  useEffect(() => {
    if (hasPortrait && callMood) playMoodSound(callMood, friend.id)
  }, [callMood, hasPortrait])

  // D194：通话也一样 —— 先抓好音效，别等换表情时现拉（那会让动作和音效对不上）
  useEffect(() => {
    if (hasPortrait) void preloadMoodSounds()
  }, [hasPortrait])

  // ── 播她的语音（分段队列，**一段好了就播**，不等整条说完 —— 跟沉浸模式一个路子）──
  //
  // ⚠️ 这里踩过一个坑：原来开播的条件是 `!audio.src`，而第一段播完后 src 一直留着 →
  // **后面的段永远不开播**（用户实测："合成出来了他也不说"）。现在改成"有货就播"，
  // 用 audioTick 这个计数器在"排队了 / 一段播完了 / 播失败了"时催促一下。
  const playNext = useCallback(() => {
    const audio = audioRef.current
    if (!audio || audioPlayingRef.current || phaseRef.current === 'listening') return
    const next = queueRef.current.shift()
    if (!next?.audioUrl) return
    setPhase('speaking')
    setSpeakingSegment(next)
    audio.src = next.audioUrl
    void audio.play().catch(() => undefined)
  }, [])

  useEffect(() => {
    // 正在生成时也要开播（她有第一段就先说第一段）
    const messageId = busy ? stream?.assistantId : lastAssistant?.id
    if (!messageId) return
    let added = false
    for (const item of segments) {
      if (item.messageId !== messageId || !item.audioUrl) continue
      const key = `${item.messageId}:${item.index}`
      if (playedRef.current.has(key)) continue
      playedRef.current.add(key)
      queueRef.current.push(item)
      added = true
    }
    if (added) {
      queueRef.current.sort((a, b) => a.index - b.index)
      setAudioTick((value) => value + 1)
    }
  }, [segments, busy, stream?.assistantId, lastAssistant?.id])

  useEffect(() => {
    playNext()
  }, [audioTick, audioPlaying, phase, playNext])

  // 她这一轮的语音状态：pending = 还在合成（**必须等**，不然会"文字完了就轮到我说"）
  const voiceStatus = busy ? undefined : lastAssistant?.audio_status ?? null
  const waitingForVoice = voiceStatus === 'pending'
  const noVoice = voiceStatus === null || voiceStatus === 'failed'
  const herTurnDone =
    !busy && !audioPlaying && queueRef.current.length === 0 && !waitingForVoice && !failed

  // 她说完了（**语音也播完了**）→ 才轮到"我在听"
  useEffect(() => {
    if (phase !== 'speaking' || !herTurnDone) return
    const timer = window.setTimeout(() => setPhase('listening'), noVoice ? 2500 : 700)
    return () => window.clearTimeout(timer)
  }, [phase, herTurnDone, noVoice, lastAssistant?.id])

  // ── 麦克风 + 音量检测（VAD）───────────────────────────────────
  const stopSegment = useCallback(() => {
    const recorder = recorderRef.current
    if (recorder && recorder.state !== 'inactive') recorder.stop()
    recorderRef.current = null
    recordingRef.current = false
    loudSinceRef.current = null
    quietSinceRef.current = null
  }, [])

  const transcribeSegment = useCallback(
    async (blob: Blob) => {
      try {
        const file = new File([blob], 'call.webm', { type: blob.type || 'audio/webm' })
        const result = await api.transcribeAudio(file)
        const text = result.ok ? meaningful(result.text ?? '') : null
        if (!text) {
          // ⚠️ 没听清 / 只有语气词 → **丢掉，继续等**（不发给 AI）
          return
        }
        setHeard(text)
        setPhase('thinking')
        onSend(text)
      } catch {
        /* 识别失败也当"没说" */
      }
    },
    [onSend],
  )

  const startSegment = useCallback(() => {
    const media = mediaRef.current
    if (!media || recordingRef.current) return
    const recorder = new MediaRecorder(media)
    chunksRef.current = []
    recorder.ondataavailable = (event) => {
      if (event.data.size) chunksRef.current.push(event.data)
    }
    recorder.onstop = () => {
      const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
      chunksRef.current = []
      if (blob.size > 2000) void transcribeSegment(blob)
    }
    recorderRef.current = recorder
    recorder.start()
    recordingRef.current = true
    segmentStartRef.current = performance.now()
  }, [transcribeSegment])

  const tick = useCallback(() => {
    const analyser = analyserRef.current
    if (!analyser || phaseRef.current !== 'listening') {
      rafRef.current = requestAnimationFrame(tick)
      return
    }
    const data = new Uint8Array(analyser.fftSize)
    analyser.getByteTimeDomainData(data)
    let peak = 0
    for (const value of data) peak = Math.max(peak, Math.abs(value - 128) / 128)
    const now = performance.now()
    if (peak > SPEECH_LEVEL) {
      quietSinceRef.current = null
      loudSinceRef.current ??= now
      if (!recordingRef.current && now - (loudSinceRef.current ?? now) > SPEECH_HOLD_MS) {
        startSegment()
      }
    } else {
      loudSinceRef.current = null
      if (recordingRef.current) {
        quietSinceRef.current ??= now
        const quietLongEnough = now - (quietSinceRef.current ?? now) > SILENCE_MS
        const tooLong = now - segmentStartRef.current > MAX_SEGMENT_MS
        if (quietLongEnough || tooLong) stopSegment()
      }
    }
    rafRef.current = requestAnimationFrame(tick)
  }, [startSegment, stopSegment])

  useEffect(() => {
    let cancelled = false
    async function openMic() {
      // App 里走原生通话录音（WebView 的 getUserMedia 在这台手机上起不来）
      const shell = nativeCallShell()
      if (shell) {
        const target = window as unknown as {
          yfOnCallHeard?: (text: string) => void
          yfOnCallError?: (hint: string) => void
        }
        target.yfOnCallHeard = (text: string) => {
          // ⚠️ 跟网页那套一个口径：没听清 / 只有语气词 → 丢掉继续等（绝不发给 AI）
          const clean = meaningful(text)
          if (!clean) return
          setHeard(clean)
          setPhase('thinking')
          onSend(clean)
        }
        target.yfOnCallError = (hint: string) => {
          setPhase('hint')
          setHint(hint)
        }
        const started = shell.callStart?.() ?? false
        if (!started) {
          setPhase('hint')
          setHint('麦克风没开起来 —— 看看系统里给 App 的麦克风权限')
        }
        return
      }
      try {
        const media = await navigator.mediaDevices.getUserMedia({ audio: true })
        if (cancelled) {
          media.getTracks().forEach((track) => track.stop())
          return
        }
        mediaRef.current = media
        const ctx = new AudioContext()
        const source = ctx.createMediaStreamSource(media)
        const analyser = ctx.createAnalyser()
        analyser.fftSize = 1024
        source.connect(analyser)
        ctxRef.current = ctx
        analyserRef.current = analyser
        setHint(null)
        rafRef.current = requestAnimationFrame(tick)
      } catch {
        setPhase('hint')
        setHint('拿不到麦克风权限（桌面外壳可能拦着）—— 先用浏览器打开，或者告诉我我去放行')
      }
    }
    void openMic()
    return () => {
      cancelled = true
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
      stopSegment()
      mediaRef.current?.getTracks().forEach((track) => track.stop())
      void ctxRef.current?.close().catch(() => undefined)
      mediaRef.current = null
      ctxRef.current = null
      analyserRef.current = null
      // 原生那条路：挂断/退出时收工，并把回调摘掉
      const shell = nativeCallShell()
      if (shell) {
        shell.callStop?.()
        const target = window as unknown as {
          yfOnCallHeard?: (text: string) => void
          yfOnCallError?: (hint: string) => void
        }
        delete target.yfOnCallHeard
        delete target.yfOnCallError
      }
    }
    // 只建一次麦克风；相位切换靠 phaseRef 控制（正在说她的语音时不录）
  }, [tick, stopSegment])

  // 她在说话 / 我在想的时候，把没说完的那一段丢掉（别录到她的声音）
  useEffect(() => {
    const shell = nativeCallShell()
    if (shell) {
      // 原生那条路：让原生暂停/继续"听"（暂停时它会把当前那段丢掉）
      if (phase === 'listening') shell.callResume?.()
      else shell.callPause?.()
      return
    }
    if (phase === 'listening') return
    stopSegment()
  }, [phase, stopSegment])

  // 没配音色 / 服务没开 → 给人话提示（用户拍板：提示，不禁用）
  useEffect(() => {
    if (friend.voice_id || friend.voice_reply_enabled) return
    setHint((prev) => prev ?? '她还没配音色（或者 GPT-SoVITS 没开）—— 现在只能听你说，她答不了声；去「设置 → 语音」配一个吧')
  }, [friend.voice_id, friend.voice_reply_enabled])

  // 表情兜底（D109）：她给的没配图就用「正常」→「平静」→ 第一张
  const portraitList = friend.portrait_emotions ?? []
  const callPortraitMood =
    callMood && (portraitList.length === 0 || portraitList.includes(callMood))
      ? callMood
      : portraitList.includes('正常')
        ? '正常'
        : portraitList.includes('平静')
          ? '平静'
          : (portraitList[0] ?? '正常')
  const statusLine =
    failed
      ? '这次没答上（看左上角）'
      : phase === 'thinking'
      ? '我在说…'
      : phase === 'speaking'
        ? `${friend.name} 正在说话`
        : phase === 'hint'
          ? (hint ?? '')
          : heard
            ? '我在听…（说完了停一下就行）'
            : '我在听…（没想好就慢慢想，我一直等着）'

  return (
    <div
      className={`fixed inset-0 z-50 flex flex-col overflow-hidden bg-page ${mobile ? 'h-dvh' : ''}`}
      // v3.6：安全区只留上边 —— 四边都加会把整个覆盖层往里缩（曲面屏上对话框贴不到边、四周露白边）
      style={{
        paddingTop: 'env(safe-area-inset-top)',
      }}
      // 手机：第一次点屏幕解锁音频（不然她第一段"自动播"会被浏览器拦掉）
      onPointerDown={() => {
        if (!mobile || audioUnlocked.current) return
        audioUnlocked.current = true
        try {
          void new Audio(SILENT_WAV).play().catch(() => undefined)
        } catch {
          // 忽略
        }
      }}
    >
      {friend.background_url ? (
        <>
          <img
            src={withWidth(friend.background_url, mobile ? 1280 : 0)}
            alt=""
            className="pointer-events-none absolute inset-0 h-full w-full object-cover"
          />
          <div className="pointer-events-none absolute inset-0 bg-scrim/50" aria-hidden />
        </>
      ) : (
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            background: 'radial-gradient(120% 80% at 50% 0%, var(--color-accent) 0%, transparent 55%)',
            opacity: 0.16,
          }}
          aria-hidden
        />
      )}

      {/* 出错 / 工具 / 思考：左上角摊开（D134） */}
      <StreamStatusCard stream={stream} />

      {/* 中间：大立绘 / 大头像（只有它） */}
      <div className="relative z-10 flex min-h-0 flex-1 items-center justify-center px-8">
        {friend.portrait_emotions && friend.portrait_emotions.length > 0 ? (
          <img
            key={`${callPortraitMood}#${callMood}`}
            src={`/api/friends/${friend.id}/portraits/${encodeURIComponent(callPortraitMood)}`}
            alt=""
            className={`portrait-motion ${motionClassOf(callMood)} max-h-full w-auto max-w-full object-contain drop-shadow-[0_20px_50px_rgba(0,0,0,0.45)]`}
          />
        ) : (
          <Avatar name={friend.name} url={friend.avatar_url} size={320} />
        )}
      </div>

      {/* 底部：只有一行状态 + 挂断 */}
      <div
        className="relative z-10 flex shrink-0 flex-col items-center gap-3 pb-10"
        style={{ paddingBottom: 'max(2.5rem, env(safe-area-inset-bottom))' }}
      >
        <span className="flex items-center gap-2 rounded-full bg-black/45 px-4 py-1.5 text-[13.5px] text-white/90 backdrop-blur">
          <span
            className={`h-2 w-2 rounded-full ${phase === 'listening' ? 'animate-pulse' : ''}`}
            style={{ background: phase === 'listening' ? ACCENT : 'rgba(255,255,255,0.5)' }}
          />
          {statusLine}
        </span>
        {heard && phase === 'thinking' && (
          <span className="text-[11.5px] text-white/50">说了：{stripMoodMarks(heard)}</span>
        )}
        <button
          type="button"
          onClick={() => {
            stopSegment()
            onStop()
            onClose()
          }}
          className="rounded-full border border-white/25 bg-black/45 px-6 py-2 text-[13px] text-white/90 backdrop-blur transition-colors hover:border-red-300/60 hover:text-red-200"
        >
          挂断
        </button>
      </div>

      <audio
        ref={audioRef}
        onPlaying={() => {
          audioPlayingRef.current = true
          setAudioPlaying(true)
        }}
        onEnded={() => {
          audioPlayingRef.current = false
          setAudioPlaying(false)
          setAudioTick((value) => value + 1) // 接着播下一段
        }}
        onError={() => {
          audioPlayingRef.current = false
          setAudioPlaying(false)
          setAudioTick((value) => value + 1) // 这一段坏了就跳过去
        }}
        className="hidden"
      />
    </div>
  )
}
