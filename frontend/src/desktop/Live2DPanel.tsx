import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import Live2DCharacter from '../shared/Live2DCharacter'
import {
  api,
  type Live2DModelSummary,
  type Live2DRuntimeStatus,
} from '../shared/api'
import type {
  Live2DLabel,
  Live2DModelMeta,
  Live2DModelSettings,
} from '../shared/live2dPresets'

const BASIC_PARAMS: { id: string; label: string }[] = [
  { id: 'ParamEyeLOpen', label: '左眼开合' },
  { id: 'ParamEyeROpen', label: '右眼开合' },
  { id: 'ParamEyeLSmile', label: '左眼笑' },
  { id: 'ParamEyeRSmile', label: '右眼笑' },
  { id: 'ParamEyeSmile', label: '双眼笑' },
  { id: 'ParamEyeAnger', label: '眼怒' },
  { id: 'ParamEyePuff', label: '眼肿' },
  { id: 'ParamMouthForm', label: '嘴形' },
  { id: 'ParamMouthOpenY', label: '张嘴' },
  { id: 'ParamMouthX', label: '歪嘴' },
  { id: 'ParamCheek', label: '脸红' },
  { id: 'ParamBrowLY', label: '左眉高度' },
  { id: 'ParamBrowRY', label: '右眉高度' },
  { id: 'ParamBrowLAngle', label: '左眉角度' },
  { id: 'ParamBrowRAngle', label: '右眉角度' },
  { id: 'ParamAngleX', label: '头左右' },
  { id: 'ParamAngleY', label: '头上下' },
  { id: 'ParamAngleZ', label: '歪头' },
  { id: 'ParamBodyAngleX', label: '身体前后' },
  { id: 'ParamBodyAngleZ', label: '身体倾斜' },
  { id: 'ParamBreath', label: '呼吸' },
  { id: 'ParamEyeBallX', label: '视线左右' },
  { id: 'ParamEyeBallY', label: '视线上下' },
  { id: 'ParamHairFront', label: '前发' },
  { id: 'ParamHairSide', label: '侧发' },
  { id: 'ParamHairBack', label: '后发' },
]

const LIVE2D_PROPRIETARY_LICENSE_URL =
  'https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html'
const LIVE2D_OPEN_LICENSE_URL =
  'https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html'
const LIVE2D_DOWNLOAD_PAGE_URL = 'https://www.live2d.com/en/sdk/download/web/'

function cloneSettings(settings: Live2DModelSettings): Live2DModelSettings {
  return JSON.parse(JSON.stringify(settings)) as Live2DModelSettings
}

function NumberSlider({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  onChange: (value: number) => void
}) {
  return (
    <label className="block rounded-lg border border-line/60 bg-surface/45 px-2 py-1.5">
      <span className="flex items-center justify-between gap-2 text-[11px] text-muted">
        <span className="truncate">{label}</span>
        <span className="shrink-0 font-mono text-[10px] text-faint">{value.toFixed(2)}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="mt-1 h-1.5 w-full accent-[var(--accent)]"
      />
    </label>
  )
}

