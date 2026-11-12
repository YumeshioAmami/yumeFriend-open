/**
 * 朋友的头像（v2，T2.0-3）。
 *
 * **硬规则：没有头像也要好看**——回退成首字母色块，**绝不出现空框**。
 * 三处都用它：左栏列表 / 聊天头部 / 朋友编辑弹窗。
 */

import type { CSSProperties } from 'react'

import { withWidth } from './imgUrl'
import { getInitial } from './ui'

export default function Avatar({
  name,
  url,
  size = 36,
  className = '',
  style,
}: {
  name: string
  url?: string | null
  size?: number
  className?: string
  style?: CSSProperties
}) {
  if (url) {
    return (
      <img
        // v3.1：头像也走压缩版（手机列表里一张 90~140 KB → 十几 KB）
        src={withWidth(url, Math.min(512, Math.round(size * 2)))}
        alt={name}
        width={size}
        height={size}
        draggable={false}
        className={`shrink-0 select-none rounded-full object-cover ${className}`}
        style={{ width: size, height: size, ...style }}
      />
    )
  }
  return (
    <div
      aria-hidden
      className={`flex shrink-0 select-none items-center justify-center rounded-full bg-avatar font-medium text-white ${className}`}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.37), ...style }}
    >
      {getInitial(name)}
    </div>
  )
}
