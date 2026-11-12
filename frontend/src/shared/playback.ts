/**
 * 「同一时间只有一处出声」（v3.14 / D182）。
 *
 * 为什么要它：她的语音有三个玩家 —— 普通聊天那条语音条、沉浸/通话那一套、桌宠窗口
 * （native 那边自己播）。以前各播各的：开着桌宠在聊天界面聊天 → **两段一模一样的声音**，
 * 再打开沉浸模式 → **三段**（用户实测）。
 *
 * 现在的规矩：**声音跟着你正在看的那一屏走**
 *
 *   沉浸 / 通话开着      → 它出声（聊天窗口静音）
 *   否则窗口在前台        → 聊天窗口出声（桌宠静音）
 *   否则（你在别的程序里）→ 桌宠出声（窗口静音）
 *
 * 桌宠读不到 `document.hasFocus()` —— 它用 Win32 的"前台窗口是不是我们自己的主窗口"
 * 来判（`pet_native._main_window_in_front`），两边刚好互补：一边在前台，另一边就静默。
 * 所以**手动点播放不受这里限制**（用户自己点，永远能放）。
 */

export type PlaybackSurface = 'chat' | 'immersive' | 'call'

const active = new Set<PlaybackSurface>()
const listeners = new Set<() => void>()

function notify(): void {
  for (const listener of listeners) listener()
}

/** 某个"面"开着/关了（挂载时 true，卸载时 false）。 */
export function setSurfaceActive(surface: PlaybackSurface, isActive: boolean): void {
  if (isActive) active.add(surface)
  else active.delete(surface)
  notify()
}

/** 谁该出声（沉浸/通话优先于普通聊天）。 */
export function playbackOwner(): PlaybackSurface {
  if (active.has('call')) return 'call'
  if (active.has('immersive')) return 'immersive'
  return 'chat'
}

export function ownsPlayback(surface: PlaybackSurface): boolean {
  return playbackOwner() === surface
}

/** 主窗口是不是当前窗口 —— 不是的话，声音交给桌宠（它也在看"前台窗口"）。 */
export function pageFocused(): boolean {
  try {
    return typeof document === 'undefined' ? true : document.hasFocus()
  } catch {
    return true
  }
}

/** 能不能**自动**放：这一屏独占 + 窗口在前台。手动点播放不走这个。 */
export function canAutoPlay(surface: PlaybackSurface): boolean {
  return ownsPlayback(surface) && pageFocused()
}

/** 订阅"该谁出声"的变化（被抢走播放权时要停下来）。 */
export function onPlaybackChange(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
