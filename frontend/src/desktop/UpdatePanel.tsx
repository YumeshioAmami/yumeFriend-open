/** 桌面自动更新（D456）：启动提示 + 设置更新页。 */

import { useCallback, useEffect, useState } from 'react'

import { api, type UpdateHistoryItem, type UpdateStatus } from '../shared/api'

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function useUpdateController() {
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [history, setHistory] = useState<UpdateHistoryItem[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const refresh = useCallback(async (force = false): Promise<UpdateStatus | null> => {
    try {
      const [next, historyResult] = await Promise.all([
        api.updateStatus(force),
        api.updateHistory(),
      ])
      setStatus(next)
      setHistory(historyResult.items)
      return next
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e))
      return null
    }
  }, [])

  useEffect(() => {
    void refresh(true)
  }, [refresh])

  const state = status?.download.state
  useEffect(() => {
    if (!state || !['downloading', 'extracting', 'applying'].includes(state)) return
    const timer = window.setInterval(() => void refresh(false), 1000)
    return () => window.clearInterval(timer)
  }, [state, refresh])

  async function updateNow() {
    if (busy) return
    setBusy(true)
    setMessage(null)
    try {
      let current = status?.download.state === 'ready' ? status : await refresh(true)
      if (!current) throw new Error('检查更新失败')
      if (!current.update_available) throw new Error('当前已经是最新版本')
      if (!current.can_auto_update) {
        throw new Error('当前版本不支持自动更新，请使用资源页下载覆盖。')
      }
      if (current.download.state !== 'ready') {
        await api.updateDownload()
        for (let i = 0; i < 600; i += 1) {
          await sleep(1000)
          const next = await api.updateStatus()
          setStatus(next)
          if (next.download.state === 'ready') {
            current = next
            break
          }
          if (next.download.state === 'error') {
            throw new Error(next.download.error ?? '下载更新包失败')
          }
        }
        if (current.download.state !== 'ready') {
          throw new Error('下载更新包超时')
        }
      }
      setMessage('准备重启更新…')
      const result = await api.updateApply()
      if (!result.ok) throw new Error(result.error ?? '启动更新器失败')
      setMessage('正在重启，更新完成后会自动打开…')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return { status, history, busy, message, refresh, updateNow }
}

function UpdateButton({ onClick, busy }: { onClick: () => void; busy: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className="rounded-xl bg-accent px-4 py-2 text-[12px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
    >
      {busy ? '正在更新…' : '立即更新'}
    </button>
  )
}

function ResourceLink({ url }: { url: string }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="rounded-xl border border-line-strong px-4 py-2 text-[12px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
    >
      跳转到资源页
    </a>
  )
}

