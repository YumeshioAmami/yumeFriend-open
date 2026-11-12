import { useCallback, useEffect, useMemo, useState } from 'react'

import { api, type PortraitCandidate, type PortraitPack } from '../shared/api'

function prettyBytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export default function PortraitPacksPanel() {
  const [packs, setPacks] = useState<PortraitPack[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [dir, setDir] = useState('')
  const [candidates, setCandidates] = useState<PortraitCandidate[]>([])
  const [usedDir, setUsedDir] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  const reload = useCallback(async () => {
    const items = await api.listPortraitPacks()
    setPacks(items)
    setSelectedId((current) =>
      current && items.some((pack) => pack.id === current) ? current : (items[0]?.id ?? null),
    )
  }, [])

  useEffect(() => {
    void reload().catch((error) => setMessage(`读取立绘包失败：${String(error)}`))
  }, [reload])

  const selected = useMemo(
    () => packs.find((pack) => pack.id === selectedId) ?? null,
    [packs, selectedId],
  )

  const scan = useCallback(async () => {
    if (!dir.trim()) {
      setMessage('先填一个立绘目录')
      return
    }
    setBusy(true)
    setMessage('')
    try {
      const result = await api.scanPortraitPack(dir.trim())
      setCandidates(result.candidates)
      setUsedDir(result.usedDir)
      setMessage(
        result.candidates.length
          ? `认到 ${result.candidates.length} 个情绪（扫的是 ${result.usedDir || dir}）`
          : `这个目录里没有可导入的立绘：${result.usedDir || dir}`,
      )
    } catch (error) {
      setCandidates([])
      setMessage(`扫描失败：${String(error)}`)
    } finally {
      setBusy(false)
    }
  }, [dir])

  const createPack = useCallback(async () => {
    if (!name.trim()) {
      setMessage('先给立绘包起个名字')
      return
    }
    setBusy(true)
    setMessage('')
    try {
      const result = await api.importPortraitPack(name.trim(), dir.trim())
      setName('')
      setDir('')
      setCandidates([])
      setUsedDir('')
      await reload()
      setSelectedId(result.pack.id)
      setMessage(`已导入 ${result.imported.length} 个情绪`)
    } catch (error) {
      setMessage(`导入失败：${String(error)}`)
    } finally {
      setBusy(false)
    }
  }, [dir, name, reload])

  const removePack = useCallback(async () => {
    if (!selected) return
    if (selected.used_by.length > 0) {
      setMessage(`这个包还在给 ${selected.used_by.map((item) => item.name).join('、')} 用`)
      return
    }
    setBusy(true)
    setMessage('')
    try {
      await api.deletePortraitPack(selected.id)
      await reload()
      setMessage('立绘包已删除')
    } catch (error) {
      setMessage(`删除失败：${String(error)}`)
    } finally {
      setBusy(false)
    }
  }, [reload, selected])

  return (
    <section className="grid h-full min-h-0 grid-cols-[190px_minmax(0,1fr)] gap-3">
      <aside className="min-h-0 space-y-1.5 overflow-y-auto pr-1">
        <div>
          <h3 className="text-[12px] font-medium text-ink">全局立绘包</h3>
          <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">
            上传一次，所有朋友都能选。朋友人格里只负责选包。
          </p>
        </div>
        {packs.map((pack) => (
          <button
            key={pack.id}
            type="button"
            onClick={() => setSelectedId(pack.id)}
            className={`w-full rounded-xl border px-2.5 py-2 text-left transition-colors ${
              selectedId === pack.id
                ? 'border-accent/60 bg-accent/10'
                : 'border-line/60 bg-surface/40 hover:border-accent/35'
            }`}
          >
            <span className="block truncate text-[11.5px] font-medium text-ink">{pack.name}</span>
            <span className="mt-0.5 block text-[10px] text-faint">
              {pack.emotions} 个情绪 · {prettyBytes(pack.bytes)}
            </span>
            {pack.used_by.length > 0 && (
              <span className="mt-1 block truncate text-[9.5px] text-accent">
                {pack.used_by.map((item) => item.name).join('、')}
              </span>
            )}
          </button>
        ))}
      </aside>

      <div className="min-h-0 space-y-2 overflow-y-auto pr-1">
        <div className="rounded-xl border border-line/60 bg-surface/35 p-3">
          <h3 className="text-[12px] font-medium text-ink">导入新立绘包</h3>
          <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">
            文件名就是情绪：`正常.png`、`高兴.webp`、`害羞.png`。一个情绪一张。
          </p>
          <div className="mt-2 flex gap-1.5">
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="立绘包名字，例如：汐音-女仆"
              className="min-w-0 flex-1 rounded-lg border border-line bg-page px-2 py-1.5 text-[11.5px] text-ink outline-none focus:border-accent/60"
            />
          </div>
          <div className="mt-1.5 flex gap-1.5">
            <input
              value={dir}
              onChange={(event) => setDir(event.target.value)}
              placeholder="立绘目录（填上一级也会自动往下找 avatar/）"
              className="min-w-0 flex-1 rounded-lg border border-line bg-page px-2 py-1.5 text-[11.5px] text-ink outline-none focus:border-accent/60"
            />
            <button
              type="button"
              onClick={() => void scan()}
              disabled={busy}
              className="rounded-lg border border-line-strong px-2.5 py-1 text-[11px] text-muted hover:border-accent/60 hover:text-accent disabled:opacity-40"
            >
              扫描
            </button>
            <button
              type="button"
              onClick={() => void createPack()}
              disabled={busy || candidates.length === 0}
              className="rounded-lg bg-accent px-2.5 py-1 text-[11px] font-medium text-white disabled:opacity-40"
            >
              导入
            </button>
          </div>
          {usedDir && <p className="mt-1 text-[10px] text-faint">实际扫描：{usedDir}</p>}
          {candidates.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {candidates.map((item) => (
                <span
                  key={item.emotion}
                  className="rounded-full border border-accent/25 bg-accent/8 px-2 py-0.5 text-[10px] text-accent"
                >
                  {item.emotion}
                </span>
              ))}
            </div>
          )}
        </div>

        {message && <p className="rounded-lg bg-accent/8 px-2 py-1.5 text-[11px] text-accent">{message}</p>}

        {selected && (
          <div className="rounded-xl border border-line/60 bg-surface/35 p-3">
            <div className="flex items-center gap-2">
              <h3 className="text-[12px] font-medium text-ink">{selected.name}</h3>
              <span className="text-[10px] text-faint">
                {selected.legacy ? '旧版朋友目录' : '全局包'} · {selected.emotions} 个情绪
              </span>
              {!selected.legacy && (
                <button
                  type="button"
                  onClick={() => void removePack()}
                  disabled={busy || selected.used_by.length > 0}
                  className="ml-auto rounded-full border border-red-300/40 px-2 py-0.5 text-[10px] text-red-300 hover:bg-red-400/10 disabled:opacity-40"
                >
                  删除包
                </button>
              )}
            </div>
            {selected.used_by.length > 0 && (
              <p className="mt-1 text-[10.5px] text-faint">
                正在使用：{selected.used_by.map((item) => item.name).join('、')}
              </p>
            )}
            <div className="mt-3 grid grid-cols-[repeat(auto-fill,minmax(74px,1fr))] gap-2">
              {selected.items.map((item) => (
                <div key={item.emotion} className="flex flex-col items-center gap-1">
                  <img
                    src={`/api/portrait-packs/${encodeURIComponent(selected.id)}/${encodeURIComponent(item.emotion)}?w=160`}
                    alt={item.emotion}
                    className="h-[86px] w-[74px] rounded-lg border border-line/60 bg-chip/20 object-contain"
                  />
                  <span className="max-w-full truncate text-[10px] text-faint">{item.emotion}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
