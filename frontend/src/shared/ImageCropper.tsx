/**
 * 头像裁剪：**让用户自己选区域**（拖动 + 缩放），而不是系统拍脑袋居中裁。
 *
 * 为什么自己写：只依赖 canvas，不引第三方库（这个项目一直在克制依赖）。
 * 输出固定 256×256 PNG——和后端那套"统一压到 256"对齐
 * （后端仍然会兜底裁一次，所以从这里传出去的就算是原图也不会出问题）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

const VIEW = 288 // 裁剪框边长（px）
const OUT = 256 // 输出边长
const MAX_ZOOM = 4
const MIN_ZOOM = 1

export default function ImageCropper({
  file,
  onCancel,
  onConfirm,
}: {
  file: File
  onCancel: () => void
  onConfirm: (cropped: File) => void
}) {
  // 直接用 file 造一个 object URL；effect 只负责回收（别在 effect 里 setState）
  const [url] = useState(() => URL.createObjectURL(file))
  const imgRef = useRef<HTMLImageElement | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null)
  const [zoom, setZoom] = useState(MIN_ZOOM) // 1 = 刚好铺满裁剪框
  const [offset, setOffset] = useState({ x: 0, y: 0 }) // 图片左上角相对裁剪框
  const [busy, setBusy] = useState(false)
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null)
  // 原生 wheel 监听器里要读最新值（React 的 onWheel 是 passive 的，拦不住滚动）
  const zoomRef = useRef(zoom)
  const offsetRef = useRef(offset)
  useEffect(() => {
    zoomRef.current = zoom
    offsetRef.current = offset
  })

  useEffect(() => () => URL.revokeObjectURL(url), [url])

  const drawSize = useCallback(
    (z: number, n = natural) => {
      if (!n) return { w: VIEW, h: VIEW }
      const base = VIEW / Math.min(n.w, n.h)
      return { w: n.w * base * z, h: n.h * base * z }
    },
    [natural],
  )

  const clampPoint = useCallback(
    (next: { x: number; y: number }, z: number, n = natural) => {
      const { w, h } = drawSize(z, n)
      return {
        x: Math.min(0, Math.max(VIEW - w, next.x)),
        y: Math.min(0, Math.max(VIEW - h, next.y)),
      }
    },
    [drawSize, natural],
  )

  /** 以裁剪框里的 (px,py) 这个点为锚缩放——滚轮就是"跟着鼠标缩"。 */
  const zoomAt = useCallback(
    (px: number, py: number, next: number) => {
      if (!natural) return
      const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next))
      const before = drawSize(zoomRef.current)
      const after = drawSize(z)
      const fx = (px - offsetRef.current.x) / before.w // 锚点在图片上的相对位置
      const fy = (py - offsetRef.current.y) / before.h
      setZoom(z)
      setOffset(clampPoint({ x: px - fx * after.w, y: py - fy * after.h }, z))
    },
    [clampPoint, drawSize, natural],
  )

  // 图片加载完：算出"铺满"的比例，并把位置摆到居中
  const onLoaded = () => {
    const el = imgRef.current
    if (!el) return
    const n = { w: el.naturalWidth, h: el.naturalHeight }
    setNatural(n)
    const { w, h } = drawSize(MIN_ZOOM, n)
    setZoom(MIN_ZOOM)
    setOffset({ x: (VIEW - w) / 2, y: (VIEW - h) / 2 })
  }

  // 滚轮缩放（原生监听 + passive:false，才拦得住页面滚动）
  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const rect = el.getBoundingClientRect()
      zoomAt(
        event.clientX - rect.left,
        event.clientY - rect.top,
        zoomRef.current - event.deltaY * 0.002,
      )
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [zoomAt])

  function onPointerDown(e: React.PointerEvent) {
    try {
      ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
    } catch {
      // 没有活动指针时会抛（合成事件就是这样）——不影响后面的拖动逻辑
    }
    drag.current = { x: e.clientX, y: e.clientY, ox: offset.x, oy: offset.y }
  }

  function onPointerMove(e: React.PointerEvent) {
    if (!drag.current) return
    const d = drag.current
    setOffset(clampPoint({ x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) }, zoomRef.current))
  }

  async function confirm() {
    const el = imgRef.current
    if (!el || !natural || busy) return
    setBusy(true)
    try {
      const { w: drawW } = drawSize(zoom)
      const perView = natural.w / drawW // 裁剪框 1px 对应原图多少 px
      const sx = -offset.x * perView
      const sy = -offset.y * perView
      const side = VIEW * perView
      const canvas = document.createElement('canvas')
      canvas.width = OUT
      canvas.height = OUT
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.imageSmoothingQuality = 'high'
      ctx.drawImage(el, sx, sy, side, side, 0, 0, OUT, OUT)
      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, 'image/png', 0.95),
      )
      if (blob) onConfirm(new File([blob], 'avatar.png', { type: 'image/png' }))
    } finally {
      setBusy(false)
    }
  }

  const size = drawSize(zoom)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim/60 p-3 backdrop-blur-sm">
      <div className="flex max-h-[92%] w-[380px] max-w-[96%] flex-col overflow-hidden rounded-2xl border border-line bg-page shadow-2xl">
        <div className="flex shrink-0 items-center px-4 py-2.5">
          <h2 className="text-[13.5px] font-medium text-ink">裁剪头像</h2>
          <button
            type="button"
            onClick={onCancel}
            aria-label="取消"
            className="ml-auto rounded-lg px-2 py-0.5 text-faint hover:bg-chip hover:text-muted"
          >
            ✕
          </button>
        </div>

        <div className="flex flex-1 flex-col items-center gap-3 overflow-y-auto px-4 pb-4">
          <div
            ref={boxRef}
            className="relative shrink-0 cursor-move touch-none overflow-hidden rounded-full bg-chip/40 ring-1 ring-line"
            style={{ width: VIEW, height: VIEW }}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={() => (drag.current = null)}
            onPointerCancel={() => (drag.current = null)}
          >
            {url && (
              <img
                ref={imgRef}
                src={url}
                alt=""
                draggable={false}
                onLoad={onLoaded}
                // ⚠️ max-w-none 必须留着：Tailwind 的基础样式给 img 加了 max-width:100%，
                // 而 max-width 永远赢过 width —— 会把绝对定位的图卡成裁剪框那么宽，
                // 于是"缩放"完全失效（只滑动不放大），图还会被压变形。踩过一次。
                className="absolute max-w-none select-none"
                style={{ width: size.w, height: size.h, left: offset.x, top: offset.y }}
              />
            )}
            <div className="pointer-events-none absolute inset-0 rounded-full ring-1 ring-white/25" />
          </div>

          <label className="flex w-full items-center gap-2">
            <span className="shrink-0 text-[11px] text-faint">缩放</span>
            <input
              type="range"
              min={MIN_ZOOM}
              max={MAX_ZOOM}
              step={0.01}
              value={zoom}
              onChange={(e) => zoomAt(VIEW / 2, VIEW / 2, Number(e.target.value))}
              className="w-full accent-[var(--color-accent)]"
            />
          </label>
          <p className="text-[11px] text-faint">拖动图片选位置；滚轮（跟着鼠标缩）或滑块缩放。</p>
        </div>

        <div className="flex shrink-0 justify-end gap-2 border-t border-line/60 px-4 py-2.5">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-xl border border-line-strong px-3 py-1.5 text-[12.5px] text-muted transition-colors hover:border-stone-400"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void confirm()}
            disabled={!natural || busy}
            className="rounded-xl bg-accent px-4 py-1.5 text-[12.5px] font-medium text-white transition-opacity disabled:opacity-40"
          >
            {busy ? '处理中…' : '就用这块'}
          </button>
        </div>
      </div>
    </div>
  )
}
