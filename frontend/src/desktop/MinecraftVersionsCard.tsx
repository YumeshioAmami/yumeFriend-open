import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  api,
  type MinecraftVersionDownloadState,
  type MinecraftVersionInfo,
  type MinecraftVersionStatus,
} from '../shared/api'

function formatMB(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return '—'
  return `${(bytes / 1024 / 1024).toFixed(bytes >= 10 * 1024 * 1024 ? 1 : 2)} MB`
}

function isDownloading(state: MinecraftVersionDownloadState | null | undefined): boolean {
  return Boolean(
    state &&
      ['downloading', 'verifying', 'extracting'].includes(String(state.state || '')),
  )
}

function tierLabel(item: MinecraftVersionInfo): string {
  if (item.tier === 'stable') return '稳定'
  if (item.tier === 'compatible') return '兼容'
  return '本机'
}

export default function MinecraftVersionsCard({
  host,
  port,
  value,
  onChange,
}: {
  host: string
  port: string
  value: string
  onChange: (value: string) => void
}) {
  const [data, setData] = useState<MinecraftVersionStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [probe, setProbe] = useState<{
    server_version: string
    protocol: number | null
    latency_ms: number
  } | null>(null)

  const refresh = useCallback(async (force = false) => {
    try {
      setError('')
      setData(await api.getMinecraftVersions(force))
    } catch (exc) {
      setError(`读取 Minecraft 版本清单失败：${String(exc)}`)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const download = data?.download ?? null
  const downloading = isDownloading(download)
  const selected = useMemo(
    () => data?.versions.find((item) => item.version === value) ?? null,
    [data, value],
  )

  useEffect(() => {
    if (!downloading) return
    const timer = window.setInterval(() => {
      void api
        .minecraftVersionDownloadStatus(value === 'auto' ? undefined : value)
        .then((next) => {
          setData((current) => (current ? { ...current, download: next } : current))
          if (next.state === 'done' || next.state === 'error') {
            void refresh()
          }
        })
        .catch(() => {
          // 一次轮询失败不把下载面板清空
        })
    }, 500)
    return () => window.clearInterval(timer)
  }, [downloading, refresh, value])

  const startDownload = useCallback(async () => {
    if (!selected || busy || downloading) return
    setBusy(true)
    setError('')
    try {
      const next = await api.downloadMinecraftVersion(selected.version)
      setData((current) => (current ? { ...current, download: next } : current))
    } catch (exc) {
      setError(`下载失败：${String(exc)}`)
    } finally {
      setBusy(false)
    }
  }, [busy, downloading, selected])

  const removePack = useCallback(async () => {
    if (!selected || !selected.installed || busy || downloading) return
    setBusy(true)
    setError('')
    try {
      await api.deleteMinecraftVersion(selected.version)
      await refresh()
    } catch (exc) {
      setError(`删除失败：${String(exc)}`)
    } finally {
      setBusy(false)
    }
  }, [busy, downloading, refresh, selected])

  const detect = useCallback(async () => {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const result = await api.probeMinecraft(host.trim(), Number(port))
      setProbe(result)
    } catch (exc) {
      setProbe(null)
      setError(`探测失败：${String(exc)}`)
    } finally {
      setBusy(false)
    }
  }, [busy, host, port])

  const warning =
    probe && selected && probe.protocol != null && selected.protocol != null
      ? probe.protocol === selected.protocol
        ? `服务器 ${probe.server_version}（协议 ${probe.protocol}）与这个数据包同协议。`
        : `服务器 ${probe.server_version}（协议 ${probe.protocol}），这个数据包是 ${selected.version}（协议 ${selected.protocol}）。启动时会按实验性跨版本处理。`
      : null

  return (
    <div className="rounded-xl border border-line/60 bg-surface/35 p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-1.5">
        <div>
          <h3 className="text-[11.5px] font-medium text-ink">Minecraft 版本数据</h3>
          <p className="mt-0.5 text-[9.5px] leading-relaxed text-faint">
            优先精确版本；没有就复用同协议号；再没有就用最高版本做实验性兼容。
          </p>
        </div>
        <button
          type="button"
          onClick={() => void refresh(true)}
          className="rounded-full border border-line-strong px-2 py-0.5 text-[9.5px] text-muted hover:border-accent/60 hover:text-accent"
        >
          刷新清单
        </button>
      </div>

      <div className="mt-2 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="min-w-0 rounded-xl border border-line bg-page/40 px-2.5 py-2 text-[11.5px] text-ink outline-none focus:border-accent/60"
        >
          <option value="auto">
            自动检测服务器版本
            {data?.latest_version ? `（最高资源包 ${data.latest_version}）` : ''}
          </option>
          {(data?.versions ?? []).map((item) => (
            <option key={item.version} value={item.version}>
              {item.version} · {tierLabel(item)}
              {item.installed ? ' · 已安装' : ` · ${formatMB(item.size)}`}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => void detect()}
          disabled={busy || !host.trim()}
          className="rounded-xl border border-line-strong px-3 py-2 text-[11px] text-muted hover:border-accent/60 hover:text-accent disabled:opacity-40"
        >
          {busy ? '检查中…' : '检测服务器版本'}
        </button>
      </div>

      {probe && (
        <p className="mt-1.5 rounded-lg bg-accent/8 px-2 py-1 text-[10px] text-accent">
          服务器：{probe.server_version || '未知'} · 协议 {probe.protocol ?? '?'} ·{' '}
          {probe.latency_ms}ms
        </p>
      )}
      {warning && (
        <p className="mt-1.5 rounded-lg bg-amber-400/10 px-2 py-1 text-[9.5px] leading-relaxed text-amber-100/85">
          {warning}
        </p>
      )}

      {selected && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {!selected.installed && selected.url && (
            <button
              type="button"
              onClick={() => void startDownload()}
              disabled={busy || downloading}
              className="rounded-full bg-accent px-2.5 py-1 text-[10px] font-medium text-white disabled:opacity-40"
            >
              {downloading && download?.version === selected.version
                ? `下载中 ${Math.round(download.percent)}%`
                : `下载 ${selected.version}（${formatMB(selected.size)}）`}
            </button>
          )}
          {selected.installed && (
            <>
              <span className="rounded-full bg-emerald-400/12 px-2 py-1 text-[9.5px] text-emerald-300">
                已安装 · {formatMB(selected.installed_size)}
              </span>
              <button
                type="button"
                onClick={() => void removePack()}
                disabled={busy || downloading}
                className="rounded-full border border-red-300/30 px-2 py-1 text-[9.5px] text-red-200/85 hover:bg-red-400/10 disabled:opacity-40"
              >
                删除本地数据
              </button>
            </>
          )}
          {!selected.url && !selected.installed && (
            <span className="text-[9.5px] text-faint">这个版本没有可下载资源。</span>
          )}
        </div>
      )}

      {downloading && download && (
        <div className="mt-2">
          <div className="h-1.5 overflow-hidden rounded-full bg-white/10">
            <div
              className="h-full rounded-full bg-accent transition-[width] duration-200"
              style={{ width: `${Math.max(2, Math.min(100, download.percent || 0))}%` }}
            />
          </div>
          <p className="mt-1 flex justify-between text-[9px] text-faint">
            <span>
              {download.version} · {download.state}
            </span>
            <span>{Math.round(download.percent || 0)}%</span>
          </p>
        </div>
      )}
      {download?.state === 'error' && (
        <p className="mt-1.5 text-[9.5px] leading-relaxed text-red-300/85">
          下载失败：{download.error || '未知原因'}
        </p>
      )}
      {error && (
        <p className="mt-1.5 rounded-lg bg-red-400/8 px-2 py-1 text-[9.5px] leading-relaxed text-red-200/85">
          {error}
        </p>
      )}
      {data?.manifest_error && (
        <p className="mt-1.5 text-[9px] leading-relaxed text-faint">
          远程清单暂时不可用；已安装版本仍可直接使用。
        </p>
      )}
    </div>
  )
}
