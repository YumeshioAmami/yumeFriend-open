/**
 * 沉浸模式（v2.2 / T2.2-2；D105~D110）。
 *
 * galgame 那一套，但**只有一个对话框**：
 *  · 她说话时：名字牌 = `阿月（高兴）`，框里是她的这一句；**点一下 → 下一句**；
 *  · 轮到你了：名字牌 = `我`（可以改叫法），同一个框变成输入框，写完回车发出去；
 *  · 没人再写第二个框 —— 说话的人换，框不换（用户原话："共用一个对话框"）。
 *  · 立绘可以在沉浸模式里**拖动 + 滚轮缩放**（D110），位置按朋友存下来。
 *  · 语音按段走，且**只绑当前这一轮**（D110：靠 start 事件给的 assistant_message_id）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import Avatar from './Avatar'
import Live2DCharacter from './Live2DCharacter'
import { withWidth } from './imgUrl'
import { api } from './api'
import { playMoodSound, preloadMoodSounds } from './moodSound'
import { setSurfaceActive } from './playback'
import StreamStatusCard from './StreamStatusCard'
import { stripMoodMarks } from './ui'
import { MarkdownText } from './MarkdownText'
import { useInputHistory } from './useInputHistory'
import type { Friend, Message } from './api'
import type { AudioSegmentEvent } from './useWs'
import type { StreamState } from './useChat'

const AUTO_KEY = 'yf.immersive.auto'
const AUTO_NO_AUDIO_MS = 2400
/**
 * 她配了语音时，"等她这一段的音频"最多等这么久（2026-10-02 用户要求：
 * **文字和声音一起出**，不许文字抢跑）。本地合成一句要几秒，所以给足余量；
 * 超时才兜底翻页（那种情况会在音频条上写"没合成出来"，是真失败）。
 */
const VOICE_WAIT_MS = 20000

/**
 * 一段"空音频"（44 字节的合法 wav，没有声音）。
 * 手机浏览器（iOS 尤其）要求**用户手势之后**才允许 `play()` ——
 * 进沉浸模式时先放它一下，后面她说话就能自动播了（variant='mobile' 才用）。
 */
const SILENT_WAV =
  'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA='

type Transform = { scale: number; x: number; y: number }
const DEFAULT_TRANSFORM: Transform = { scale: 1, x: 0, y: 0 }

/** 情绪 → 立绘动作（D111）：看一眼就知道她心情变了。没配到的用一个很轻的淡入。 */
const MOTION_RULES: [string[], string][] = [
  [['兴奋', '开心', '愉快', '高兴', '惊喜', '笑', '轻笑'], 'motion-happy'],
  [['调皮', '坏笑', '狡黠'], 'motion-playful'],
  [['害羞', '羞耻', '脸红'], 'motion-shy'],
  [['生气', '愤怒', '厌恶', '不爽'], 'motion-angry'],
  [['惊讶', '吃惊', '震惊'], 'motion-surprised'],
  [['伤心', '难过', '无奈', '担心', '紧张', '慌张', '害怕'], 'motion-sad'],
  [['心动', '喜欢', '爱慕', '软', '柔软', '宠溺'], 'motion-love'],
  [['自信', '认真', '得意'], 'motion-proud'],
  [['疑惑', '困惑', '懵'], 'motion-puzzled'],
]

/**
 * v3.6：**她写了某个情绪、但没有这张立绘**时的近义回退。
 *
 * 起因（用户实测："之前有一段情绪识别他没识别出来"）：她写 `[关心]` 写了 13 次，
 * 但导入的 20 张立绘里**没有"关心"** → 直接退回「正常」，看着就像情绪没识别到。
 * 现在先按这张表找个她**真有**的近义表情，找不到才退「正常」。
 */
const MOOD_ALIAS: Record<string, string[]> = {
  关心: ['担心', '平静'],
  正经: ['认真'],
  好奇: ['疑惑'],
  思考: ['认真'],
  沉思: ['认真'],
  欢呼: ['兴奋', '高兴'],
  激动: ['兴奋'],
  期待: ['心动', '兴奋'],
  恍然大悟: ['惊讶'],
  恍然: ['惊讶'],
  轻快: ['高兴'],
  满足: ['高兴', '平静'],
  温柔: ['平静'],
  感动: ['高兴', '心动'],
  委屈: ['伤心'],
  撒娇: ['调皮'],
  叹气: ['无奈'],
  偷笑: ['调皮'],
  坏笑: ['调皮'],
  心虚: ['慌张'],
  释然: ['平静'],
  纠结: ['无奈'],
  嘀咕: ['无奈'],
  愣住: ['疑惑'],
  // v3.13（D181）：LingChat 那两位新朋友（钦灵 / 风雪）会写的词 —— 她们**没有**这几张立绘，
  // 就近落到一张**她真有**的表情上（顺序 = 优先挑哪张；一张都没有才退「正常」）。
  冷淡: ['平静', '正常'],
  盯着: ['认真', '疑惑'],
  无语: ['无奈'],
  小声嘀咕: ['嘀咕', '无奈'],
  // v3.51（D241）：平和 71 次！还有调侃 / 嘟囔 / 小声 / 沮丧 —— 都没有对应立绘，就近落一张。
  平和: ['平静', '正常'],
  调侃: ['调皮'],
  嘟囔: ['无奈'],
  小声: ['无奈', '嘀咕'],
  沮丧: ['伤心', '无奈'],
  // v7.18.1（D370）：模型自创的近义标签，摘掉后落到她真有的立绘。
  软: ['心动', '害羞'],
  柔软: ['心动', '害羞'],
  笑: ['高兴', '调皮'],
  轻笑: ['高兴', '调皮'],
  宠溺: ['心动', '高兴'],
  蹙眉: ['担心'],
  凑近: ['心动'],
}

