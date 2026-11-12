/** v7.12（D342）：本地局域网 / 服务器模式切换。
 *
 * 本地模式 = 现有手动局域网同步；服务器模式 = 轻量服务器权威数据源 + 2 秒增量交换。
 * 服务器不跑 LLM / 生活；手机端只聊天、看状态。
 */

import { useEffect, useRef, useState } from 'react'

import { api, type ServerSyncStatus } from './api'

export default function ServerSyncCard() {
  const [status, setStatus] = useState<ServerSyncStatus | null>(null)
  const [mode, setMode] = useState<'local' | 'server'>('local')
  const [baseUrl, setBaseUrl] = useState('')
  const [token, setToken] = useState('')
  const [hint, setHint] = useState('')
  const [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [confirmAction, setConfirmAction] = useState<
    'push' | 'pull' | 'push-memory' | 'pull-memory' | null
  >(null)
  const dirtyRef = useRef(false)

  useEffect(() => {
    dirtyRef.current = dirty
  }, [dirty])

  const load = async () => {
    try {
      const next = await api.serverSyncStatus()
      setStatus(next)
      // 轮询只更新状态；用户正在编辑时不要把选择改回去（否则点服务器模式
      // 几秒后会被 5 秒轮询重置成本地模式）。
      if (!dirtyRef.current) {
        setMode(next.mode)
        setBaseUrl(next.base_url)
      }
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => void load(), 5000)
    return () => window.clearInterval(timer)
  }, [])

  const save = async () => {
    setBusy(true)
    setHint('')
    try {
      const next = await api.setServerSync({
        mode,
        base_url: baseUrl.trim(),
        ...(token.trim() ? { token: token.trim() } : {}),
      })
      setStatus(next)
      setMode(next.mode)
      setBaseUrl(next.base_url)
      setToken('')
      setDirty(false)
      dirtyRef.current = false
      setHint(mode === 'server' ? '服务器模式已保存，正在自动同步' : '已切回本地模式')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const test = async () => {
    setBusy(true)
    setHint('')
    try {
      const result = await api.testServerSync({ base_url: baseUrl.trim(), token: token.trim() })
      setHint(result.ok ? `服务器通了：${String(result.app ?? 'yumeFriend')}` : String(result.error ?? '没通'))
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const syncNow = async () => {
    setBusy(true)
    setHint('')
    try {
      const result = await api.serverSyncNow()
      setHint(
        result.ok
          ? result.skipped === 'paused'
            ? '自动交换已暂停，没有执行'
            : '增量交换完成'
          : `同步失败：${String(result.error ?? '')}`,
      )
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const setPaused = async (paused: boolean) => {
    setBusy(true)
    setHint('')
    try {
      const next = paused ? await api.pauseServerSync() : await api.resumeServerSync()
      setStatus(next)
      setConfirmAction(null)
      setHint(paused ? '自动交换已暂停，可以做全量覆盖' : '自动交换已恢复')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const replaceChat = async (direction: 'push' | 'pull') => {
    if (confirmAction !== direction) {
      setConfirmAction(direction)
      setHint(
        direction === 'push'
          ? '再点一次：会用本机聊天/记忆覆盖服务器（服务器会先备份）'
          : '再点一次：会用服务器聊天/记忆覆盖本机（本机先备份）',
      )
      return
    }
    setBusy(true)
    setHint('')
    try {
      const result =
        direction === 'push'
          ? await api.pushServerChat()
          : await api.pullServerChat()
      setHint(
        result.ok
          ? direction === 'push'
            ? `已推送聊天/记忆到服务器；备份：${String(result.backup ?? '')}`
            : `已从服务器拉取聊天/记忆；本机备份：${String(result.backup ?? '')}`
          : `失败：${String(result.error ?? '')}`,
      )
      setConfirmAction(null)
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const replaceMemory = async (direction: 'push-memory' | 'pull-memory') => {
    if (confirmAction !== direction) {
      setConfirmAction(direction)
      setHint(
        direction === 'push-memory'
          ? '再点一次：只用本机记忆覆盖服务器（服务器会先备份）'
          : '再点一次：只用服务器记忆覆盖本机（本机先备份）',
      )
      return
    }
    setBusy(true)
    setHint('')
    try {
      const result =
        direction === 'push-memory'
          ? await api.pushServerMemories()
          : await api.pullServerMemories()
      setHint(
        result.ok
          ? direction === 'push-memory'
            ? `已推送记忆到服务器；备份：${String(result.backup ?? '')}`
            : `已从服务器拉取记忆；本机备份：${String(result.backup ?? '')}`
          : `失败：${String(result.error ?? '')}`,
      )
      setConfirmAction(null)
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-xl border border-line bg-surface-hover/40 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-[12px] font-medium text-ink">数据模式</h3>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            本地模式走现在的局域网手动同步；服务器模式由轻量服务器保存权威数据，
            两端自动增量同步。
          </p>
        </div>
        <span className="shrink-0 rounded-full border border-line px-2 py-0.5 text-[10px] text-muted">
          {status?.paused
            ? '已暂停'
            : status?.running
              ? '自动同步中'
              : status?.mode === 'server'
                ? '服务器模式'
                : '本地模式'}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={() => {
            setMode('local')
            setDirty(true)
            dirtyRef.current = true
          }}
          className={`rounded-lg border px-3 py-2 text-left text-[12px] ${
            mode === 'local' ? 'border-accent bg-accent/10 text-ink' : 'border-line bg-surface text-muted'
          }`}
        >
          本地 / 局域网
          <span className="mt-0.5 block text-[10px] text-faint">手动发现 + 6 位码</span>
        </button>
        <button
          type="button"
          onClick={() => {
            setMode('server')
            setDirty(true)
            dirtyRef.current = true
          }}
          className={`rounded-lg border px-3 py-2 text-left text-[12px] ${
            mode === 'server' ? 'border-accent bg-accent/10 text-ink' : 'border-line bg-surface text-muted'
          }`}
        >
          服务器
          <span className="mt-0.5 block text-[10px] text-faint">实时增量交换</span>
        </button>
      </div>

      {mode === 'server' && (
        <div className="mt-3 space-y-2">
          <input
            value={baseUrl}
            onChange={(event) => {
              setBaseUrl(event.target.value)
              setDirty(true)
              dirtyRef.current = true
            }}
            placeholder="https://你的服务器"
            className="w-full rounded-lg border border-line bg-surface px-3 py-2 text-[12px] text-ink outline-none focus:border-accent"
          />
          <input
            value={token}
            onChange={(event) => {
              setToken(event.target.value)
              setDirty(true)
              dirtyRef.current = true
            }}
            type="password"
            placeholder={status?.token_set ? '密钥已保存；留空 = 不改' : '服务器共享密钥 YF_SERVER_TOKEN'}
            className="w-full rounded-lg border border-line bg-surface px-3 py-2 text-[12px] text-ink outline-none focus:border-accent"
          />
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy} onClick={() => void test()} className="rounded-lg border border-line px-3 py-1.5 text-[11px] text-muted disabled:opacity-40">
              测试连接
            </button>
            <button type="button" disabled={busy} onClick={() => void syncNow()} className="rounded-lg border border-line px-3 py-1.5 text-[11px] text-muted disabled:opacity-40">
              立即交换
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void setPaused(!status?.paused)}
              className="rounded-lg border border-line px-3 py-1.5 text-[11px] text-muted disabled:opacity-40"
            >
              {status?.paused ? '恢复自动交换' : '暂停自动交换'}
            </button>
          </div>
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-2">
            <p className="text-[10px] leading-relaxed text-muted">
              全量覆盖只替换聊天记录、记忆和当前状态；朋友资料、游戏、生活线、知识库不动。
              先暂停自动交换，再操作。下面四颗按钮：前两颗是聊天+记忆一起覆盖，
              后两颗只覆盖记忆。
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                type="button"
                disabled={busy || !status?.paused}
                onClick={() => void replaceChat('push')}
                className="rounded-lg border border-amber-500/40 px-3 py-1.5 text-[11px] text-amber-600 disabled:opacity-40"
              >
                {confirmAction === 'push' ? '确认覆盖服务器' : '推送聊天/记忆（覆盖服务器）'}
              </button>
              <button
                type="button"
                disabled={busy || !status?.paused}
                onClick={() => void replaceChat('pull')}
                className="rounded-lg border border-amber-500/40 px-3 py-1.5 text-[11px] text-amber-600 disabled:opacity-40"
              >
                {confirmAction === 'pull' ? '确认覆盖本机' : '拉取聊天/记忆（覆盖本机）'}
              </button>
              <button
                type="button"
                disabled={busy || !status?.paused}
                onClick={() => void replaceMemory('push-memory')}
                className="rounded-lg border border-sky-500/40 px-3 py-1.5 text-[11px] text-sky-600 disabled:opacity-40"
              >
                {confirmAction === 'push-memory' ? '确认覆盖服务器' : '只推送记忆（覆盖服务器）'}
              </button>
              <button
                type="button"
                disabled={busy || !status?.paused}
                onClick={() => void replaceMemory('pull-memory')}
                className="rounded-lg border border-sky-500/40 px-3 py-1.5 text-[11px] text-sky-600 disabled:opacity-40"
              >
                {confirmAction === 'pull-memory' ? '确认覆盖本机' : '只拉取记忆（覆盖本机）'}
              </button>
            </div>
            {!status?.paused && (
              <p className="mt-2 text-[10px] text-amber-600">先点「暂停自动交换」。</p>
            )}
          </div>
          {status?.running && (
            <p className="text-[10px] text-faint">
              自动拉取：每 {Math.max(1, Math.round(status.interval_seconds ?? 2))} 秒增量交换
            </p>
          )}
          {status?.last_sync_at && (
            <p className="text-[10px] text-faint">上次同步：{status.last_sync_at}</p>
          )}
          {status?.last_error && (
            <p className="text-[10px] text-red-400">{status.last_error}</p>
          )}
        </div>
      )}

      <button
        type="button"
        disabled={busy}
        onClick={() => void save()}
        className="mt-3 w-full rounded-lg bg-accent px-4 py-2 text-[12px] font-medium text-white disabled:opacity-40"
      >
        {busy ? '保存中…' : '保存数据模式'}
      </button>
      {hint && <p className="mt-2 text-[11px] text-muted">{hint}</p>}
      {dirty && <p className="mt-1 text-[10px] text-amber-500">有未保存的改动</p>}
    </div>
  )
}