export default function Live2DPanel() {
  const [models, setModels] = useState<Live2DModelSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [meta, setMeta] = useState<Live2DModelMeta | null>(null)
  const [draft, setDraft] = useState<Live2DModelSettings | null>(null)
  const [activeLabelId, setActiveLabelId] = useState<string | null>(null)
  const [newLabelName, setNewLabelName] = useState('')
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [testSpeaking, setTestSpeaking] = useState(false)
  const [runtime, setRuntime] = useState<Live2DRuntimeStatus | null>(null)
  const [runtimeAgreed, setRuntimeAgreed] = useState(false)
  const [runtimeBusy, setRuntimeBusy] = useState(false)
  const [runtimeError, setRuntimeError] = useState('')
  const [runtimeProgress, setRuntimeProgress] = useState({ received: 0, total: 0 })
  const importInputRef = useRef<HTMLInputElement | null>(null)

  const refreshRuntime = useCallback(async () => {
    try {
      setRuntime(await api.getLive2DRuntimeStatus())
    } catch (error) {
      setRuntimeError(`读取 Live2D 运行时状态失败：${String(error)}`)
    }
  }, [])

  useEffect(() => {
    void refreshRuntime()
  }, [refreshRuntime])

  useEffect(() => {
    void api
      .listLive2DModels()
      .then((items) => {
        setModels(items)
        setSelectedId((current) => current ?? items[0]?.id ?? null)
      })
      .catch((error) => setMessage(`读取模型列表失败：${String(error)}`))
  }, [])

  useEffect(() => {
    if (!selectedId) return
    setBusy(true)
    setMessage('')
    void api
      .getLive2DModelMeta(selectedId)
      .then((data) => {
        setMeta(data)
        const next = cloneSettings(data.settings)
        setDraft(next)
        setActiveLabelId(
          next.labels.find((item) => item.name === next.default_label)?.id ??
            next.labels[0]?.id ??
            null,
        )
      })
      .catch((error) => setMessage(`读取模型设置失败：${String(error)}`))
      .finally(() => setBusy(false))
  }, [selectedId])

  const activeLabel = useMemo(
    () => draft?.labels.find((item) => item.id === activeLabelId) ?? null,
    [draft, activeLabelId],
  )

  const basicParams = useMemo(
    () =>
      BASIC_PARAMS.map((item) => ({
        ...item,
        meta: meta?.parameters.find((param) => param.id === item.id),
      })).filter((item) => item.meta),
    [meta],
  )

  const advancedParams = useMemo(() => {
    const basic = new Set(BASIC_PARAMS.map((item) => item.id))
    return (meta?.parameters ?? []).filter((param) => !basic.has(param.id))
  }, [meta])

  const patchLabel = useCallback(
    (patch: Partial<Live2DLabel>) => {
      if (!draft || !activeLabelId) return
      const current = draft.labels.find((label) => label.id === activeLabelId)
      const renamedDefault =
        patch.name && current?.name === draft.default_label ? patch.name : draft.default_label
      setDraft({
        ...draft,
        default_label: renamedDefault,
        labels: draft.labels.map((label) =>
          label.id === activeLabelId ? { ...label, ...patch } : label,
        ),
      })
    },
    [draft, activeLabelId],
  )

  const setParam = useCallback(
    (paramId: string, value: number) => {
      if (!activeLabel) return
      patchLabel({ params: { ...activeLabel.params, [paramId]: value } })
    },
    [activeLabel, patchLabel],
  )

  const toggleExpression = useCallback(
    (expressionId: string, enabled: boolean) => {
      if (!activeLabel) return
      const current = new Set(activeLabel.expressions ?? [])
      if (enabled) {
        const target = meta?.expressions.find((item) => item.id === expressionId)
        const targetParams = new Set((target?.params ?? []).map((item) => item.id))
        for (const exprId of [...current]) {
          if (exprId === expressionId) continue
          const other = meta?.expressions.find((item) => item.id === exprId)
          const conflicts = (other?.params ?? []).some((item) => targetParams.has(item.id))
          if (conflicts) {
            current.delete(exprId)
            setMessage(`「${exprId}」和「${expressionId}」会抢同一组参数，已自动取消前者`)
          }
        }
        current.add(expressionId)
      } else {
        current.delete(expressionId)
      }
      patchLabel({ expressions: [...current] })
    },
    [activeLabel, meta, patchLabel],
  )

  const addLabel = useCallback(() => {
    if (!draft) return
    const name = newLabelName.trim()
    if (!name) return
    if (draft.labels.some((label) => label.name === name)) {
      setMessage('这个标签名已经有了')
      return
    }
    const label: Live2DLabel = {
      id: `label_${Date.now().toString(36)}`,
      name,
      aliases: [],
      params: {},
      expressions: [],
      transition_ms: null,
    }
    setDraft({ ...draft, labels: [...draft.labels, label] })
    setActiveLabelId(label.id)
    setNewLabelName('')
    setMessage('')
  }, [draft, newLabelName])

  const deleteLabel = useCallback(() => {
    if (!draft || !activeLabel || draft.labels.length <= 1) return
    if (activeLabel.name === draft.default_label) {
      setMessage('默认标签不能删；先把别的标签设为默认')
      return
    }
    const labels = draft.labels.filter((label) => label.id !== activeLabel.id)
    setDraft({ ...draft, labels })
    setActiveLabelId(labels[0]?.id ?? null)
    setMessage('')
  }, [draft, activeLabel])

  const save = useCallback(async () => {
    if (!draft || !selectedId) return
    setBusy(true)
    setMessage('')
    try {
      const result = await api.saveLive2DModelSettings(selectedId, draft)
      setDraft(cloneSettings(result.settings))
      setMessage('已保存；沉浸模式和桌宠下次加载这个模型时会用新设置')
      window.dispatchEvent(
        new CustomEvent('yf-live2d-settings-changed', {
          detail: { modelId: selectedId },
        }),
      )
    } catch (error) {
      setMessage(`保存失败：${String(error)}`)
    } finally {
      setBusy(false)
    }
  }, [draft, selectedId])

  const preview = useCallback(() => {
    setTestSpeaking(true)
    window.setTimeout(() => setTestSpeaking(false), 1400)
  }, [])

  const importModel = useCallback(
    async (file: File) => {
      setBusy(true)
      setMessage('正在导入模型…')
      try {
        const result = await api.importLive2DModel(file)
        const items = await api.listLive2DModels()
        setModels(items)
        setSelectedId(result.model.id)
        setMessage(`已导入模型：${result.model.name}`)
      } catch (error) {
        setMessage(`导入失败：${String(error)}`)
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  const downloadRuntime = useCallback(async () => {
    if (!runtimeAgreed || runtimeBusy) return
    setRuntimeBusy(true)
    setRuntimeError('')
    setRuntimeProgress({ received: 0, total: 0 })
    try {
      const result = await api.downloadLive2DRuntime((progress) => {
        setRuntimeProgress(progress)
      })
      await refreshRuntime()
      setMessage(`Live2D 运行时已就绪（${(result.size / 1024).toFixed(0)} KB）`)
      window.dispatchEvent(new CustomEvent('yf-live2d-runtime-changed'))
    } catch (error) {
      setRuntimeError(`下载失败：${String(error)}`)
    } finally {
      setRuntimeBusy(false)
    }
  }, [refreshRuntime, runtimeAgreed, runtimeBusy])

  const runtimePercent = runtimeBusy
    ? Math.min(
        96,
        Math.round(
          (runtimeProgress.received /
            Math.max(runtimeProgress.total || 240 * 1024, 1)) *
            100,
        ),
      )
    : runtime?.available
      ? 100
      : 0

  return (
    <section className="grid h-full min-h-0 grid-cols-[150px_minmax(230px,1fr)_minmax(290px,1.2fr)] gap-2.5">
      <aside className="min-h-0 space-y-1.5 overflow-y-auto pr-1">
        <div className="rounded-xl border border-line/60 bg-surface/40 p-2">
          <div className="flex items-center justify-between gap-1">
            <h3 className="text-[12px] font-medium text-ink">Live2D 运行时</h3>
            <span
              className={`rounded-full px-1.5 py-0.5 text-[9.5px] ${
                runtime?.available
                  ? 'bg-emerald-400/12 text-emerald-300'
                  : 'bg-amber-400/12 text-amber-200'
              }`}
            >
              {runtime ? (runtime.available ? '已就绪' : '未安装') : '检查中'}
            </span>
          </div>
          <p className="mt-1 text-[10px] leading-relaxed text-faint">
            浏览器渲染所需的官方 Cubism Core。下载只从 Live2D 官方地址获取，不随应用分发。
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={() => void downloadRuntime()}
              disabled={!runtimeAgreed || runtimeBusy}
              className="rounded-full bg-accent px-2.5 py-1 text-[10px] font-medium text-white disabled:cursor-not-allowed disabled:opacity-40"
            >
              {runtimeBusy
                ? '正在下载…'
                : runtime?.available
                  ? '重新下载并校验'
                  : '下载运行时'}
            </button>
            <a
              href={runtime?.license_urls.proprietary ?? LIVE2D_PROPRIETARY_LICENSE_URL}
              target="_blank"
              rel="noreferrer"
              className="text-[10px] text-accent/90 underline decoration-accent/30 underline-offset-2 hover:text-accent"
            >
              专有软件许可
            </a>
            <a
              href={runtime?.license_urls.open ?? LIVE2D_OPEN_LICENSE_URL}
              target="_blank"
              rel="noreferrer"
              className="text-[10px] text-accent/90 underline decoration-accent/30 underline-offset-2 hover:text-accent"
            >
              开放软件许可
            </a>
          </div>
          <label className="mt-1.5 flex items-start gap-1.5 text-[9.5px] leading-relaxed text-muted">
            <input
              type="checkbox"
              checked={runtimeAgreed}
              onChange={(event) => setRuntimeAgreed(event.target.checked)}
              className="mt-0.5 h-3 w-3 shrink-0 accent-[var(--accent)]"
            />
            <span>我已阅读并同意上述两份 Live2D 官方许可协议。</span>
          </label>
          <p className="mt-1 text-[9.5px] leading-relaxed text-amber-100/65">
            下载此运行时即表示你同意 Live2D 官方许可协议。
          </p>
          {runtimeBusy && (
            <div className="mt-1.5">
              <div className="h-1.5 overflow-hidden rounded-full bg-white/10">
                <div
                  className="h-full rounded-full bg-accent transition-[width] duration-150"
                  style={{ width: `${runtimePercent}%` }}
                />
              </div>
              <p className="mt-1 flex justify-between text-[9px] text-faint">
                <span>{runtimePercent}%</span>
                <span>
                  {(runtimeProgress.received / 1024).toFixed(0)} KB
                  {runtimeProgress.total > 0
                    ? ` / ${(runtimeProgress.total / 1024).toFixed(0)} KB`
                    : ''}
                </span>
              </p>
            </div>
          )}
          {runtimeError && (
            <p className="mt-1 rounded-md bg-red-400/8 px-1.5 py-1 text-[9.5px] leading-relaxed text-red-200/85">
              {runtimeError}
            </p>
          )}
          {runtime?.available && (
            <p className="mt-1 text-[9.5px] text-faint">
              {runtime.size ? `${(runtime.size / 1024).toFixed(0)} KB · ` : ''}
              Live2D 会在预览和沉浸模式中自动启用。
            </p>
          )}
          <a
            href={runtime?.official_download_page ?? LIVE2D_DOWNLOAD_PAGE_URL}
            target="_blank"
            rel="noreferrer"
            className="mt-1 inline-block text-[9.5px] text-faint underline decoration-line-strong/40 underline-offset-2 hover:text-accent"
          >
            打开 Live2D 官方下载页
          </a>
        </div>

        <div>
          <h3 className="text-[12px] font-medium text-ink">Live2D 模型</h3>
          <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">
            设置按模型保存，所有用这个模型的朋友共享。
          </p>
          <button
            type="button"
            onClick={() => importInputRef.current?.click()}
            disabled={busy}
            className="mt-1.5 rounded-full border border-line-strong px-2.5 py-1 text-[10.5px] text-muted hover:border-accent/60 hover:text-accent disabled:opacity-40"
          >
            ＋ 导入模型（zip）
          </button>
          <input
            ref={importInputRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0]
              event.target.value = ''
              if (file) void importModel(file)
            }}
          />
        </div>
        {models.map((model) => (
          <button
            key={model.id}
            type="button"
            onClick={() => setSelectedId(model.id)}
            className={`w-full rounded-xl border px-2.5 py-2 text-left transition-colors ${
              selectedId === model.id
                ? 'border-accent/60 bg-accent/10'
                : 'border-line/60 bg-surface/40 hover:border-accent/35'
            }`}
          >
            <span className="block truncate text-[11.5px] font-medium text-ink">{model.name}</span>
            <span className="mt-0.5 block truncate text-[10px] text-faint">
              {model.used_by.length > 0
                ? `使用者：${model.used_by.map((item) => item.name).join('、')}`
                : '还没有朋友使用'}
            </span>
            <span className="mt-1 flex flex-wrap gap-1">
              {model.is_default && (
                <span className="inline-block rounded-full bg-accent/18 px-1.5 py-0.5 text-[9.5px] text-accent">
                  默认
                </span>
              )}
              {model.has_settings && (
                <span className="inline-block rounded-full bg-accent/12 px-1.5 py-0.5 text-[9.5px] text-accent">
                  已自定义
                </span>
              )}
            </span>
          </button>
        ))}
      </aside>

      <div className="relative min-h-0 overflow-hidden rounded-xl border border-line/60 bg-[#0d141b]">
        <Live2DCharacter
          modelId={selectedId ?? undefined}
          settingsOverride={draft}
          expressionsOverride={meta?.expressions ?? []}
          mood={activeLabel?.name ?? draft?.default_label ?? '正常'}
          speaking={testSpeaking}
        />
        <div className="absolute inset-x-2 bottom-2 flex items-center gap-1.5">
          <button
            type="button"
            onClick={preview}
            className="rounded-full border border-white/15 bg-black/55 px-2.5 py-1 text-[10.5px] text-white/85 hover:bg-black/70"
          >
            试说一句
          </button>
          <span className="rounded-full bg-black/45 px-2 py-1 text-[10px] text-white/60">
            {activeLabel?.name ?? '—'}
          </span>
        </div>
      </div>

      <div className="min-h-0 space-y-2 overflow-y-auto pr-1">
        {busy && <p className="text-[11px] text-muted">正在读取模型…</p>}
        {message && <p className="rounded-lg bg-accent/8 px-2 py-1.5 text-[11px] text-accent">{message}</p>}

        {draft && meta && (
          <>
            <div className="rounded-xl border border-line/60 bg-surface/35 p-2.5">
              <div className="flex items-center gap-2">
                <input
                  value={draft.display_name}
                  onChange={(event) => setDraft({ ...draft, display_name: event.target.value })}
                  className="min-w-0 flex-1 rounded-lg border border-line/70 bg-page px-2 py-1 text-[12px] text-ink outline-none focus:border-accent/60"
                />
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void save()}
                  className="rounded-full bg-accent px-3 py-1 text-[11px] font-medium text-white disabled:opacity-50"
                >
                  保存并定格
                </button>
              </div>
              <div className="mt-2 flex items-center gap-1.5 text-[10.5px] text-faint">
                过渡
                <input
                  type="range"
                  min={0}
                  max={1200}
                  step={20}
                  value={draft.transition_ms}
                  onChange={(event) =>
                    setDraft({ ...draft, transition_ms: Number(event.target.value) })
                  }
                  className="h-1.5 flex-1 accent-[var(--accent)]"
                />
                <span className="w-12 text-right font-mono">{draft.transition_ms}ms</span>
              </div>
            </div>

            <div className="rounded-xl border border-line/60 bg-surface/35 p-2.5">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-[12px] font-medium text-ink">标签</h3>
                <span className="text-[10px] text-faint">模型输出这个名字就切到这个表情</span>
              </div>
              <div className="mt-1.5 flex max-h-[112px] flex-wrap gap-1 overflow-y-auto">
                {draft.labels.map((label) => (
                  <button
                    key={label.id}
                    type="button"
                    onClick={() => setActiveLabelId(label.id)}
                    className={`rounded-full border px-2 py-0.5 text-[10.5px] ${
                      activeLabelId === label.id
                        ? 'border-accent bg-accent/12 text-accent'
                        : 'border-line/60 text-muted hover:border-accent/40'
                    }`}
                  >
                    {label.name}
                    {label.name === draft.default_label ? ' ·默认' : ''}
                  </button>
                ))}
              </div>
              <div className="mt-2 flex gap-1">
                <input
                  value={newLabelName}
                  onChange={(event) => setNewLabelName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') addLabel()
                  }}
                  placeholder="新标签名，例如：大笑"
                  className="min-w-0 flex-1 rounded-lg border border-line/70 bg-page px-2 py-1 text-[11px] text-ink outline-none focus:border-accent/60"
                />
                <button
                  type="button"
                  onClick={addLabel}
                  className="rounded-lg border border-line/70 px-2 py-1 text-[10.5px] text-muted hover:border-accent/50 hover:text-accent"
                >
                  新增
                </button>
              </div>
            </div>

            {activeLabel && (
              <div className="rounded-xl border border-line/60 bg-surface/35 p-2.5">
                <div className="flex items-center gap-1.5">
                  <input
                    value={activeLabel.name}
                    onChange={(event) => patchLabel({ name: event.target.value })}
                    className="min-w-0 flex-1 rounded-lg border border-line/70 bg-page px-2 py-1 text-[11.5px] text-ink outline-none focus:border-accent/60"
                  />
                  <button
                    type="button"
                    onClick={() => setDraft({ ...draft, default_label: activeLabel.name })}
                    className="rounded-lg border border-line/70 px-2 py-1 text-[10px] text-muted hover:border-accent/50 hover:text-accent"
                  >
                    设为默认
                  </button>
                  <button
                    type="button"
                    onClick={deleteLabel}
                    className="rounded-lg border border-red-300/40 px-2 py-1 text-[10px] text-red-300/90 hover:bg-red-400/10"
                  >
                    删除
                  </button>
                </div>

                <h4 className="mt-2 text-[11px] font-medium text-ink">基础参数</h4>
                <div className="mt-1 grid grid-cols-2 gap-1">
                  {basicParams.map(({ id, label, meta: param }) => (
                    <NumberSlider
                      key={id}
                      label={label}
                      value={activeLabel.params[id] ?? param?.default ?? 0}
                      min={param?.min ?? -1}
                      max={param?.max ?? 1}
                      step={Math.max(0.001, ((param?.max ?? 1) - (param?.min ?? -1)) / 1000)}
                      onChange={(value) => setParam(id, value)}
                    />
                  ))}
                </div>

                <button
                  type="button"
                  onClick={() => setAdvancedOpen((open) => !open)}
                  className="mt-2 text-[10.5px] text-faint hover:text-accent"
                >
                  {advancedOpen ? '收起高级参数' : `高级参数（${advancedParams.length}）`}
                </button>
                {advancedOpen && (
                  <div className="mt-1 grid max-h-[260px] grid-cols-2 gap-1 overflow-y-auto">
                    {advancedParams.map((param) => (
                      <NumberSlider
                        key={param.id}
                        label={param.name ?? param.id}
                        value={activeLabel.params[param.id] ?? param.default}
                        min={param.min}
                        max={param.max}
                        step={Math.max(0.001, (param.max - param.min) / 1000)}
                        onChange={(value) => setParam(param.id, value)}
                      />
                    ))}
                  </div>
                )}

                <h4 className="mt-2 text-[11px] font-medium text-ink">表情 / 特效</h4>
                <div className="mt-1 grid grid-cols-2 gap-x-2 gap-y-1">
                  {meta.expressions.map((expression) => (
                    <label key={expression.id} className="flex min-w-0 items-center gap-1.5 text-[10.5px] text-muted">
                      <input
                        type="checkbox"
                        checked={(activeLabel.expressions ?? []).includes(expression.id)}
                        onChange={(event) => toggleExpression(expression.id, event.target.checked)}
                        className="h-3 w-3 accent-[var(--accent)]"
                      />
                      <span className="truncate">{expression.name}</span>
                    </label>
                  ))}
                </div>
              </div>
            )}

            <div className="rounded-xl border border-line/60 bg-surface/35 p-2.5">
              <h3 className="text-[12px] font-medium text-ink">待机晃动</h3>
              <div className="mt-1 grid grid-cols-2 gap-1">
                <NumberSlider
                  label="头左右"
                  value={draft.idle.head_x}
                  min={0}
                  max={12}
                  step={0.1}
                  onChange={(value) => setDraft({ ...draft, idle: { ...draft.idle, head_x: value } })}
                />
                <NumberSlider
                  label="头上下"
                  value={draft.idle.head_y}
                  min={0}
                  max={12}
                  step={0.1}
                  onChange={(value) => setDraft({ ...draft, idle: { ...draft.idle, head_y: value } })}
                />
                <NumberSlider
                  label="歪头"
                  value={draft.idle.head_z}
                  min={0}
                  max={12}
                  step={0.1}
                  onChange={(value) => setDraft({ ...draft, idle: { ...draft.idle, head_z: value } })}
                />
                <NumberSlider
                  label="身体"
                  value={draft.idle.body_x}
                  min={0}
                  max={8}
                  step={0.1}
                  onChange={(value) => setDraft({ ...draft, idle: { ...draft.idle, body_x: value } })}
                />
                <NumberSlider
                  label="视线左右"
                  value={draft.idle.gaze_x}
                  min={0}
                  max={1}
                  step={0.01}
                  onChange={(value) => setDraft({ ...draft, idle: { ...draft.idle, gaze_x: value } })}
                />
                <NumberSlider
                  label="呼吸"
                  value={draft.idle.breath}
                  min={0}
                  max={1}
                  step={0.01}
                  onChange={(value) => setDraft({ ...draft, idle: { ...draft.idle, breath: value } })}
                />
              </div>
            </div>

            <div className="rounded-xl border border-line/60 bg-surface/35 p-2.5">
              <h3 className="text-[12px] font-medium text-ink">说话口型</h3>
              <p className="mt-0.5 text-[10px] text-faint">
                有语音跟音频时长；没有语音时按字数估算。
              </p>
              <div className="mt-1 grid grid-cols-2 gap-1">
                <NumberSlider
                  label="每字毫秒"
                  value={draft.speech.text_ms_per_char}
                  min={40}
                  max={400}
                  step={10}
                  onChange={(value) =>
                    setDraft({ ...draft, speech: { ...draft.speech, text_ms_per_char: value } })
                  }
                />
                <NumberSlider
                  label="张嘴幅度"
                  value={draft.speech.mouth_open}
                  min={0}
                  max={1}
                  step={0.01}
                  onChange={(value) =>
                    setDraft({ ...draft, speech: { ...draft.speech, mouth_open: value } })
                  }
                />
              </div>
            </div>
          </>
        )}
      </div>
    </section>
  )
}