function motionClassOf(mood: string): string {
  for (const [keywords, className] of MOTION_RULES) {
    if (keywords.some((word) => mood.includes(word))) return className
  }
  return 'motion-plain'
}

/**
 * v3.9：情绪动作改用 **Web Animations API** 播（不再靠"摘类→回流→挂类"那套）。
 *
 * 为什么换：那套在真机上**看不见动作**（不重挂载时浏览器不一定重播，观众也看不出差别），
 * 而 `el.animate()` 是显式调用，**一定播**、也一定从头播。
 *
 * 幅度按**元素自身尺寸**换算（`f.y` 是"高度的百分比"）：她的立绘可能被放大 3 倍，
 * 用比例算出来的位移在任何缩放下都看得见，又不会变成"突然变大"。
 */
const MOTION_FRAMES: Record<string, {
  ms: number
  y?: number[]
  x?: number[]
  rot?: number[]
  scale?: number[]
}> = {
  'motion-happy': { ms: 900, y: [0, -0.05, -0.015, 0] },
  'motion-playful': { ms: 900, x: [0, -0.02, 0.02, -0.006, 0], rot: [0, -3.5, 3.5, -1.5, 0] },
  'motion-shy': { ms: 900, y: [0, 0.032, 0.01, 0], scale: [1, 0.985, 0.996, 1] },
  'motion-angry': {
    ms: 850,
    x: [0, -0.022, 0.02, -0.015, 0.01, -0.005, 0],
    rot: [0, -1.2, 1.2, -0.8, 0.6, 0, 0],
  },
  'motion-surprised': { ms: 800, y: [0, -0.045, 0.006, 0], scale: [1, 1.012, 0.997, 1] },
  'motion-sad': { ms: 1000, y: [0, 0.032, 0.014, 0], rot: [0, -1.4, 0.9, 0] },
  'motion-love': { ms: 950, y: [0, -0.028, -0.007, 0] },
  'motion-proud': { ms: 800, y: [0, -0.016, 0], scale: [1, 1.01, 1] },
  'motion-puzzled': { ms: 900, rot: [0, -5, 3.5, 0] },
}

/** 把"比例关键帧"换算成真正的像素/角度关键帧。 */
function motionKeyframes(name: string, width: number, height: number): Keyframe[] | null {
  const spec = MOTION_FRAMES[name]
  if (!spec) return null
  const steps = Math.max(
    spec.y?.length ?? 0,
    spec.x?.length ?? 0,
    spec.rot?.length ?? 0,
    spec.scale?.length ?? 0,
  )
  const out: Keyframe[] = []
  for (let i = 0; i < steps; i += 1) {
    const px = (list: number[] | undefined) => (list ? list[Math.min(i, list.length - 1)] : 0)
    const translateY = px(spec.y) * height
    const translateX = px(spec.x) * width
    const rotate = spec.rot ? px(spec.rot) : 0
    const scale = spec.scale ? 1 + (px(spec.scale) - 1) : 1
    out.push({
      transform: `translate(${translateX.toFixed(1)}px, ${translateY.toFixed(1)}px) rotate(${rotate}deg) scale(${scale.toFixed(4)})`,
    })
  }
  return out
}

/**
 * 按自然段切（D107）：空行分段；**只由括号组成的行不算一段**（动作挂在后面的台词上）。
 * 跟后端 `voice_reply_service.extract_ready_paragraphs` 是同一条规则。
 */
function splitParagraphs(text: string): { text: string; start: number }[] {
  const parts = text.split(/(\n{2,})/)
  const out: { text: string; start: number }[] = []
  let pending: { text: string; start: number }[] = []
  let offset = 0
  for (const part of parts) {
    const start = offset
    offset += part.length
    if (/^\n{2,}$/.test(part)) continue
    const trimmed = part.trim()
    if (!trimmed) continue
    const lead = part.length - part.trimStart().length
    pending.push({ text: trimmed, start: start + lead })
    // 只由括号组成的块不算一段（它挂在后面的台词上，D107）
    if (trimmed.replace(/[（(][^）)]*[）)]/g, '').trim().length === 0) continue
    out.push({ text: pending.map((item) => item.text).join('\n'), start: pending[0].start })
    pending = []
  }
  if (pending.length > 0) {
    out.push({ text: pending.map((item) => item.text).join('\n'), start: pending[0].start })
  }
  return out
}

/** D115：这一段该用哪个情绪 —— 取"偏移落在这一段之前的最后一个标记"。 */
function moodAt(moods: { mood: string; at: number }[], start: number): string | null {
  let found: string | null = null
  for (const item of moods) {
    if (item.at <= start + 1) found = item.mood
    else break
  }
  return found
}

type ImmersiveLine = { text: string; start: number; fromBubble: boolean }

/** 只用于把气泡正文和音频段对齐；不改变实际显示文本。 */
function normalizeForMatch(text: string): string {
  return stripMoodMarks(text).replace(/\s+/g, '').trim()
}

