/**
 * 平板判定（v3.16 / D200）—— 同一个 `/m/` 入口，宽屏时换两栏。
 *
 * 口径跟安卓自己的平板判定一致：**最短边 ≥ 600dp**（WebView 里 CSS px ≈ dp）。
 * 另外给 `?ui=tablet` / `?ui=mobile` 强制覆盖（验收 / 手动切换用）。
 *
 * 一进来判一次就够：旋转时最短边不变（手机的短边永远 <600），所以不用监听 resize。
 * 判定不命中 → 渲染原来的 `MobileApp`，手机端完全不受影响。
 */
export function isTablet(): boolean {
  try {
    const forced = new URLSearchParams(window.location.search).get('ui')
    if (forced === 'tablet') return true
    if (forced === 'mobile') return false
  } catch {
    // URL 解析不了就按屏幕尺寸判
  }
  try {
    const { width, height } = window.screen
    if (!width || !height) return false
    return Math.min(width, height) >= 600
  } catch {
    return false
  }
}
