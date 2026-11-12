/**
 * 图标（**不用 emoji** —— 用户明确要求）。
 *
 * 统一风格：24 视口、`currentColor`、圆头圆角、1.6 线宽。
 * 关键好处：描边跟着文字色走，所以浅色/深色**不用各写一套**，
 * 悬浮态也只是换个 text-* 类而已。
 */

import type { ReactNode } from 'react'

interface SvgProps {
  size?: number
  className?: string
  children: ReactNode
}

function Svg({ size = 16, className, children }: SvgProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
      focusable="false"
    >
      {children}
    </svg>
  )
}

/** 品牌标记：暖橙渐变底 + 白色的「星与环」（yume = 梦，环绕不张牙舞爪）。 */
export function LogoMark({ size = 32, className }: { size?: number; className?: string }) {
  // D128：logo = 「ゆ」（yume = 梦）放进伴侣橙渐变徽章里。
  // 图片跟托盘/程序图标/打包资源**同一份画法**生成（backend/desktop/icon.py），保证到处一致。
  return (
    <img
      src="/logo-192.png"
      width={size}
      height={size}
      className={className}
      alt=""
      aria-hidden
    />
  )
}
export function IconSun({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6" />
    </Svg>
  )
}

export function IconMoon({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M20.4 14.6A8.6 8.6 0 0 1 9.4 3.6a8.6 8.6 0 1 0 11 11z" />
    </Svg>
  )
}

/** 跟随系统：一块"显示器"，一眼能读成"由系统决定"。 */
export function IconSystem({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <rect x="2.8" y="4.2" width="18.4" height="12.6" rx="2.4" />
      <path d="M9 20.2h6M12 16.8v3.4" />
    </Svg>
  )
}

/** 设置：外圈 + 八颗齿 + 中心孔（比 sliders 更一眼可读）。 */
export function IconGear({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <circle cx="12" cy="12" r="7.2" />
      <circle cx="12" cy="12" r="2.9" />
      {Array.from({ length: 8 }, (_, i) => (
        <rect
          key={i}
          x="11.15"
          y="2.4"
          width="1.7"
          height="3.4"
          rx="0.85"
          transform={`rotate(${i * 45} 12 12)`}
        />
      ))}
    </Svg>
  )
}

/** 日志：终端窗口（它本来就是"控制台"）。 */
export function IconTerminal({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <rect x="2.8" y="4.4" width="18.4" height="15.2" rx="2.6" />
      <path d="M7.2 9.6 9.9 12l-2.7 2.4M12.8 14.8h4" />
    </Svg>
  )
}

export function IconBell({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M12 3.4a5.4 5.4 0 0 0-5.4 5.4c0 4-1.3 5.4-1.3 5.4h13.4s-1.3-1.4-1.3-5.4A5.4 5.4 0 0 0 12 3.4z" />
      <path d="M9.9 17.4a2.2 2.2 0 0 0 4.2 0" />
    </Svg>
  )
}

/** 通知被挡住 / 不支持：铃铛加一道斜杠。 */
export function IconBellOff({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M12 3.4a5.4 5.4 0 0 0-5.4 5.4c0 4-1.3 5.4-1.3 5.4h13.4s-1.3-1.4-1.3-5.4A5.4 5.4 0 0 0 12 3.4z" />
      <path d="M9.9 17.4a2.2 2.2 0 0 0 4.2 0" />
      <path d="M3.6 3.6l16.8 16.8" />
    </Svg>
  )
}

// ── 下面是 v2 设置页用的（用途图标 + 分页图标），风格同上 ──────────

/** 聊天：对话气泡。 */
export function IconChat({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M20.4 12.2c0 3.9-3.8 7-8.4 7-1 0-2-.14-2.9-.4L4.2 20.4l1.3-3.6a6.6 6.6 0 0 1-2-4.6c0-3.9 3.8-7 8.5-7s8.4 3.1 8.4 7z" />
    </Svg>
  )
}

/** 视觉 / 看图。 */
export function IconEye({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M2.6 12S6 5.8 12 5.8 21.4 12 21.4 12 18 18.2 12 18.2 2.6 12 2.6 12z" />
      <circle cx="12" cy="12" r="3" />
    </Svg>
  )
}

/** 语音：三根高低不同的声波。 */
export function IconWave({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M5 10v4M9 7v10M12 4.8v14.4M15 7v10M19 10v4" />
    </Svg>
  )
}

export function IconMic({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <rect x="9" y="3" width="6" height="10" rx="3" />
      <path d="M5.6 11.4a6.4 6.4 0 0 0 12.8 0M12 17.8V21M9 21h6" />
    </Svg>
  )
}

/** 图片生成 / 图片。 */
export function IconImage({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <rect x="3" y="4.4" width="18" height="15.2" rx="2.6" />
      <circle cx="8.6" cy="9.8" r="1.6" />
      <path d="M3.6 17.2 9 12.4l3.4 3 3.2-2.6 4.8 4.4" />
    </Svg>
  )
}

/** 模型：芯片。 */
export function IconChip({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <rect x="6.6" y="6.6" width="10.8" height="10.8" rx="2.4" />
      <path d="M10 3.4v3.2M14 3.4v3.2M10 17.4v3.2M14 17.4v3.2M3.4 10h3.2M3.4 14h3.2M17.4 10h3.2M17.4 14h3.2" />
    </Svg>
  )
}

/** 记忆：叠起来的卡片（记录）。 */
export function IconStack({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <rect x="3.4" y="4.4" width="12.6" height="12.6" rx="2.4" />
      <path d="M8 20.4h10.2a2.4 2.4 0 0 0 2.4-2.4V8" />
    </Svg>
  )
}