export default function ImmersiveView({
  friend,
  messages,
  stream,
  segments,
  onSend,
  onStop,
  onClose,
  onOpenCall,
  variant = 'desktop',
  myName: myNameProp = '我',
  onMyNameChange,
}: {
  friend: Friend
  messages: Message[]
  stream?: StreamState
  segments: AudioSegmentEvent[]
  onSend: (text: string) => void
  onStop: () => void
  onClose: () => void
  /** v2.3（D126）：直接进语音通话 */
  onOpenCall?: () => void
  /** v3.0（D140）：手机壳里用同一份组件，只换手机需要的那点东西（版式不变） */
  variant?: 'desktop' | 'mobile'
  /** v3.0（B9 / D142）：沉浸模式里"我"叫什么 —— 由外面传（存 DB，两端一致） */
  myName?: string
  onMyNameChange?: (name: string) => void
}) {
  const mobile = variant === 'mobile'
  const [draft, setDraft] = useState('')
  const [index, setIndex] = useState(0)
  const [mode, setMode] = useState<'her' | 'me'>('her')
  const [auto, setAuto] = useState(() => localStorage.getItem(AUTO_KEY) === '1')
  // B9：以前存在 localStorage（每个壳各叫各的）→ 现在由外面传（DB 里一份）
  const [myName, setMyName] = useState(myNameProp || '我')
  const [editingName, setEditingName] = useState(false)
  const [brokenPortrait, setBrokenPortrait] = useState<string | null>(null)
  const [displayMode, setDisplayMode] = useState<'live2d' | 'portrait'>('live2d')
  const [live2dFailed, setLive2dFailed] = useState(false)
  const [speaking, setSpeaking] = useState(false)
  // v3.8：立绘元素（用来"重播动作"，见下面的 effect）
  const portraitRef = useRef<HTMLImageElement | null>(null)
  const [transform, setTransform] = useState<Transform>(
    friend.portrait_transform ?? DEFAULT_TRANSFORM,
  )
  const [adjusting, setAdjusting] = useState(false)
  // D126：沉浸模式里的语音输入（按住说话 → 识别 → 直接发）
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [speechHint, setSpeechHint] = useState<string | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const recorderRef = useRef<MediaRecorder | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)
  const sentHistory = useMemo(
    () =>
      messages
        .filter((message) => message.role === 'user' && message.content.trim())
        .map((message) => message.content),
    [messages],
  )
  const inputHistory = useInputHistory(sentHistory)
  const { reset: resetInputHistory, onArrow: onInputArrow } = inputHistory

  const audioRef = useRef<HTMLAudioElement | null>(null)
  const audioUnlocked = useRef(false)
  const dragRef = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null)
  const saveTimer = useRef<number | null>(null)

  const streaming = stream?.content ?? ''
  const lastAssistant = useMemo(
    () =>
      [...messages]
        .reverse()
        .find((item) => item.role === 'assistant' && item.content.trim().length > 0),
    [messages],
  )
  const busy =
    stream !== undefined &&
    stream.status !== 'idle' &&
    stream.status !== 'error' &&
    stream.status !== 'partial'

  useEffect(() => {
    resetInputHistory()
  }, [friend.id, resetInputHistory])

  // ---- 她这句回复的段落 ----
  const paragraphs = useMemo<ImmersiveLine[]>(() => {
    if (busy) {
      const bubbles = (stream?.segments ?? [])
        .map((item) => stripMoodMarks(item).replace(/\n{2,}/g, '\n').trim())
        .filter(Boolean)
      if (bubbles.length > 0) {
        return bubbles.map((text) => ({ text, start: -1, fromBubble: true }))
      }
      return splitParagraphs(stripMoodMarks(streaming)).map((item) => ({
        ...item,
        fromBubble: false,
      }))
    }
    const ownBubbles = (lastAssistant?.segments ?? [])
      .map((item) => stripMoodMarks(item).replace(/\n{2,}/g, '\n').trim())
      .filter(Boolean)
    if (ownBubbles.length > 0) {
      return ownBubbles.map((text) => ({ text, start: -1, fromBubble: true }))
    }
    const own = lastAssistant?.audio_segments ?? []
    if (own.length > 0) {
      return own.map((item) => ({
        text: stripMoodMarks(item.text).replace(/\n{2,}/g, '\n'),
        start: -1,
        fromBubble: false,
      }))
    }
    return splitParagraphs(stripMoodMarks(lastAssistant?.content ?? '')).map((item) => ({
      ...item,
      fromBubble: false,
    }))
  }, [busy, lastAssistant, stream?.segments, streaming])

  // D110：这一轮到底是哪条消息 —— 流一开始后端就告诉我们了，不用猜
  const currentMessageId = busy ? (stream?.assistantId ?? null) : (lastAssistant?.id ?? null)

  // 新一轮 → 从第一段重新开始，并且**把她上一轮的语音停掉**
  const replyKey = readyKey(busy, stream?.assistantId, stream?.lastUserText, lastAssistant?.id)
  useEffect(() => {
    setIndex(0)
    setMode('her')
    const audio = audioRef.current
    if (audio) {
      audio.pause()
      audio.removeAttribute('src')
    }
  }, [replyKey])

  const safeIndex = Math.max(0, Math.min(index, Math.max(0, paragraphs.length - 1)))
  const current = paragraphs[safeIndex]?.text ?? ''
  const canAdvance = safeIndex < paragraphs.length - 1
  const herTurnDone = !busy && !canAdvance && paragraphs.length > 0

  // ---- 这一段的语音（只认当前这条消息，D110）----
  const liveSegments = useMemo(
    () =>
      segments
        .filter((item) => item.messageId === currentMessageId && item.audioUrl)
        .sort((a, b) => a.index - b.index),
    [segments, currentMessageId],
  )
  const savedSegments = busy ? [] : (lastAssistant?.audio_segments ?? [])
  const currentLine = paragraphs[safeIndex]
  const currentKey = normalizeForMatch(currentLine?.text ?? '')
  const matchedLive = currentKey
    ? liveSegments.find((item) => normalizeForMatch(item.text) === currentKey)
    : undefined
  const matchedSaved = currentKey
    ? savedSegments.find((item) => normalizeForMatch(item.text) === currentKey)
    : undefined
  const indexedLive = liveSegments.find((item) => item.index === safeIndex)
  const indexedSaved = savedSegments.find((item) => item.index === safeIndex)
  // 一个气泡 = 一页。优先按正文精确对齐；只有气泡数和音频段数一致时才允许按下标兜底，
  // 避免音频段比气泡少时把下一句的声音错配到这一页。
  const segmentUrl =
    matchedLive?.audioUrl ??
    matchedSaved?.url ??
    (savedSegments.length === paragraphs.length
      ? indexedLive?.audioUrl ?? indexedSaved?.url ?? null
      : null)

  // D109：表情兜底 —— 没给标记 / 给了她没有的那张 → 「正常」→「平静」→ 大号头像
  // D112：**一段一个表情** —— 优先用这一段的音频携带的情绪，其次用流里按段给的，
  // 再退到整条消息的 mood（老消息 / 模型只给了一个标记的情况）
  const streamMoods = stream?.moods ?? []
  const paragraphStart = paragraphs[safeIndex]?.start ?? -1
  const storedSegmentMood =
    !busy && (lastAssistant?.moods?.length ?? 0) > 0
      ? (lastAssistant?.moods?.[safeIndex] ?? null)
      : null
  const segmentMood = currentLine?.fromBubble
    ? // 先信后端按段对齐的 moods；音频分段的 mood 只作兜底。
      // 2026-11-08 用户实测：音频段 emotion 可能全挂成同一个，反而盖掉
      // 真正按段算出来的情绪，导致第一句之后表情不换。
      storedSegmentMood ??
      streamMoods[safeIndex]?.mood ??
      matchedLive?.mood ??
      matchedSaved?.mood ??
      (busy ? null : lastAssistant?.mood) ??
      ''
    : // 老消息 / 无气泡：保留原来的“偏移 → 音频 → 落库段情绪”顺序。
      moodAt(streamMoods, paragraphStart) ??
      matchedLive?.mood ??
      matchedSaved?.mood ??
      liveSegments[safeIndex]?.mood ??
      savedSegments[safeIndex]?.mood ??
      lastAssistant?.moods?.[safeIndex] ??
      (busy ? null : lastAssistant?.mood) ??
      ''
  const available = friend.portrait_emotions ?? []
  const wanted = segmentMood
  // v3.6：先看这个情绪有没有立绘；没有就试近义（MOOD_ALIAS），最后才退「正常」/「平静」
  const moodCandidates = [wanted, ...(wanted ? (MOOD_ALIAS[wanted] ?? []) : [])]
  const mood =
    (available.length === 0 ? wanted : moodCandidates.find((item) => item && available.includes(item))) ||
    (available.includes('正常')
      ? '正常'
      : available.includes('平静')
        ? '平静'
        : (available[0] ?? wanted ?? '平静'))
  // D440：Live2D 用模型自己的标签表，不能跟着立绘的可用图片一起回退。
  const live2dMood = segmentMood || mood
  // v3.2（D145）：立绘**两端都用原图** —— 用户实测"手机端太模糊"。
  // 不慢靠三件事：磁盘缓存 + 客户端缓存头 + 进聊天先把她所有立绘预取好。
  const portraitUrl = `/api/friends/${friend.id}/portraits/${encodeURIComponent(mood)}`
  const portraitOk = brokenPortrait !== portraitUrl
  const onImage = Boolean(friend.background_url)

  // 2026-11-07（D428）：沉浸模式默认 Live2D，可切回静态立绘；选择按朋友记。
  useEffect(() => {
    // v7.43（D441）：默认呈现方式由朋友人格设置决定；右上角仍可临时预览另一种。
    setDisplayMode(friend.visual_mode === 'portrait' ? 'portrait' : 'live2d')
    setLive2dFailed(false)
  }, [friend.id, friend.visual_mode])

  const chooseDisplay = useCallback(
    (next: 'live2d' | 'portrait') => {
      setDisplayMode(next)
      setLive2dFailed(false)
    },
    [],
  )
  const markLive2DUnavailable = useCallback(() => {
    setLive2dFailed(true)
  }, [])

  // v3.7：进沉浸就把她的立绘**都预取一遍**（原图 ~280KB/张）。
  // 为什么要它：情绪切换时 `<img>` 会重挂载（为了重播动作），图要是在缓存里，换脸是"瞬发"；
  // 没预热的话会白一下 —— 那才是真正的"闪"。
  useEffect(() => {
    const emotions = friend.portrait_emotions ?? []
    if (emotions.length === 0) return
    for (const emotion of emotions) {
      const image = new Image()
      image.src = `/api/friends/${friend.id}/portraits/${encodeURIComponent(emotion)}`
    }
  }, [friend.id, friend.portrait_emotions])

  // v3.9：情绪切换 → 用 Web Animations API **显式播一遍**那段小动作。
  // （不重挂载、不闪；也绕开"摘类再挂类浏览器可能不重播"的坑。）
  useEffect(() => {
    const element = portraitRef.current
    if (!element) return
    const next = motionClassOf(mood)
    if (next === 'motion-plain') return // 正常/平静不折腾
    const frames = motionKeyframes(
      next,
      element.clientWidth || 300,
      element.clientHeight || 400,
    )
    if (!frames) return
    const animation = element.animate(frames, {
      duration: MOTION_FRAMES[next].ms,
      easing: 'cubic-bezier(0.22, 0.8, 0.3, 1)',
    })
    return () => animation.cancel()
  }, [mood])

  const advance = useCallback(() => {
    if (index + 1 < paragraphs.length) {
      setIndex(index + 1)
      return
    }
    // 她已经说完了 → 轮到你（同一个框变成输入）
    if (paragraphs.length > 0 && !busy) setMode('me')
  }, [index, paragraphs.length, busy])

  // 这一段有语音就播它
  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    if (!segmentUrl || adjusting) {
      if (!adjusting) {
        audio.pause()
        audio.removeAttribute('src')
      }
      return
    }
    audio.src = segmentUrl
    void audio.play().catch(() => undefined)
  }, [segmentUrl, adjusting])

  // 自动模式：语音播完翻页；没语音就等一会儿
  // 2026-10-02：她配了语音 → **必须等这一段音频到位**（原来用同一个 2.4s 短兜底，
  // 本地合成要好几秒 → 文字抢跑、音频永远慢一步；用户实测"文字先蹦出来，半天没声音"）。
  const voiceOn = Boolean(friend.voice_reply_enabled && friend.voice_id)
  useEffect(() => {
    if (!auto || !canAdvance || segmentUrl || mode !== 'her') return
    const timer = window.setTimeout(advance, voiceOn ? VOICE_WAIT_MS : AUTO_NO_AUDIO_MS)
    return () => window.clearTimeout(timer)
  }, [auto, canAdvance, segmentUrl, advance, safeIndex, mode, voiceOn])

  useEffect(() => {
    localStorage.setItem(AUTO_KEY, auto ? '1' : '0')
  }, [auto])

  // 换表情的一声轻音效（D131）：**只有她真有立绘时**才放（没立绘就别出声）
  useEffect(() => {
    if ((friend.portrait_emotions?.length ?? 0) > 0) playMoodSound(mood, friend.id)
  }, [mood, friend.portrait_emotions])

  // D194：进沉浸就先把 9 个音效抓成本地 blob —— 不然每次换表情都要现拉 200~400KB，
  // 动作演完了那一声才到（用户实测报的"动作和音效对不上"就是这个）。
  useEffect(() => {
    if ((friend.portrait_emotions?.length ?? 0) > 0) void preloadMoodSounds()
  }, [friend.portrait_emotions])

  // B9：外面的"我"变了（比如手机上改了、电脑上刷新）→ 跟上
  useEffect(() => {
    setMyName(myNameProp || '我')
  }, [myNameProp])

  // v3.14（D182）：沉浸模式开着就由**这一屏**出声（普通聊天那条语音条会自觉静音；
  // 桌宠那边的判据是"主窗口在不在前台"，这边开着时通常就在前台）。
  useEffect(() => {
    setSurfaceActive('immersive', true)
    return () => setSurfaceActive('immersive', false)
  }, [])

  useEffect(() => {
    const audio = audioRef.current
    return () => {
      if (audio) {
        audio.pause()
        audio.removeAttribute('src')
      }
    }
  }, [])

  // 键盘：Esc 退出；空格/回车翻页（在输入框里就别抢）
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (tag === 'TEXTAREA' || tag === 'INPUT') return
      if (event.key === ' ' || event.key === 'Enter') {
        event.preventDefault()
        if (mode === 'me') return
        advance()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [advance, onClose, mode])

  // ---- 立绘的拖动 / 缩放（D110）----
  function persistTransform(next: Transform) {
    setTransform(next)
    if (saveTimer.current) window.clearTimeout(saveTimer.current)
    saveTimer.current = window.setTimeout(() => {
      void api.updateFriend(friend.id, { portrait_transform: next }).catch(() => undefined)
    }, 600)
  }

  function onPortraitPointerDown(event: React.PointerEvent) {
    if (!adjusting) return
    event.stopPropagation()
    dragRef.current = {
      x: event.clientX,
      y: event.clientY,
      ox: transform.x,
      oy: transform.y,
    }
    ;(event.target as HTMLElement).setPointerCapture?.(event.pointerId)
  }

  function onPortraitPointerMove(event: React.PointerEvent) {
    const drag = dragRef.current
    if (!adjusting || !drag) return
    event.stopPropagation()
    persistTransform({
      ...transform,
      x: drag.ox + (event.clientX - drag.x),
      y: drag.oy + (event.clientY - drag.y),
    })
  }

  function onPortraitPointerUp(event: React.PointerEvent) {
    if (!dragRef.current) return
    event.stopPropagation()
    dragRef.current = null
  }

  function onPortraitWheel(event: React.WheelEvent) {
    if (!adjusting) return
    event.stopPropagation()
    const next = Math.min(3, Math.max(0.3, transform.scale - event.deltaY / 800))
    persistTransform({ ...transform, scale: Number(next.toFixed(3)) })
  }

  // ---------- 手机专属（variant='mobile'；桌面这几条一律不生效）----------

  /** 让 <audio> 在手机上能自动播：第一次用户手势时放一段"空音频"解锁。 */
  function unlockAudio() {
    if (!mobile || audioUnlocked.current) return
    audioUnlocked.current = true
    try {
      void new Audio(SILENT_WAV).play().catch(() => undefined)
    } catch {
      // 解锁失败不影响别的事
    }
  }

  function submit() {
    const text = draft.trim()
    if (!text || busy) return
    setDraft('')
    resetInputHistory()
    setMode('her')
    onSend(text)
  }

  // ── 语音输入（D126）：按住说话 → 识别 → **直接发**（和普通聊天同款，D124）────
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
        const blob = new Blob(chunksRef.current, {
          type: recorder.mimeType || 'audio/webm',
        })
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
      setSpeechHint(`说了：${text}`)
      setDraft('')
      setMode('her')
      onSend(text) // 识别完直接发（D124）
    } catch (e) {
      setSpeechHint(e instanceof Error ? e.message : String(e))
    } finally {
      setTranscribing(false)
    }
  }

  return (
    <div
      className={`fixed inset-0 z-40 flex flex-col overflow-hidden bg-page ${mobile ? 'h-dvh' : ''}`}
      // v3.6：安全区**只留上边**。
      // 起因（用户实测）：以前四条边都加 padding，而曲面屏/圆角手机的
      // `safe-area-inset-left/right` 不是 0 → 整个覆盖层被往里缩，对话框贴不到屏幕左边（看着"错位"），
      // 四边还会露出 `bg-page` 的窄边。现在背景/对话框**通铺到边**，
      // 该躲开刘海的东西（顶部按钮 / 底部文字）各自用 max(...) 内边距躲。
      style={{
        paddingTop: 'env(safe-area-inset-top)',
        // 调立绘的时候**整屏都归我们**（不然手机上一拖就被浏览器/WebView 当成"拖页面"）
        touchAction: adjusting ? 'none' : 'pan-x pan-y',
      }}
      onPointerDown={(event) => {
        // 手机：第一次点屏幕先把音频解锁（浏览器要求"用户手势之后"才让自动播）
        unlockAudio()
        // 顶部那条（按钮）和输入框：**不要抢它们的手势** —— 调立绘时点按钮得能点到
        const source = event.target as HTMLElement | null
        if (source?.closest('header, button, input, textarea, a')) return
        // 调立绘时：点屏幕任何地方都能拖立绘（手机屏幕小，非要点在立绘上很难）
        if (adjusting) onPortraitPointerDown(event)
      }}
      onPointerMove={(event) => {
        const source = event.target as HTMLElement | null
        if (source?.closest('header, button, input, textarea, a')) return
        if (adjusting) onPortraitPointerMove(event)
      }}
      onPointerUp={(event) => {
        if (adjusting) onPortraitPointerUp(event)
      }}
      onClick={() => {
        if (mode === 'her' && !adjusting) advance()
      }}
    >
      {/* 出错 / 工具调用 / 思考（D134）：左上角摊开，别让人干等 */}
      <StreamStatusCard stream={stream} />

      {friend.background_url ? (
        <>
          <img
              src={withWidth(friend.background_url, mobile ? 1280 : 0)}
            alt=""
            className="pointer-events-none absolute inset-0 h-full w-full object-cover"
          />
          <div className="pointer-events-none absolute inset-0 bg-scrim/40" aria-hidden />
        </>
      ) : (
        <div
          className="pointer-events-none absolute inset-0"
          style={{
            background:
              'radial-gradient(120% 80% at 50% 0%, var(--color-accent) 0%, transparent 55%)',
            opacity: 0.14,
          }}
          aria-hidden
        />
      )}

      {/* 顶部：自动 / 调整立绘 / 退出 */}
      <header
        // z-30 + 不透明底：**立绘放大后会往上顶到这一条**，以前它盖在按钮上，用户点不到
        //（2026-09-22 用户实测："立绘会遮挡按钮，让我点不到确认调整"）
        className={`relative z-30 flex h-11 shrink-0 items-center gap-2 px-4 ${
          adjusting ? (onImage ? 'bg-scrim/45 backdrop-blur-sm' : 'bg-page/85 backdrop-blur-sm') : ''
        }`}
        // 顶部按钮躲开圆角/刘海（背景仍然通铺，所以只给内容加内边距）
        style={{
          paddingLeft: 'max(1rem, env(safe-area-inset-left))',
          paddingRight: 'max(1rem, env(safe-area-inset-right))',
        }}
        onClick={(event) => event.stopPropagation()}
      >
        <span className={`text-[12.5px] ${onImage ? 'text-white/70' : 'text-muted'}`}>
          {busy ? '正在说…' : adjusting ? (mobile ? '拖动移动 · 用 ＋/－ 缩放' : '拖动移动 · 滚轮缩放') : ''}
        </span>
        <button
          type="button"
          onClick={() => chooseDisplay(displayMode === 'live2d' ? 'portrait' : 'live2d')}
          title="沉浸模式的呈现方式：Live2D 会跟着情绪和口型动；立绘是静态图"
          className={`ml-auto rounded-full border px-3 py-1 text-[11.5px] transition-colors ${
            displayMode === 'live2d'
              ? 'border-accent/60 bg-accent/15 text-accent'
              : onImage
                ? 'border-white/25 bg-black/35 text-white/80 hover:border-white/50'
                : 'border-line/80 bg-surface/70 text-muted hover:border-accent/60 hover:text-accent'
          }`}
        >
          {displayMode === 'live2d' ? 'Live2D' : '立绘'}
        </button>
        <button
          type="button"
          onClick={() => setAuto((prev) => !prev)}
          title="自动：这一段说完自动接下一段"
          className={`rounded-full border px-3 py-1 text-[11.5px] transition-colors ${
            auto
              ? 'border-accent/60 bg-accent/15 text-accent'
              : onImage
                ? 'border-white/25 bg-black/35 text-white/80 hover:border-white/50'
                : 'border-line/80 bg-surface/70 text-muted hover:border-accent/60 hover:text-accent'
          }`}
        >
          {auto ? '自动 ●' : '自动 ○'}
        </button>
        <button
          type="button"
          onClick={() => setAdjusting((prev) => !prev)}
          title="拖动 / 滚轮调整立绘的位置和大小（按朋友保存）"
          className={`rounded-full border px-3 py-1 text-[11.5px] transition-colors ${
            adjusting
              ? 'border-accent/60 bg-accent/15 text-accent'
              : onImage
                ? 'border-white/25 bg-black/35 text-white/80 hover:border-white/50'
                : 'border-line/80 bg-surface/70 text-muted hover:border-accent/60 hover:text-accent'
          }`}
        >
          {adjusting ? '调整中' : '调整立绘'}
        </button>
        {adjusting && (
          // 手机上没法用滚轮 → 给两个按钮缩放（用户："双指不要了，但得能放大"）
          <>
            <button
              type="button"
              aria-label="缩小立绘"
              onClick={() =>
                persistTransform({
                  ...transform,
                  scale: Number(Math.max(0.3, transform.scale - 0.1).toFixed(3)),
                })
              }
              className={`h-7 w-7 rounded-full border text-[14px] leading-none ${
                onImage ? 'border-white/25 bg-black/35 text-white/80' : 'border-line/80 bg-surface/70 text-muted'
              }`}
            >
              －
            </button>
            <button
              type="button"
              aria-label="放大立绘"
              onClick={() =>
                persistTransform({
                  ...transform,
                  scale: Number(Math.min(3, transform.scale + 0.1).toFixed(3)),
                })
              }
              className={`h-7 w-7 rounded-full border text-[14px] leading-none ${
                onImage ? 'border-white/25 bg-black/35 text-white/80' : 'border-line/80 bg-surface/70 text-muted'
              }`}
            >
              ＋
            </button>
          </>
        )}
        {adjusting && (
          <button
            type="button"
            onClick={() => persistTransform(DEFAULT_TRANSFORM)}
            className={`rounded-full border px-3 py-1 text-[11.5px] ${
              onImage
                ? 'border-white/25 bg-black/35 text-white/80'
                : 'border-line/80 bg-surface/70 text-muted'
            }`}
          >
            复位
          </button>
        )}
        {onOpenCall && (
          <button
            type="button"
            onClick={onOpenCall}
            title="语音通话：不显示文字，她说完自动听你说"
            className={`rounded-full border px-3 py-1 text-[11.5px] transition-colors ${
              onImage
                ? 'border-white/25 bg-black/35 text-white/80 hover:border-white/50'
                : 'border-line/80 bg-surface/70 text-muted hover:border-accent/60 hover:text-accent'
            }`}
          >
            通话
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          title="退出沉浸模式（Esc）"
          className={`rounded-full border px-3 py-1 text-[11.5px] transition-colors ${
            onImage
              ? 'border-white/25 bg-black/35 text-white/85 hover:border-white/50'
              : 'border-line/80 bg-surface/70 text-muted hover:border-accent/60 hover:text-accent'
          }`}
        >
          退出
        </button>
      </header>

      {/* 立绘 / 大号头像（手机上给立绘留够地方：至少 40dvh，别被下面那个框挤没） */}
      <div
        className={`relative z-10 flex min-h-0 flex-1 items-end justify-center pb-2 ${
          mobile ? 'min-h-[40dvh] px-3' : 'px-6'
        }`}
      >
        {displayMode === 'live2d' && !live2dFailed ? (
          <div
            onPointerDown={onPortraitPointerDown}
            onPointerMove={onPortraitPointerMove}
            onPointerUp={onPortraitPointerUp}
            onWheel={onPortraitWheel}
            style={{
              transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
              transformOrigin: 'bottom center',
              cursor: adjusting ? 'grab' : 'default',
              touchAction: adjusting ? 'none' : 'pan-x pan-y',
            }}
            className={`flex h-full items-end ${adjusting ? 'ring-1 ring-accent/50' : ''}`}
          >
            <Live2DCharacter
              friendId={friend.id}
              mood={live2dMood}
              speaking={speaking}
              speakText={voiceOn ? undefined : current}
              onUnavailable={markLive2DUnavailable}
              className="max-h-full max-w-full"
            />
          </div>
        ) : portraitOk ? (
          // 外层 = 用户调的位置/大小（D110）；内层 = 情绪动作（D111）。两层互不打架。
          <div
            onPointerDown={onPortraitPointerDown}
            onPointerMove={onPortraitPointerMove}
            onPointerUp={onPortraitPointerUp}
            onWheel={onPortraitWheel}
            style={{
              transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
              transformOrigin: 'bottom center',
              cursor: adjusting ? 'grab' : 'default',
              // 立绘自己处理拖动（不然浏览器/WebView 会来抢这个手势）
              touchAction: adjusting ? 'none' : 'pan-x pan-y',
            }}
            className={`flex h-full items-end ${adjusting ? 'ring-1 ring-accent/50' : ''}`}
          >
            <img
              // v3.8：**元素不重挂载**（key 只跟朋友走）—— 重挂载会闪。
              // 动作靠上面的 effect"摘类→回流→挂回"来重播。
              key={friend.id}
              ref={portraitRef}
              src={portraitUrl}
              alt={mood}
              onError={() => setBrokenPortrait(portraitUrl)}
              draggable={false}
              className="portrait-motion max-h-full w-auto max-w-full object-contain drop-shadow-[0_18px_40px_rgba(0,0,0,0.3)]"
            />
          </div>
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-3">
            <Avatar name={friend.name} url={friend.avatar_url} size={230} />
            <span className={`text-[11px] ${onImage ? 'text-white/60' : 'text-faint'}`}>
    她还没有立绘 · 在「设置 → 立绘包」里导入，然后在人格里选这个包
            </span>
          </div>
        )}
      </div>

      {/* **唯一**那个对话框：说话的人换，框不换（D110） */}
      <div
        className="relative z-10 shrink-0 border-t border-white/15 bg-black/60 backdrop-blur-sm"
        // 框的**背景**通铺到底；文字躲开底部手势条（v3.6）
        style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
        onClick={(event) => {
          event.stopPropagation()
          if (mode === 'her') advance()
        }}
      >
        <span
          key={`plate-${live2dMood}`}
          className="mood-pop absolute -top-3.5 left-8 flex items-center gap-1.5 rounded-md border border-white/20 bg-black/70 px-3 py-0.5 text-[13px] text-white/90"
        >
          {mode === 'her' ? (
            <>
              {friend.name}
              <span className="text-white/60">（{live2dMood}）</span>
            </>
          ) : editingName ? (
            <input
              autoFocus
              value={myName}
              onChange={(event) => {
                const next = event.target.value.slice(0, 12)
                setMyName(next)
                onMyNameChange?.(next)
              }}
              onBlur={(event) => {
                // 清空就回到默认"我"（存 DB 的是最后这个值）
                const next = event.target.value.trim() || '我'
                setMyName(next)
                onMyNameChange?.(next)
                setEditingName(false)
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  const next = (event.target as HTMLInputElement).value.trim() || '我'
                  setMyName(next)
                  onMyNameChange?.(next)
                  setEditingName(false)
                }
              }}
              className="w-[74px] bg-transparent text-[13px] text-white outline-none"
            />
          ) : (
            <button
              type="button"
              onClick={() => setEditingName(true)}
              title="点一下改叫法（比如你的名字）"
              className="text-[13px] text-white/90"
            >
              {myName || '我'}
              <span className="ml-1 text-white/40">✎</span>
            </button>
          )}
        </span>

        {mode === 'her' ? (
          // 手机上：字号/内边距小一档 + **限高可滚**，不然长句会把立绘整个挡住（用户实测反馈）
          <div className={mobile ? 'max-h-[36dvh] overflow-y-auto px-4 py-3' : 'px-8 py-5'}>
            <div
              className={`select-text whitespace-pre-wrap text-white ${
                mobile ? 'min-h-[56px] text-[15px] leading-[1.7]' : 'min-h-[76px] text-[16px] leading-[1.85]'
              }`}
            >
              {current ? (
                <MarkdownText text={current} tone="inverse" />
              ) : busy ? (
                '……'
              ) : stream?.status === 'error' ? (
                '（这次没答上 —— 出错原因看左上角）'
              ) : (
                '（她在等你说话）'
              )}
            </div>
            {canAdvance ? (
              <span className="absolute bottom-3 right-6 animate-bounce text-[12px] text-white/60">
                ▼ 点一下继续
              </span>
            ) : (
              herTurnDone && (
                <span className="absolute bottom-3 right-6 text-[11.5px] text-white/50">
                  点一下轮到你 ▸
                </span>
              )
            )}
          </div>
        ) : (
          <div className={mobile ? 'px-4 py-3' : 'px-8 py-4'}>
            <textarea
              ref={composerRef}
              autoFocus
              rows={2}
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value)
                resetInputHistory()
              }}
              onKeyDown={(event) => {
                if (
                  !mobile &&
                  onInputArrow(draft, event, (text) => {
                    setDraft(text)
                    requestAnimationFrame(() => {
                      const el = composerRef.current
                      if (el) el.setSelectionRange(text.length, text.length)
                    })
                  })
                ) {
                  return
                }
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  submit()
                }
              }}
              placeholder={`对 ${friend.name} 说…（Enter 发送，Shift+Enter 换行）`}
              className="h-[76px] w-full resize-none bg-transparent text-[16px] leading-[1.85] text-white outline-none placeholder:text-white/35"
            />
            <div className="mt-1 flex items-center justify-end gap-2">
              {(speechHint || recording || transcribing) && (
                <span className="mr-auto truncate text-[11.5px] text-white/60">
                  {recording ? '正在听…' : transcribing ? '识别中…' : speechHint}
                </span>
              )}
              <button
                type="button"
                onPointerDown={(event) => {
                  event.preventDefault()
                  void startRecording()
                }}
                onPointerUp={() => stopRecording()}
                onPointerLeave={() => recording && stopRecording()}
                title="按住说话（松手识别，识别完直接发出去）"
                className={`rounded-lg border px-3 py-1 text-[12px] transition-colors ${
                  recording
                    ? 'border-accent bg-accent/20 text-white'
                    : 'border-white/20 text-white/70 hover:border-white/40'
                }`}
              >
                {recording ? '松手发送' : '按住说话'}
              </button>
              <button
                type="button"
                onClick={() => setMode('her')}
                className="rounded-lg border border-white/20 px-3 py-1 text-[12px] text-white/70 hover:border-white/40"
              >
                听她说
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={!draft.trim() || busy}
                className="rounded-lg bg-accent px-4 py-1 text-[12.5px] text-white disabled:opacity-40"
              >
                说
              </button>
            </div>
          </div>
        )}

        {/* 她在说的时候，右下角给一个"停下" */}
        {busy && (
          <button
            type="button"
            onClick={onStop}
            className="absolute bottom-2 right-6 text-[11.5px] text-white/50 hover:text-white/80"
          >
            停下
          </button>
        )}
      </div>

      <audio
        ref={audioRef}
        onPlay={() => setSpeaking(true)}
        onPause={() => setSpeaking(false)}
        onEnded={() => {
          setSpeaking(false)
          if (auto && mode === 'her' && canAdvance) advance()
        }}
        className="hidden"
      />
    </div>
  )
}

/** 这一轮的唯一标识：流里用 assistantId，完事用消息 id（D110）。 */
function readyKey(
  busy: boolean,
  assistantId: string | null | undefined,
  lastUserText: string | null | undefined,
  messageId: string | undefined,
): string {
  // 同一轮从“流式”切到“已落库”时 id 不变；不能用不同的前缀/状态当 key，
  // 否则 done 一刷新就会把用户已经翻到的第 2/3 泡重置回第一句。
  const stableId = assistantId ?? messageId ?? ''
  if (stableId) return `turn:${stableId}`
  return busy ? `pending:${lastUserText ?? ''}` : 'turn:'
}
