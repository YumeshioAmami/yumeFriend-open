/**
 * 传图前先在**本机**压一下（D215，用户 2026-09-27："手机端上传图片经常上传不上去"）。
 *
 * 为什么要压（三条都实测过）：
 *   · 后端只收 **≤ 10 MB**（`api.md` 【v2】1）—— 手机随手一张就是 6~20 MB，超了直接 400；
 *   · 手机壳走的是组网软件那条隧道：传一张 7 MB 的图要 **2.1~2.4s**（本机直连也一样），
 *     隧道一抖就断，看着就是"传不上去"；压到几百 KB 基本秒到；
 *   · 后端把长边缩到 2048、我们这里缩到 1600 —— 反正那边也要缩，不如在**上传前**缩掉，
 *     省的都是手机的上行带宽。
 *
 * ⚠️ 压不动（浏览器解不开这张图 / 没有 canvas）就**原样返回** —— 压缩只是"更快"，
 * 绝不能因为它反而传不了；真不行后端那边还有一条明确的人话提示兜底。
 */

export interface ShrinkOptions {
  /** 长边缩到这个像素以内（默认 1600；后端是 2048，我们留点余量） */
  maxSide?: number
  /** 有损压缩质量（默认 0.85） */
  quality?: number
  /** 比这个小就不折腾了（默认 1.2 MB） */
  skipBelowBytes?: number
}

/** 等比缩到长边不超过 `maxSide`（纯函数，方便单测/一眼看懂）。 */
export function fitWithin(
  width: number,
  height: number,
  maxSide: number,
): { width: number; height: number } {
  const longest = Math.max(width, height)
  if (longest <= 0 || longest <= maxSide) return { width, height }
  const ratio = maxSide / longest
  return {
    width: Math.max(1, Math.round(width * ratio)),
    height: Math.max(1, Math.round(height * ratio)),
  }
}

function toBlob(canvas: HTMLCanvasElement, type: string, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality))
}

function renameFor(name: string, type: string): string {
  const ext = type === 'image/webp' ? 'webp' : 'jpg'
  const stem = name.replace(/\.[^./\\]+$/, '') || 'photo'
  return `${stem}.${ext}`
}

/**
 * 需要的话把图压小；返回一个新 `File`（不需要压 / 压不了 → 原样返回，**同一个对象**）。
 *
 * 编码优先 WebP（体积小、还保透明），拿不到就 JPEG；比原图还大就退回原图。
 */
export async function shrinkImage(file: File, options: ShrinkOptions = {}): Promise<File> {
  const maxSide = options.maxSide ?? 1600
  const quality = options.quality ?? 0.85
  const skipBelow = options.skipBelowBytes ?? 1_200_000
  if (file.size <= skipBelow) return file
  if (typeof createImageBitmap !== 'function') return file

  try {
    const bitmap = await createImageBitmap(file)
    try {
      const target = fitWithin(bitmap.width, bitmap.height, maxSide)
      const canvas = document.createElement('canvas')
      canvas.width = target.width
      canvas.height = target.height
      const ctx = canvas.getContext('2d')
      if (!ctx) return file
      ctx.imageSmoothingQuality = 'high'
      ctx.drawImage(bitmap, 0, 0, target.width, target.height)
      const blob =
        (await toBlob(canvas, 'image/webp', quality)) ??
        (await toBlob(canvas, 'image/jpeg', quality))
      if (!blob || blob.size >= file.size) return file
      return new File([blob], renameFor(file.name, blob.type), {
        type: blob.type,
        lastModified: Date.now(),
      })
    } finally {
      bitmap.close()
    }
  } catch {
    // 解不开（比如浏览器不认这种 HEIC）→ 原样传，让后端给一句人话
    return file
  }
}
