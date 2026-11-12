/**
 * 给图片 URL 挂一个 `?w=`（v3.1）：**手机端拉电脑的图太慢**的解药。
 *
 * 后端 `/api/images/{id}`、`/api/friends/{id}/portraits/{emotion}`、`/avatar`、`/background`
 * 都支持 `w=`（按需生成压缩版 + 磁盘缓存 + 让客户端缓存住）。这里只负责把参数拼对
 * —— 原来的 URL 可能已经带 `?v=`（破缓存用），所以得分清用 `?` 还是 `&`。
 */

export function withWidth(url: string | null | undefined, width: number): string {
  if (!url) return ''
  if (!width || width <= 0) return url
  // 只给站内图片加（外链、data: 一律不动）
  if (!url.startsWith('/api/')) return url
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}w=${width}`
}
