/** 向量检索设置（D450 / D452）：
 *
 * 普通页只负责「开 / 关」；没有模型时在这里问一句要不要下载。
 * 高级页独立成一张清晰的模型配置页，和聊天模型一样填地址 / 密钥 / 模型名。
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import {
  api,
  type EmbeddingDownloadState,
  type EmbeddingStatus,
  type PurposeConfig,
} from '../shared/api'

function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
}) {
  return (
    <div className="flex items-center gap-0.5 rounded-full border border-line/70 bg-surface/60 p-0.5">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          onClick={() => onChange(option.value)}
          className={`rounded-full px-2.5 py-0.5 text-[11px] transition-colors ${
            value === option.value
              ? 'bg-accent/10 text-accent ring-1 ring-accent/25'
              : 'text-faint hover:text-muted'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

const INPUT =
  'w-full rounded-lg border border-line bg-surface/80 px-2 py-1 text-[11.5px] outline-none transition-colors focus:border-accent/60'

function Row({
  label,
  children,
  hint,
}: {
  label: string
  children: ReactNode
  hint?: string
}) {
  return (
    <label className="flex items-start gap-2">
      <span className="w-[86px] shrink-0 pt-1 text-right text-[11px] text-faint">{label}</span>
      <span className="min-w-0 flex-1">
        {children}
        {hint && <span className="mt-0.5 block text-[10px] leading-relaxed text-faint">{hint}</span>}
      </span>
    </label>
  )
}

function modelBytesText(bytes: number): string {
  if (!bytes) return ''
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function useEmbeddingStatus() {
  const [status, setStatus] = useState<EmbeddingStatus | null>(null)
  const [model, setModel] = useState<EmbeddingDownloadState | null>(null)
  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextModel] = await Promise.all([
        api.embeddingStatus(),
        api.embeddingModel(),
      ])
      setStatus(nextStatus)
      setModel(nextModel)
    } catch {
      /* 状态拿不到不影响主流程 */
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const downloadState = model?.state
  useEffect(() => {
    if (!downloadState || !['downloading', 'verifying', 'extracting'].includes(downloadState)) {
      return
    }
    const timer = window.setInterval(() => void refresh(), 1500)
    return () => window.clearInterval(timer)
  }, [downloadState, refresh])

  return { status, model, refresh }
}

