/**
 * 三设备手动同步（D302）。
 *
 * 两台设备都打开这页 → 自动发现彼此 → 选设备 → 输入对方屏幕上的 6 位码。
 * 正常流程不需要填地址。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  api,
  type SyncDirection,
  type SyncPeer,
  type SyncRunResult,
  type SyncSessionInfo,
} from '../shared/api'
import ServerSyncCard from '../shared/ServerSyncCard'

interface Props {
  onBack: () => void
}

function shellText(fn: 'deviceName' | 'deviceKind'): string | null {
  const shell = (window as unknown as { yfShell?: Record<string, unknown> }).yfShell
  const value = shell?.[fn]
  if (typeof value === 'function') {
    try {
      const text = String((value as () => string)() || '').trim()
      return text || null
    } catch {
      return null
    }
  }
  return null
}

function total(values: Record<string, number> | undefined): number {
  return Object.values(values ?? {}).reduce((sum, value) => sum + value, 0)
}

const DIRECTIONS: { value: SyncDirection; label: string; hint: string }[] = [
  { value: 'both', label: '双向同步', hint: '先收对方的，再把自己的发过去' },
  { value: 'pull', label: '只拉取', hint: '对方 → 本机；不改对方' },
  { value: 'push', label: '只推送', hint: '本机 → 对方；不把对方数据拉回来' },
]

export default function SyncScreen({ onBack }: Props) {
  const [session, setSession] = useState<SyncSessionInfo>({})
  const [peers, setPeers] = useState<SyncPeer[]>([])
  const [selected, setSelected] = useState<SyncPeer | null>(null)
  const [code, setCode] = useState('')
  const [direction, setDirection] = useState<SyncDirection>('both')
  const [busy, setBusy] = useState(false)
  const [hint, setHint] = useState('正在启动同步…')
  const [result, setResult] = useState<SyncRunResult | null>(null)

  const deviceName = useMemo(() => shellText('deviceName'), [])
  const deviceKind = useMemo(() => shellText('deviceKind'), [])

  const start = useCallback(async () => {
    try {
      const info = await api.syncStart({
        ...(deviceName ? { name: deviceName } : {}),
        ...(deviceKind ? { kind: deviceKind } : {}),
      })
      setSession(info)
      setHint('让另一台设备打开同步页，选中这台，输入上面 6 位码。')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }, [deviceName, deviceKind])

  useEffect(() => {
    void start()
    return () => {
      void api.syncStop().catch(() => undefined)
    }
  }, [start])

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [st, found] = await Promise.all([api.syncStatus(), api.syncPeers()])
        if (!alive) return
        setSession(st.session)
        setPeers(found.peers)
        if (!selected && found.peers.length === 1) setSelected(found.peers[0])
      } catch {
        // 页面轮询失败不吵；真正同步时再报错。
      }
    }
    void tick()
    const timer = window.setInterval(() => void tick(), 2200)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [selected])

  const run = async () => {
    if (!selected) {
      setHint('先选一台设备')
      return
    }
    if (!/^\d{6}$/.test(code)) {
      setHint('输入对方屏幕上的 6 位数字')
      return
    }
    setBusy(true)
    setResult(null)
    setHint('正在同步…')
    try {
      const out = await api.syncRun(selected, code, direction)
      setResult(out)
      setHint('同步完成')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-dvh flex-col bg-page">
      <header className="flex items-center gap-3 border-b border-line/70 px-4 pb-3 pt-[max(1rem,env(safe-area-inset-top))]">
        <button
          type="button"
          onClick={onBack}
          className="rounded-full border border-line bg-card px-3 py-2 text-sm"
        >
          返回
        </button>
        <div>
          <h1 className="text-lg font-semibold text-ink">同步</h1>
          <p className="text-xs text-muted">本地局域网 / 服务器模式可选</p>
        </div>
      </header>

      <main className="flex-1 space-y-4 overflow-auto px-4 py-4">
        <ServerSyncCard />
        <section className="rounded-2xl border border-line bg-card p-4">
          <p className="text-sm text-muted">这台设备</p>
          <p className="mt-1 text-lg font-semibold text-ink">
            {session.device_name || deviceName || '本机'}
          </p>
          {session.code ? (
            <>
              <p className="mt-4 text-sm text-muted">让对方输入这个同步码</p>
              <p className="mt-1 font-mono text-4xl tracking-[0.35em] text-accent">
                {session.code}
              </p>
            </>
          ) : (
            <p className="mt-3 text-sm text-muted">服务启动中…</p>
          )}
          <p className="mt-3 text-xs text-faint">{hint}</p>
        </section>

        <section className="rounded-2xl border border-line bg-card p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-ink">发现到的设备</p>
              <p className="text-xs text-muted">同一 Wi-Fi 下会自动出现</p>
            </div>
            <button
              type="button"
              onClick={() => void api.syncPeers().then((r) => setPeers(r.peers))}
              className="rounded-full border border-line px-3 py-1.5 text-xs"
            >
              刷新
            </button>
          </div>
          <div className="mt-3 space-y-2">
            {peers.length === 0 && <p className="text-sm text-muted">暂时没发现别的设备。</p>}
            {peers.map((peer) => (
              <button
                key={peer.device_id}
                type="button"
                onClick={() => setSelected(peer)}
                className={`flex w-full items-center justify-between rounded-xl border px-3 py-3 text-left ${
                  selected?.device_id === peer.device_id
                    ? 'border-accent bg-accent/10'
                    : 'border-line bg-page'
                }`}
              >
                <span>
                  <span className="block text-sm font-medium text-ink">{peer.name}</span>
                  <span className="block text-xs text-muted">
                    {peer.kind} · {peer.ip}
                  </span>
                </span>
                <span className="text-xs text-accent">
                  {selected?.device_id === peer.device_id ? '已选择' : '选择'}
                </span>
              </button>
            ))}
          </div>
          <input
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
            inputMode="numeric"
            placeholder="输入对方屏幕上的 6 位码"
            className="mt-4 w-full rounded-xl border border-line bg-page px-3 py-3 text-center font-mono text-2xl tracking-[0.25em] text-ink outline-none focus:border-accent"
          />
          <div className="mt-4 space-y-2">
            <p className="text-sm font-medium text-ink">同步方向</p>
            {DIRECTIONS.map((item) => (
              <button
                key={item.value}
                type="button"
                onClick={() => setDirection(item.value)}
                className={`flex w-full items-center justify-between rounded-xl border px-3 py-2.5 text-left ${
                  direction === item.value
                    ? 'border-accent bg-accent/10'
                    : 'border-line bg-page'
                }`}
              >
                <span>
                  <span className="block text-sm text-ink">{item.label}</span>
                  <span className="block text-xs text-muted">{item.hint}</span>
                </span>
                <span className="text-xs text-accent">
                  {direction === item.value ? '已选择' : '选择'}
                </span>
              </button>
            ))}
          </div>
          <button
            type="button"
            disabled={busy || !selected}
            onClick={() => void run()}
            className="mt-3 w-full rounded-xl bg-accent px-4 py-3 text-sm font-semibold text-white disabled:opacity-40"
          >
            {busy ? '同步中…' : '开始同步'}
          </button>
        </section>

        {result && (
          <section className="rounded-2xl border border-line bg-card p-4 text-sm text-ink">
            <p className="font-medium">同步完成</p>
            {result.direction === 'pull' ? (
              <p className="mt-1 text-muted">只拉取：从对方收下 {total(result.pulled)} 条。</p>
            ) : result.direction === 'push' ? (
              <p className="mt-1 text-muted">只推送：发给对方 {total(result.pushed)} 条。</p>
            ) : (
              <p className="mt-1 text-muted">
                从对方拉取 {total(result.pulled)} 条；推给对方 {total(result.pushed)} 条。
              </p>
            )}
          </section>
        )}
      </main>
    </div>
  )
}
