/** 桌面端同步页（D302）：和设备发现 / 6 位码 / 手动同步同一套 API。 */

import { useCallback, useEffect, useState } from 'react'

import {
  api,
  type SyncDirection,
  type SyncPeer,
  type SyncRunResult,
  type SyncSessionInfo,
} from '../shared/api'
import ServerSyncCard from '../shared/ServerSyncCard'

const DIRECTIONS: { value: SyncDirection; label: string; hint: string }[] = [
  { value: 'both', label: '双向同步', hint: '先收对方的，再把自己的发过去' },
  { value: 'pull', label: '只拉取', hint: '对方 → 本机；不改对方' },
  { value: 'push', label: '只推送', hint: '本机 → 对方；不把对方数据拉回来' },
]

export default function SyncPanel() {
  const [session, setSession] = useState<SyncSessionInfo>({})
  const [peers, setPeers] = useState<SyncPeer[]>([])
  const [selected, setSelected] = useState<SyncPeer | null>(null)
  const [code, setCode] = useState('')
  const [direction, setDirection] = useState<SyncDirection>('both')
  const [busy, setBusy] = useState(false)
  const [hint, setHint] = useState('正在启动同步…')
  const [result, setResult] = useState<SyncRunResult | null>(null)

  const start = useCallback(async () => {
    try {
      const info = await api.syncStart({ name: '电脑', kind: 'desktop' })
      setSession(info)
      setHint('让手机或平板打开同步页，选中这台电脑，输入上面 6 位码。')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    void start()
    return () => {
      void api.syncStop().catch(() => undefined)
    }
  }, [start])

  useEffect(() => {
    const tick = async () => {
      try {
        const [st, found] = await Promise.all([api.syncStatus(), api.syncPeers()])
        setSession(st.session)
        setPeers(found.peers)
      } catch {
        // 轮询失败不吵，按钮处再报。
      }
    }
    void tick()
    const timer = window.setInterval(() => void tick(), 2200)
    return () => window.clearInterval(timer)
  }, [])

  const run = async () => {
    if (!selected) return setHint('先选一台设备')
    if (!/^\d{6}$/.test(code)) return setHint('输入对方屏幕上的 6 位数字')
    setBusy(true)
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
    <div className="space-y-5">
      <ServerSyncCard />
      <div>
        <h3 className="text-[12px] font-medium text-ink">设备同步</h3>
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          手机 / 平板 / 电脑各自本地保存。两台设备打开这页，自动发现后选设备、输入 6 位码。
        </p>
      </div>

      <div className="rounded-xl border border-line bg-surface-hover/40 p-4">
        <p className="text-[11px] text-muted">这台设备</p>
        <p className="mt-1 text-[14px] text-ink">{session.device_name || '电脑'}</p>
        {session.code ? (
          <>
            <p className="mt-3 text-[11px] text-muted">同步码</p>
            <p className="mt-1 font-mono text-3xl tracking-[0.35em] text-accent">{session.code}</p>
          </>
        ) : (
          <p className="mt-2 text-[12px] text-muted">服务启动中…</p>
        )}
        <p className="mt-3 text-[11px] text-faint">{hint}</p>
      </div>

      <div className="rounded-xl border border-line p-4">
        <div className="flex items-center justify-between">
          <p className="text-[12px] font-medium text-ink">发现到的设备</p>
          <button
            type="button"
            onClick={() => void api.syncPeers().then((r) => setPeers(r.peers))}
            className="rounded-full border border-line px-3 py-1 text-[11px]"
          >
            刷新
          </button>
        </div>
        <div className="mt-3 space-y-2">
          {peers.length === 0 && <p className="text-[12px] text-muted">暂时没发现别的设备。</p>}
          {peers.map((peer) => (
            <button
              key={peer.device_id}
              type="button"
              onClick={() => setSelected(peer)}
              className={`flex w-full items-center justify-between rounded-lg border px-3 py-2 text-left ${
                selected?.device_id === peer.device_id
                  ? 'border-accent bg-accent/10'
                  : 'border-line bg-surface'
              }`}
            >
              <span>
                <span className="block text-[12px] text-ink">{peer.name}</span>
                <span className="block text-[10px] text-muted">{peer.kind} · {peer.ip}</span>
              </span>
              <span className="text-[10px] text-accent">
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
          className="mt-3 w-full rounded-lg border border-line bg-surface px-3 py-2.5 text-center font-mono text-xl tracking-[0.25em] text-ink outline-none focus:border-accent"
        />
        <div className="mt-3 space-y-2">
          <p className="text-[12px] font-medium text-ink">同步方向</p>
          {DIRECTIONS.map((item) => (
            <button
              key={item.value}
              type="button"
              onClick={() => setDirection(item.value)}
              className={`flex w-full items-center justify-between rounded-lg border px-3 py-2 text-left ${
                direction === item.value
                  ? 'border-accent bg-accent/10'
                  : 'border-line bg-surface'
              }`}
            >
              <span>
                <span className="block text-[12px] text-ink">{item.label}</span>
                <span className="block text-[10px] text-muted">{item.hint}</span>
              </span>
              <span className="text-[10px] text-accent">
                {direction === item.value ? '已选择' : '选择'}
              </span>
            </button>
          ))}
        </div>
        <button
          type="button"
          disabled={busy || !selected}
          onClick={() => void run()}
          className="mt-3 w-full rounded-lg bg-accent px-4 py-2.5 text-[12px] font-medium text-white disabled:opacity-40"
        >
          {busy ? '同步中…' : '开始同步'}
        </button>
      </div>

      {result && (
        <p className="text-[12px] text-muted">
          {result.direction === 'pull'
            ? `只拉取：从对方收下 ${Object.values(result.pulled ?? {}).reduce((a, b) => a + b, 0)} 条。`
            : result.direction === 'push'
              ? `只推送：发给对方 ${Object.values(result.pushed ?? {}).reduce((a, b) => a + b, 0)} 条。`
              : `双向同步完成：拉取 ${Object.values(result.pulled ?? {}).reduce((a, b) => a + b, 0)} 条，推送 ${Object.values(result.pushed ?? {}).reduce((a, b) => a + b, 0)} 条。`}
        </p>
      )}
    </div>
  )
}
