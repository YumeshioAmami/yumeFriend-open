import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../index.css'
import { isTablet } from './isTablet.ts'
import MobileApp from './MobileApp.tsx'
import TabletApp from './TabletApp.tsx'
import NetworkBanner from '../shared/NetworkBanner.tsx'
import { applyTheme, readThemeChoice } from '../shared/theme'

// 手机上**不套界面缩放**（用户拍板：手机端不要缩放档位，字号跟着系统走，
// 触控尺寸按 ≥44px 的约定写死）。

// 但**主题要套**：以前手机壳只跑默认浅色 → 晚上看着难受（用户实测反馈）。
// 默认是"跟随系统"，所以系统开了深色，手机壳就跟着深。
applyTheme(readThemeChoice())
try {
  const media = window.matchMedia('(prefers-color-scheme: dark)')
  const sync = () => {
    if (readThemeChoice() === 'system') applyTheme('system')
  }
  media.addEventListener('change', sync)
  // 从别的页面/别的 App 切回来时也对一下（设置页里可能刚改过）
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) applyTheme(readThemeChoice())
  })
} catch {
  // 老 WebView 没有 matchMedia 也不影响用
}

// D200：同一个入口，宽屏（平板）换成两栏；手机端走原来那套，一行不动。
const tablet = isTablet()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* D196：断线时顶上一条黄色横幅 + 「重新加载」抓手（手机在外面走流量时很有用） */}
    <NetworkBanner />
    {tablet ? <TabletApp /> : <MobileApp />}
  </StrictMode>,
)
