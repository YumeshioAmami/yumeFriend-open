/** 全局 Toast（D31 第 3 条）：出错了飘一条，带【查看日志】。
 *
 * 为什么需要：聊天里的红色气泡只说"这一轮失败了"，
 * 但"为什么失败"在日志里 —— 所以要有一条能直接跳过去的入口。
 *
 * 2026-09-19：加 `kind: 'info'`。有些反馈不是错误（"界面缩放 130%"
 * "系统通知已开启"），但**必须说出来**——不然又是"点一下就没反应"。
 * info 一律**不带**【查看日志】（没出错，没什么可查的）。
 */

import { useEffect } from 'react'

export interface Toast {
  id: number
  text: string
  kind?: 'error' | 'info'
}

export default function Toasts({
  toasts,
  onDismiss,
  onOpenLogs,
}: {
  toasts: Toast[]
  onDismiss: (id: number) => void
  onOpenLogs: () => void
}) {
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[360px] flex-col gap-2">
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onDismiss={onDismiss} onOpenLogs={onOpenLogs} />
      ))}
    </div>
  )
}

function ToastItem({
  toast,
  onDismiss,
  onOpenLogs,
}: {
  toast: Toast
  onDismiss: (id: number) => void
  onOpenLogs: () => void
}) {
  useEffect(() => {
    const timer = window.setTimeout(() => onDismiss(toast.id), 12000)
    return () => window.clearTimeout(timer)
  }, [toast.id, onDismiss])

  return (
    <div
      className={`pointer-events-auto animate-fade-up rounded-xl border px-3 py-2 shadow-lg backdrop-blur ${
        toast.kind === 'info'
          ? 'border-line bg-surface/95'
          : 'border-red-200 bg-red-50/95'
      }`}
    >
      <p
        className={`whitespace-pre-line text-[12px] leading-relaxed ${
          toast.kind === 'info' ? 'text-ink-soft' : 'text-red-800'
        }`}
      >
        {toast.text}
      </p>
      <div className="mt-1 flex justify-end gap-2">
        {toast.kind !== 'info' && (
          <button
            type="button"
            onClick={() => {
              onOpenLogs()
              onDismiss(toast.id)
            }}
            className="rounded-full border border-red-300 px-2 py-0.5 text-[11px] text-red-700 hover:bg-red-100"
          >
            查看日志
          </button>
        )}
        <button
          type="button"
          onClick={() => onDismiss(toast.id)}
          className={`rounded-full px-2 py-0.5 text-[11px] ${
            toast.kind === 'info' ? 'text-muted hover:text-ink' : 'text-red-500 hover:text-red-700'
          }`}
        >
          知道了
        </button>
      </div>
    </div>
  )
}