/** 普通设置：只有一个开关；没有模型时才弹出下载询问（行内，不弹窗）。 */
export function EmbeddingToggleCard({
  config,
  onPatch,
  onDownloaded,
}: {
  config: PurposeConfig
  onPatch: (patch: PurposeConfig) => void
  onDownloaded: (info: EmbeddingDownloadState) => void
}) {
  const enabled = config.enabled === true
  const { status, model, refresh } = useEmbeddingStatus()
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const completedRef = useRef(false)
  const startedRef = useRef(false)
  const onDownloadedRef = useRef(onDownloaded)

  useEffect(() => {
    onDownloadedRef.current = onDownloaded
  }, [onDownloaded])

  const configured = Boolean(status?.configured)
  const downloading =
    model !== null && ['downloading', 'verifying', 'extracting'].includes(model.state)
  const percent = Math.max(0, Math.min(100, model?.percent ?? 0))

  useEffect(() => {
    if (
      startedRef.current &&
      model?.state === 'done' &&
      model.installed &&
      !completedRef.current
    ) {
      completedRef.current = true
      startedRef.current = false
      setAsking(false)
      onDownloadedRef.current(model)
    }
  }, [model])

  function setEnabled(value: 'on' | 'off') {
    if (value === 'off') {
      setAsking(false)
      setNote(null)
      onPatch({ enabled: false })
      return
    }
    if (!status) return
    if (configured) {
      onPatch({ enabled: true })
      return
    }
    setAsking(true)
  }

  async function download() {
    if (busy) return
    setBusy(true)
    setNote(null)
    completedRef.current = false
    startedRef.current = true
    try {
      await api.downloadEmbeddingModel()
      await refresh()
    } catch (e) {
      startedRef.current = false
      setNote(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-2xl border border-line/70 bg-surface/70 p-4">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-[13px] font-medium text-ink">语义回忆</h3>
          <p className="mt-1 text-[11.5px] leading-relaxed text-faint">
            她说“上次那个买资料的事”，哪怕字面完全不沾，也能把相关记忆找出来。
            关掉就只走关键词。
          </p>
        </div>
        <Segmented
          value={enabled ? 'on' : 'off'}
          options={[
            { value: 'on', label: '开' },
            { value: 'off', label: '关' },
          ]}
          onChange={setEnabled}
        />
      </div>

      {status && configured && (
        <p className="mt-2 text-[11px] text-muted">
          {enabled ? '已开启' : '已关闭'} · 已建索引 {status.indexed}/{status.total}
        </p>
      )}
      {status && !configured && !downloading && !asking && (
        <p className="mt-2 text-[11px] text-faint">还没有可用的向量模型。</p>
      )}

      {(asking || (enabled && status && !configured)) && !downloading && (
        <div className="mt-3 rounded-xl border border-accent/25 bg-accent/5 p-3">
          <p className="text-[11.5px] leading-relaxed text-ink">
            还没有可用的向量模型。要自动下载推荐模型并开启语义回忆吗？
          </p>
          <p className="mt-1 text-[10.5px] leading-relaxed text-faint">
            约 {model?.size_mb ?? 157} MB，只下一次；之后完全离线。
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void download()}
              disabled={busy}
              className="rounded-xl bg-accent px-3 py-1.5 text-[11.5px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {busy ? '准备下载…' : '自动下载并开启'}
            </button>
            <button
              type="button"
              onClick={() => {
                setAsking(false)
                onPatch({ enabled: false })
              }}
              className="rounded-xl border border-line-strong px-3 py-1.5 text-[11.5px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
            >
              先不用
            </button>
          </div>
        </div>
      )}

      {downloading && model && (
        <div className="mt-3">
          <div className="flex items-center justify-between text-[11px] text-muted">
            <span>
              {model.state === 'downloading'
                ? '正在下载向量模型…'
                : model.state === 'verifying'
                  ? '校验文件…'
                  : '正在解压…'}
            </span>
            <span>{model.state === 'downloading' ? `${percent}%` : ''}</span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-chip">
            <div
              className="h-full rounded-full bg-accent transition-all duration-300"
              style={{ width: `${model.state === 'downloading' ? Math.max(2, percent) : 100}%` }}
            />
          </div>
          <p className="mt-1 text-[10.5px] text-faint">
            {modelBytesText(model.downloaded_bytes)}
            {model.total_bytes ? ` / ${modelBytesText(model.total_bytes)}` : ''}
          </p>
        </div>
      )}

      {model?.state === 'error' && (
        <p className="mt-2 text-[11px] text-red-500">下载失败：{model.error ?? '未知原因'}</p>
      )}
      {note && <p className="mt-2 text-[11px] text-muted">{note}</p>}
    </div>
  )
}

/** 高级设置：一个独立、和聊天模型同样清晰的 embedding 配置页。 */
export function EmbeddingAdvancedPanel({
  config,
  onPatch,
}: {
  config: PurposeConfig
  onPatch: (patch: PurposeConfig) => void
}) {
  const enabled = config.enabled === true
  const provider = config.provider === 'openai' ? 'openai' : 'local'
  const localSource = config.provider === 'local_file' ? 'file' : 'recommended'
  const protocol = config.protocol === 'gemini' ? 'gemini' : 'openai'
  const { status, model, refresh } = useEmbeddingStatus()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<string | null>(null)
  const [test, setTest] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const folderInputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    folderInputRef.current?.setAttribute('webkitdirectory', '')
    folderInputRef.current?.setAttribute('directory', '')
  }, [])

  const installed = Boolean(status?.installed)
  const configured = Boolean(status?.configured)
  const downloading =
    model !== null && ['downloading', 'verifying', 'extracting'].includes(model.state)

  async function rebuild() {
    if (busy) return
    setBusy(true)
    setNote(null)
    try {
      const result = await api.rebuildEmbedding()
      setNote(result.ok ? `补齐了 ${result.added} 条记忆` : result.error ?? '没成功')
      await refresh()
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function runTest() {
    setBusy(true)
    setTest(null)
    try {
      const result = await api.testPurpose('embedding', {
        ...config,
        enabled: true,
      } as unknown as Record<string, unknown>)
      setTest(
        result.ok
          ? `通了 · ${result.latency_ms ?? '?'} ms · ${result.model_echo ?? config.model ?? ''}`
          : result.error ?? '加载失败',
      )
      await refresh()
    } catch (e) {
      setTest(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function downloadRecommended() {
    if (busy) return
    setBusy(true)
    setNote(null)
    try {
      await api.downloadEmbeddingModel()
      await refresh()
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function uploadFolder(files: File[]) {
    if (!files.length) return
    setUploading(true)
    setNote(null)
    try {
      const result = await api.uploadEmbeddingModel(files)
      onPatch({
        provider: 'local_file',
        model_path: result.path,
        dim: config.dim ?? 768,
        pooling: config.pooling ?? 'mean',
        max_length: config.max_length ?? 512,
      })
      setNote(`已导入 ${result.files.length} 个文件`)
      await refresh()
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e))
    } finally {
      setUploading(false)
    }
  }

  function switchProvider(value: 'openai' | 'local') {
    const model = (config.model ?? '').trim()
    if (value === 'openai') {
      const localModel = model.startsWith('jinaai/')
      if (!model || localModel) {
        onPatch({
          provider: 'openai',
          protocol: config.protocol === 'gemini' ? 'gemini' : 'openai',
          model: 'text-embedding-3-small',
          dim: 1536,
          base_url: config.base_url || 'https://api.openai.com/v1',
        })
      } else {
        onPatch({ provider: 'openai' })
      }
      return
    }
    onPatch({
      provider: 'local',
      model: 'jinaai/jina-embeddings-v2-base-zh-quantized',
      dim: 768,
      cache_dir: '',
      allow_download: false,
    })
  }

  function switchLocalSource(value: 'recommended' | 'file') {
    if (value === 'recommended') {
      onPatch({
        provider: 'local',
        model: 'jinaai/jina-embeddings-v2-base-zh-quantized',
        dim: 768,
        cache_dir: '',
        allow_download: false,
      })
    } else {
      onPatch({
        provider: 'local_file',
        model_path: config.model_path || '',
        dim: config.dim || 768,
        pooling: config.pooling || 'mean',
        max_length: config.max_length || 512,
      })
    }
  }

  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-[12px] font-medium text-ink">向量检索模型（语义回忆）</h3>
        <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
          可以接 OpenAI 兼容或 Gemini 的 API，也可以直接选本地 ONNX 模型文件夹
          （model.onnx + tokenizer.json）。换模型后旧向量会自动过期。
        </p>
      </div>

      <div className="rounded-2xl border border-line/70 bg-surface/70 p-3">
        <div className="flex items-center gap-2">
          <div className="min-w-0 flex-1">
            <h4 className="text-[12px] font-medium text-ink">启用语义回忆</h4>
            <p className="mt-0.5 text-[10.5px] text-faint">关掉就是纯关键词检索。</p>
          </div>
          <Segmented
            value={enabled ? 'on' : 'off'}
            options={[
              { value: 'on', label: '开' },
              { value: 'off', label: '关' },
            ]}
            onChange={(value) => onPatch({ enabled: value === 'on' })}
          />
        </div>
      </div>

      <div className="rounded-2xl border border-line/70 bg-surface/70 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Segmented
            value={provider}
            options={[
              { value: 'openai', label: 'API 服务' },
              { value: 'local', label: '本地模型' },
            ]}
            onChange={switchProvider}
          />
        </div>

        {provider === 'local' && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="text-[10.5px] text-faint">模型来源</span>
            <Segmented
              value={localSource}
              options={[
                { value: 'recommended', label: '推荐模型（自动下载）' },
                { value: 'file', label: '本地文件' },
              ]}
              onChange={switchLocalSource}
            />
          </div>
        )}

        <div className="mt-3 space-y-1.5">
          {provider === 'openai' ? (
            <>
              <Row label="协议">
                <select
                  className={INPUT}
                  value={protocol}
                  onChange={(event) =>
                    onPatch({ protocol: event.target.value as 'openai' | 'gemini' })
                  }
                >
                  <option value="openai">OpenAI 兼容 /embeddings</option>
                  <option value="gemini">Gemini batchEmbedContents</option>
                </select>
              </Row>
              <Row label="服务地址">
                <input
                  className={INPUT}
                  value={config.base_url ?? ''}
                  placeholder={
                    protocol === 'gemini'
                      ? 'https://generativelanguage.googleapis.com/v1beta'
                      : 'https://api.openai.com/v1'
                  }
                  onChange={(event) => onPatch({ base_url: event.target.value })}
                />
              </Row>
              <Row label="密钥">
                <input
                  className={INPUT}
                  type="password"
                  value={config.api_key ?? ''}
                  placeholder={config.api_key_set ? '已保存 · 留空不改' : 'sk-...'}
                  onChange={(event) => onPatch({ api_key: event.target.value })}
                />
              </Row>
            </>
          ) : localSource === 'file' ? (
            <>
              <Row label="模型文件夹" hint="至少要有 model.onnx / 任意 .onnx 和 tokenizer.json">
                <div className="flex items-center gap-1.5">
                  <input
                    className={INPUT}
                    value={config.model_path ?? ''}
                    placeholder="选择文件夹，或直接填本地绝对路径"
                    onChange={(event) => onPatch({ model_path: event.target.value })}
                  />
                  <button
                    type="button"
                    onClick={() => folderInputRef.current?.click()}
                    disabled={uploading}
                    className="shrink-0 rounded-lg border border-line-strong px-2.5 py-1 text-[10.5px] text-muted hover:border-accent/50 hover:text-accent disabled:opacity-40"
                  >
                    {uploading ? '导入中…' : '选择文件夹'}
                  </button>
                </div>
              </Row>
              <input
                ref={folderInputRef}
                type="file"
                multiple
                className="hidden"
                onChange={(event) => {
                  const files = Array.from(event.target.files ?? [])
                  void uploadFolder(files)
                  event.target.value = ''
                }}
              />
              <Row label="池化方式">
                <select
                  className={INPUT}
                  value={config.pooling ?? 'mean'}
                  onChange={(event) =>
                    onPatch({ pooling: event.target.value as 'mean' | 'cls' })
                  }
                >
                  <option value="mean">Mean pooling（多数 sentence-transformers）</option>
                  <option value="cls">CLS token</option>
                </select>
              </Row>
              <Row label="最大长度">
                <input
                  className={INPUT}
                  type="number"
                  min={16}
                  max={8192}
                  value={config.max_length ?? 512}
                  onChange={(event) => onPatch({ max_length: Number(event.target.value) })}
                />
              </Row>
            </>
          ) : (
            <div className="rounded-xl border border-line/60 bg-page/40 p-2.5">
              <div className="flex items-center gap-2">
                <span className="text-[11.5px] font-medium text-ink">
                  Jina 中文量化向量模型
                </span>
                <span className="ml-auto text-[10.5px] text-faint">
                  768 维 · Apache-2.0
                </span>
              </div>
              <p className="mt-1 text-[10.5px] leading-relaxed text-faint">
                约 157 MB，从资源页下载一次；下载按钮在下方状态区。不需要填模型名。
              </p>
            </div>
          )}
          {provider === 'openai' && (
            <Row label="模型名">
              <input
                className={INPUT}
                value={config.model ?? ''}
                placeholder={
                  protocol === 'gemini'
                    ? 'gemini-embedding-001'
                    : 'text-embedding-3-small / bge-m3 / nomic-embed-text'
                }
                onChange={(event) => onPatch({ model: event.target.value })}
              />
            </Row>
          )}
          {(provider === 'openai' || localSource === 'file') && (
            <Row label="维度">
              <input
                className={INPUT}
                type="number"
                min={1}
                value={config.dim ?? 768}
                onChange={(event) => onPatch({ dim: Number(event.target.value) })}
              />
            </Row>
          )}

          <Row label="相似度阈值" hint="0.45 是保守默认；调低召回更多，调高更严格。">
            <input
              className={INPUT}
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={config.min_similarity ?? 0.45}
              onChange={(event) => onPatch({ min_similarity: Number(event.target.value) })}
            />
          </Row>
        </div>
      </div>

      <div className="rounded-2xl border border-line/70 bg-surface/70 p-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
          <span className={configured ? 'text-accent' : 'text-faint'}>
            {configured ? '已配置' : '未配置'}
          </span>
          <span>
            {status?.provider === 'openai'
              ? status.protocol === 'gemini'
                ? 'Gemini API'
                : 'OpenAI 兼容 API'
              : status?.provider === 'local_file'
                ? '本地模型文件'
                : '推荐本地模型'}{' '}
            ·{' '}
            {localSource === 'file'
              ? config.model_path || '未选文件夹'
              : provider === 'openai'
                ? status?.model ?? config.model ?? '未填模型名'
                : '推荐本地模型'}
          </span>
          <span>已建索引 {status ? `${status.indexed}/${status.total}` : '…'}</span>
          <span>{status?.ready ? '模型已加载' : '模型未加载'}</span>
          {status && <span>缓存 {status.cache_mb} MB</span>}
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void runTest()}
            disabled={busy}
            className="rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
          >
            {busy ? '测试中…' : '测试连接'}
          </button>
          <button
            type="button"
            onClick={() => void rebuild()}
            disabled={!enabled || busy}
            className="rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
          >
            {busy ? '重建中…' : '重建索引'}
          </button>
          {provider === 'local' && !installed && !downloading && (
            <button
              type="button"
              onClick={() => void downloadRecommended()}
              disabled={busy}
              className="rounded-full border border-accent/30 bg-accent/5 px-2.5 py-0.5 text-[11px] text-accent transition-colors hover:bg-accent/10 disabled:opacity-40"
            >
              {busy ? '准备下载…' : '下载推荐本地模型'}
            </button>
          )}
          {test && <span className="text-[11px] text-muted">{test}</span>}
          {note && <span className="text-[11px] text-muted">{note}</span>}
        </div>

        {downloading && model && (
          <p className="mt-1 text-[10.5px] text-faint">
            正在下载 {model.percent}% · {modelBytesText(model.downloaded_bytes)}
            {model.total_bytes ? ` / ${modelBytesText(model.total_bytes)}` : ''}
          </p>
        )}
        <p className="mt-1 text-[10.5px] leading-relaxed text-faint">
          配置改完先点下面的「保存」，再点「重建索引」；旧模型的向量会按新模型重算。
        </p>
      </div>
    </section>
  )
}