/** 关于：圆圈里的 i。 */
export function IconInfo({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <circle cx="12" cy="12" r="8.6" />
      <path d="M12 11v5.2M12 7.9v.1" />
    </Svg>
  )
}

// ── v3.7（D165）：列表 / 手机也用矢量，不再拿 emoji 当按钮 ──────────────

/** 删除：垃圾桶（朋友列表项上的那个小按钮）。 */
export function IconTrash({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M4.2 6.6h15.6" />
      <path d="M9.4 6.6V4.9a1.3 1.3 0 0 1 1.3-1.3h2.6a1.3 1.3 0 0 1 1.3 1.3v1.7" />
      <path d="M6.5 6.6l.9 12.1a1.8 1.8 0 0 0 1.8 1.7h5.6a1.8 1.8 0 0 0 1.8-1.7l.9-12.1" />
      <path d="M10.3 10.3v6.5M13.7 10.3v6.5" />
    </Svg>
  )
}

/** 相机：发图（手机输入区那一个按钮 —— 点一下弹"拍摄 / 相册"）。 */
export function IconCamera({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M3.4 9.2A2.1 2.1 0 0 1 5.5 7.1h1.7l1.2-2.2h7.2l1.2 2.2h1.7a2.1 2.1 0 0 1 2.1 2.1v7.6a2.1 2.1 0 0 1-2.1 2.1H5.5a2.1 2.1 0 0 1-2.1-2.1z" />
      <circle cx="12" cy="12.8" r="3.3" />
    </Svg>
  )
}

/** 刷新：两个首尾相接的箭头（手机列表 / 设置页右上角）。 */
export function IconRefresh({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M3.9 12a8.1 8.1 0 0 1 13.8-5.8l2.4 2.3" />
      <path d="M20.1 12a8.1 8.1 0 0 1-13.8 5.8l-2.4-2.3" />
      <path d="M20.4 3.7v4.6h-4.6" />
      <path d="M3.6 20.3v-4.6h4.6" />
    </Svg>
  )
}

// ── v5.1（D287）：全屏游戏模式用的矢量图标 ───────────────────────────

/** 对局：手柄。 */
export function IconGamepad({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M7.1 7.2h9.8a4.1 4.1 0 0 1 4 3.3l.8 5.1a2.6 2.6 0 0 1-4.6 2.1l-1.4-1.7H8.3l-1.4 1.7a2.6 2.6 0 0 1-4.6-2.1l.8-5.1a4.1 4.1 0 0 1 4-3.3z" />
      <path d="M8 10.2v3.2M6.4 11.8h3.2" />
      <circle cx="16.1" cy="11.5" r=".7" fill="currentColor" stroke="none" />
      <circle cx="17.8" cy="13.2" r=".7" fill="currentColor" stroke="none" />
    </Svg>
  )
}

/** 作弊 / 难度：盾牌。 */
export function IconShield({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M12 3.2 19 6v5.4c0 4.2-2.8 7.4-7 9.4-4.2-2-7-5.2-7-9.4V6z" />
      <path d="m8.8 11.8 2.1 2.1 4.4-4.4" />
    </Svg>
  )
}

/** 技能：闪光。 */
export function IconSpark({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="m12 2.8 1.8 5.4 5.4 1.8-5.4 1.8L12 17.2l-1.8-5.4L4.8 10l5.4-1.8z" />
      <path d="m18.6 15.4.8 2.3 2.3.8-2.3.8-.8 2.3-.8-2.3-2.3-.8 2.3-.8z" />
    </Svg>
  )
}

/** 蓝图：折叠图纸。 */
export function IconBlueprint({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M4 5.4 9 3.8l6 1.8 5-1.6v14.2l-5 1.6-6-1.8-5 1.6z" />
      <path d="M9 3.8v14.2M15 5.6v14.2M4 9.8l5-1.6M15 8.4l5-1.6" />
    </Svg>
  )
}

/** 数据：数据库。 */
export function IconDatabase({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <ellipse cx="12" cy="5.2" rx="7.8" ry="2.8" />
      <path d="M4.2 5.2v6.2c0 1.5 3.5 2.8 7.8 2.8s7.8-1.3 7.8-2.8V5.2" />
      <path d="M4.2 11.4v6.2c0 1.5 3.5 2.8 7.8 2.8s7.8-1.3 7.8-2.8v-6.2" />
    </Svg>
  )
}

/** 游戏记忆：书。 */
export function IconBook({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M4.2 5.2A2.2 2.2 0 0 1 6.4 3h13.4v16.5H6.6a2.4 2.4 0 0 0-2.4 2.4z" />
      <path d="M4.2 5.2v16.7M7 7.2h9.2M7 10.6h7.4" />
    </Svg>
  )
}

/** 退出游戏模式。 */
export function IconLogout({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M10.2 4.2H5.6a1.8 1.8 0 0 0-1.8 1.8v12a1.8 1.8 0 0 0 1.8 1.8h4.6" />
      <path d="M14.6 8 18.8 12l-4.2 4M9 12h9.5" />
    </Svg>
  )
}

/** 发送。 */
export function IconSend({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="m3.4 4.2 17.2 7.8-17.2 7.8 2.1-7.8z" />
      <path d="M5.5 12h15.1" />
    </Svg>
  )
}

/** 开始 / 进游戏。 */
export function IconPlay({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <path d="M8 4.7 20 12 8 19.3z" />
    </Svg>
  )
}

/** 停止当前动作。 */
export function IconStop({ size, className }: { size?: number; className?: string }) {
  return (
    <Svg size={size} className={className}>
      <rect x="5.2" y="5.2" width="13.6" height="13.6" rx="2.2" />
    </Svg>
  )
}
