/**
 * 工具审批卡片（v2.1 / T2.1-4 / D99）。
 *
 * 她要做**改动磁盘**的事（写/挪/删）时弹这张卡：说清"她要做什么"，
 * 用户点「允许」才真执行；不点（或超过 3 分钟）= 拒绝。
 *
 * 三条别破坏的：
 * · **不挡着聊天**：固定右下角，用户可以一边看聊天一边决定
 * · **说人话**：标题写清动作 + 路径；写文件时带内容预览
 * · 点了就立刻打接口，**不乐观地假设成功**（失败就把卡片留着）
 */

import { useState } from 'react'

import { api } from '../shared/api'
import type { ApprovalEvent } from '../shared/useWs'
import { ACCENT } from '../shared/ui'

export default function ApprovalCard({
  request,
  onDone,
}: {
  request: ApprovalEvent
  onDone: (id: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function decide(allow: boolean) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await api.resolveApproval(request.id, allow)
      onDone(request.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="pointer-events-auto w-[360px] max-w-[92vw] rounded-2xl border border-amber-300/70 bg-page/95 p-3 shadow-2xl backdrop-blur">
      <p className="text-[12px] font-medium text-ink">
        {request.friendName} 想在你的电脑上做一件事
      </p>
      <p className="mt-1 break-words text-[11.5px] leading-relaxed text-muted">{request.summary}</p>
      {request.detail && (
        <pre className="mt-1.5 max-h-28 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-surface/70 p-2 text-[10.5px] leading-relaxed text-faint">
          {request.detail}
        </pre>
      )}
      <p className="mt-1.5 text-[10.5px] text-faint">
        不点的话，{Math.round(request.expiresIn / 60)} 分钟后自动当她被拒绝了。
      </p>
      {error && <p className="mt-1 text-[11px] text-red-600">{error}</p>}
      <div className="mt-2 flex justify-end gap-2">
        <button
          type="button"
          onClick={() => void decide(false)}
          disabled={busy}
          className="rounded-xl border border-line-strong px-3 py-1 text-[12px] text-muted transition-colors hover:border-stone-400 disabled:opacity-40"
        >
          拒绝
        </button>
        <button
          type="button"
          onClick={() => void decide(true)}
          disabled={busy}
          className="rounded-xl px-3 py-1 text-[12px] font-medium text-white disabled:opacity-40"
          style={{ background: ACCENT }}
        >
          {busy ? '处理中…' : '允许'}
        </button>
      </div>
    </div>
  )
}
