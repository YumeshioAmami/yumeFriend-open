/**
 * 换表情时那一声"哒"（D131）：情绪 → 音效文件。
 *
 * ⚠️ **和 `backend/desktop/pet_render.py::MOOD_SOUNDS` 是同一张表**（改一边要改另一边）。
 * 音效文件是从 LingChat 那套按情绪的音效里挑的，放在 `public/sounds/`。
 * 只在**有立绘**的场景播（沉浸模式 / 带立绘的通话 / 桌宠），没立绘不放。
 */
const MOOD_SOUNDS: Record<string, string> = {
  happy: 'happy.wav',
  playful: 'playful.wav',
  shy: 'shy.wav',
  angry: 'angry.wav',
  surprised: 'surprised.wav',
  sad: 'sad.wav',
  love: 'love.wav',
  proud: 'proud.wav',
  puzzled: 'puzzled.wav',
}

/** 情绪 → 动作名（跟 pet_render.py 的 MOOD_MOTIONS / 前端 motionClassOf 同一张表）。 */
const MOOD_MOTIONS: [string[], string][] = [
  [['兴奋', '开心', '愉快', '高兴', '惊喜', '笑', '轻笑'], 'happy'],
  [['调皮', '坏笑', '狡黠'], 'playful'],
  [['害羞', '羞耻', '脸红'], 'shy'],
  [['生气', '愤怒', '厌恶', '不爽'], 'angry'],
  [['惊讶', '吃惊', '震惊'], 'surprised'],
  [['伤心', '难过', '无奈', '担心', '紧张', '慌张', '害怕'], 'sad'],
  [['心动', '喜欢', '爱慕', '软', '柔软', '宠溺'], 'love'],
  [['自信', '认真', '得意'], 'proud'],
  [['疑惑', '困惑', '懵'], 'puzzled'],
]

let lastPlayed = ''

/**
 * v3.17（2026-09-27 / D194）：**预加载**。
 *
 * 病根（用户实测报的）：以前每次换表情都 `new Audio('/sounds/xxx.wav')` ——
 * 也就是**每次都要从电脑那边现拉 230~390KB 的 wav**（手机上走组网/流量更慢）→
 * **立绘动作都演完了，那一声才到**，动作和音效对不上。
 *
 * 现在：进沉浸 / 通话（以及手机壳启动）时先把这些文件 fetch 成 blob（**一个会话只拉一次**），
 * 播的时候直接用本地 blob → 零网络、立刻响。
 */
const soundBlobs = new Map<string, string>()
let preloading: Promise<void> | null = null

/** 把情绪音效抓成本地 blob（幂等；失败不抛，播的时候退回网络地址）。 */
export function preloadMoodSounds(): Promise<void> {
  if (preloading) return preloading
  preloading = (async () => {
    const files = [...new Set(Object.values(MOOD_SOUNDS))]
    await Promise.all(
      files.map(async (file) => {
        if (soundBlobs.has(file)) return
        try {
          const response = await fetch(`/sounds/${file}`)
          if (!response.ok) return
          soundBlobs.set(file, URL.createObjectURL(await response.blob()))
        } catch {
          // 拉不到就算了：下面 playMoodSound 会退回原来的网络地址
        }
      }),
    )
  })()
  return preloading
}

/**
 * v3.6：`scope` 传朋友 id —— 以前只按情绪去重，**跨朋友会互相压制**
 * （阿月刚响过「调皮」，切到汐音也是「调皮」就不响了）。
 */
export function playMoodSound(mood: string, scope = ''): void {
  const key = `${scope}:${mood}`
  if (!mood || key === lastPlayed) return
  const name = MOOD_MOTIONS.find(([words]) => words.some((word) => mood.includes(word)))?.[1]
  const file = name ? MOOD_SOUNDS[name] : undefined
  if (!file) return
  lastPlayed = key
  try {
    const src = soundBlobs.get(file) ?? `/sounds/${file}`
    const audio = new Audio(src)
    audio.volume = 0.35
    void audio.play().catch(() => undefined)
    if (!soundBlobs.has(file)) void preloadMoodSounds() // 这次来不及了，下次别再等网络
  } catch {
    // 放不出来就算了（不影响任何事）
  }
}
/** 情绪 → 立绘动作的 CSS 类（跟 ImmersiveView 的 motionClassOf 同一张表）。 */
const MOTION_RULES: [string[], string][] = [
  [['兴奋', '开心', '愉快', '高兴', '惊喜'], 'motion-happy'],
  [['调皮', '坏笑', '狡黠'], 'motion-playful'],
  [['害羞', '羞耻', '脸红'], 'motion-shy'],
  [['生气', '愤怒', '厌恶', '不爽'], 'motion-angry'],
  [['惊讶', '吃惊', '震惊'], 'motion-surprised'],
  [['伤心', '难过', '无奈', '担心', '紧张', '慌张', '害怕'], 'motion-sad'],
  [['心动', '喜欢', '爱慕'], 'motion-love'],
  [['自信', '认真', '得意'], 'motion-proud'],
  [['疑惑', '困惑', '懵'], 'motion-puzzled'],
]

export function motionClassOf(mood: string): string {
  for (const [keywords, className] of MOTION_RULES) {
    if (keywords.some((word) => mood.includes(word))) return className
  }
  return 'motion-plain'
}
