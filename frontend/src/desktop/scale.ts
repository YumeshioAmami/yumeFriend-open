/**
 * 界面缩放（用户要的：**手动、可控**，不要自动）。
 *
 * 为什么不放大字号了事：界面里大量尺寸是写死的 px（`text-[13px]`、`h-11` …），
 * 只把字号调大，文字和它周围的框会错位。
 *
 * 所以这里直接用 Chromium 的 `zoom`——**和浏览器 Ctrl+滚轮是同一套东西**，
 * 整页等比例放大，布局比例完全不变。用户在浏览器里怎么习惯，这里就怎么用。
 *
 * 存 localStorage：这是"**这台电脑上**看着舒服的字号"，跟后端设置无关，
 * 也不该跟着账号/数据库走。
 */

const STORAGE_KEY = 'yumefriend.ui_scale'

/** 五档，够用且不容易调出怪比例（用户说"太大太小都会出问题"）。 */
export const UI_SCALE_CHOICES: { value: number; label: string }[] = [
  { value: 0.9, label: '小 90%' },
  { value: 1, label: '标准 100%' },
  { value: 1.15, label: '大 115%' },
  { value: 1.3, label: '更大 130%' },
  { value: 1.5, label: '特大 150%' },
]

function clampScale(value: number): number {
  if (!Number.isFinite(value)) return 1
  return Math.min(2, Math.max(0.7, value))
}

export function readUiScale(): number {
  const saved = Number(localStorage.getItem(STORAGE_KEY))
  return saved ? clampScale(saved) : 1
}

export function saveUiScale(scale: number): void {
  localStorage.setItem(STORAGE_KEY, String(scale))
}

/** 真正生效的那一行：整页缩放。 */
export function applyUiScale(scale: number): void {
  document.documentElement.style.zoom = String(clampScale(scale))
}

/**
 * 档位之间上/下一格。
 * 如果当前值不在档位上（比如手改过 localStorage），就往那个方向找最近的档。
 */
export function stepUiScale(current: number, direction: 1 | -1): number {
  const values = UI_SCALE_CHOICES.map((c) => c.value)
  const index = values.findIndex((v) => Math.abs(v - current) < 0.001)
  if (index >= 0) {
    return values[Math.min(values.length - 1, Math.max(0, index + direction))]
  }
  const sorted = direction > 0 ? values : [...values].reverse()
  return sorted.find((v) => (direction > 0 ? v > current : v < current)) ?? (direction > 0 ? values[values.length - 1] : values[0])
}
