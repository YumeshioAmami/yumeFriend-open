/**
 * 「保存图片」这件事在两个壳里是两套机制，这里统一：
 *
 * · **电脑（桌面外壳）**：WebView2 自己**不会**下载 → 走 `window.pywebview.api.save_image()`：
 *   Python 那边弹**原生「另存为」对话框**（D163），用户自己选路径，我们把**真实路径**贴回界面。
 * · **手机 App**：WebView **不认 `<a download>`**（点了不下载、也不触发 DownloadListener，
 *   用户实测"根本存不了"）→ 走壳的原生桥 `yfShell.saveImage(url)`：App 自己抓图写进**系统相册**（D163）。
 * · **普通浏览器**（含手机浏览器）：`<a download>` —— 浏览器自己的下载行为。
 *
 * 返回一句**给人看的**结果，调用方贴在界面上（用户要的"保存成功要有提示"）。
 */

export async function saveImage(url: string): Promise<string> {
  const absolute = new URL(url, window.location.origin).href

  // 1) 桌面外壳：原生"另存为"（D163）
  const bridge = (window as unknown as { pywebview?: { api?: Record<string, unknown> } }).pywebview
  const save = bridge?.api?.save_image as ((href: string) => Promise<{ ok: boolean; path?: string; error?: string }>) | undefined
  if (typeof save === 'function') {
    try {
      const result = await save(absolute)
      if (result?.ok) return `已保存到：${result.path}`
      return `没存上：${result?.error ?? '原因不明'}`
    } catch (exc) {
      return `没存上：${exc instanceof Error ? exc.message : String(exc)}`
    }
  }

  // 2) 手机 App：壳的原生桥把图写进系统相册（D163）
  const shell = (window as unknown as { yfShell?: { saveImage?: (href: string) => unknown } }).yfShell
  if (typeof shell?.saveImage === 'function') {
    try {
      const message = await Promise.resolve(shell.saveImage(absolute))
      const text = String(message ?? '').trim()
      if (text) return text
    } catch (exc) {
      return `没存上：${exc instanceof Error ? exc.message : String(exc)}`
    }
  }

  // 3) 普通浏览器
  const link = document.createElement('a')
  link.href = url
  link.download = ''
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()
  return '已开始保存（看浏览器的下载栏）'
}

export default function SaveImageButton({
  url,
  onDone,
  className = '',
}: {
  url: string
  onDone: (message: string) => void
  className?: string
}) {
  return (
    <button
      type="button"
      onClick={() => {
        onDone('正在保存…')
        void saveImage(url).then(onDone)
      }}
      className={className}
    >
      保存图片
    </button>
  )
}