/** 启动时发现新版本弹出的应用内更新页。 */
export function UpdatePrompt() {
  const { status, busy, message, updateNow } = useUpdateController()
  const [dismissed, setDismissed] = useState(false)
  if (dismissed || !status?.update_available || !status.supported) return null
  const downloading = ['downloading', 'extracting', 'applying'].includes(
    status.download.state,
  )
  const progress = Math.max(0, Math.min(100, status.download.progress || 0))
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim/50 p-4 backdrop-blur-sm">
      <div className="w-[520px] max-w-full overflow-hidden rounded-2xl border border-line bg-page shadow-2xl">
        <div className="border-b border-line/60 px-5 py-4">
          <div className="text-[11px] font-medium tracking-[0.14em] text-accent">
            YUMEFRIEND UPDATE
          </div>
          <h2 className="mt-1 text-[18px] font-medium text-ink">
            发现新版本 {status.latest_version}
          </h2>
          <p className="mt-1 text-[11.5px] text-faint">
            当前版本 {status.current_version} · 更新不会动你的 user_data
          </p>
        </div>
        <div className="max-h-[45vh] overflow-y-auto px-5 py-4">
          <pre className="whitespace-pre-wrap font-sans text-[12px] leading-relaxed text-muted">
            {status.notes || '这次更新没有附说明。'}
          </pre>
        </div>
        <div className="border-t border-line/60 px-5 py-4">
          {downloading && (
            <div className="mb-3">
              <div className="flex items-center justify-between text-[11px] text-muted">
                <span>
                  {status.download.state === 'downloading'
                    ? '正在下载更新包…'
                    : status.download.state === 'extracting'
                      ? '正在解压…'
                      : '正在重启更新…'}
                </span>
                <span>
                  {status.download.state === 'downloading'
                    ? `${progress}%`
                    : ''}
                </span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-chip">
                <div
                  className="h-full rounded-full bg-accent transition-all duration-300"
                  style={{ width: `${Math.max(2, progress)}%` }}
                />
              </div>
            </div>
          )}
          {message && <p className="mb-2 text-[11.5px] text-muted">{message}</p>}
          <div className="flex flex-wrap items-center gap-2">
            <UpdateButton onClick={() => void updateNow()} busy={busy} />
            <button
              type="button"
              onClick={() => setDismissed(true)}
              disabled={busy}
              className="rounded-xl border border-line-strong px-4 py-2 text-[12px] text-muted transition-colors hover:border-accent/50 hover:text-accent disabled:opacity-40"
            >
              暂不更新
            </button>
            {!status.can_auto_update && (
              <span className="ml-auto">
                <ResourceLink url={status.resource_url} />
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/** 设置「更新」页：始终保留立即更新和资源页。 */
export function UpdatePanel() {
  const { status, history, busy, message, updateNow } = useUpdateController()
  if (!status) {
    return (
      <section className="space-y-3">
        <h3 className="text-[12px] font-medium text-ink">更新</h3>
        <p className="text-[11px] text-faint">正在检查更新…</p>
      </section>
    )
  }
  const downloading = ['downloading', 'extracting', 'applying'].includes(
    status.download.state,
  )
  const progress = Math.max(0, Math.min(100, status.download.progress || 0))
  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-[12px] font-medium text-ink">更新</h3>
        <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
          启动时会自动检查；这里也可以随时手动更新或打开资源页。
        </p>
      </div>
      <div className="rounded-2xl border border-line/70 bg-surface/70 p-4">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11.5px] text-muted">
          <span>当前版本 {status.current_version}</span>
          <span>最新版本 {status.latest_version}</span>
          <span className={status.update_available ? 'text-accent' : 'text-faint'}>
            {status.update_available ? '发现新版本' : '已是最新版本'}
          </span>
        </div>
        {!status.supported && (
          <p className="mt-2 text-[11px] text-faint">
            当前是源码版或未打包版本，自动替换只在打包版生效；可以跳转资源页下载。
          </p>
        )}
        {!status.can_auto_update && status.supported && (
          <p className="mt-2 text-[11px] text-amber-600">
            安装目录不可写（可能在 Program Files 或权限不足），请使用资源页下载。
          </p>
        )}
        {status.error && <p className="mt-2 text-[11px] text-red-500">{status.error}</p>}
        {status.notes && (
          <pre className="mt-3 max-h-[26vh] overflow-y-auto whitespace-pre-wrap rounded-xl border border-line/60 bg-page/40 p-2.5 font-sans text-[11px] leading-relaxed text-muted">
            {status.notes}
          </pre>
        )}
        {history.length > 0 && (
          <div className="mt-3">
            <p className="text-[11px] font-medium text-ink">更新日志</p>
            <div className="mt-1.5 max-h-[22vh] space-y-1 overflow-y-auto rounded-xl border border-line/60 bg-page/40 p-2.5">
              {history.map((item, index) => (
                <div key={`${item.at}-${index}`} className="text-[10.5px] leading-relaxed text-muted">
                  <span className="text-faint">{item.at.replace('T', ' ').slice(0, 19)}</span>
                  <span className="mx-1.5 text-faint">·</span>
                  <span>
                    {item.event === 'downloaded'
                      ? '已下载'
                      : item.event === 'applying'
                        ? '开始安装'
                        : item.event === 'applied'
                          ? '更新完成'
                          : item.event === 'failed'
                            ? '更新失败'
                            : item.event}{' '}
                    {item.version}
                  </span>
                  {item.message && <span className="text-red-400"> · {item.message}</span>}
                </div>
              ))}
            </div>
          </div>
        )}
        {downloading && (
          <div className="mt-3">
            <div className="flex items-center justify-between text-[11px] text-muted">
              <span>
                {status.download.state === 'downloading'
                  ? '正在下载更新包…'
                  : status.download.state === 'extracting'
                    ? '正在解压…'
                    : '正在重启更新…'}
              </span>
              <span>
                {status.download.state === 'downloading' ? `${progress}%` : ''}
              </span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-chip">
              <div
                className="h-full rounded-full bg-accent transition-all duration-300"
                style={{ width: `${Math.max(2, progress)}%` }}
              />
            </div>
          </div>
        )}
        {message && <p className="mt-2 text-[11px] text-muted">{message}</p>}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <UpdateButton
            onClick={() => void updateNow()}
            busy={busy || !status.update_available || !status.can_auto_update}
          />
          <ResourceLink url={status.resource_url} />
        </div>
      </div>
    </section>
  )
}
