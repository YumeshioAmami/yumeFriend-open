/** 手机/平板设置（v7.0 / D300）：她的行为、模型、语音、主设备、外观。 */

import { useEffect, useState } from 'react'

import { api, type Settings, type SyncDeviceInfo } from '../shared/api'
import ServerSyncCard from '../shared/ServerSyncCard'
import { applyTheme, readThemeChoice } from '../shared/theme'
import { LEGAL_DOCS } from '../shared/legal'

interface Props {
  onBack: () => void
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-line bg-card p-4">
      <h2 className="text-[15px] font-medium text-ink">{title}</h2>
      {hint && <p className="mt-1 text-[11px] leading-relaxed text-faint">{hint}</p>}
      <div className="mt-3 space-y-3">{children}</div>
    </section>
  )
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[13px] text-ink">{label}</span>
      {hint && <span className="mt-0.5 block text-[11px] text-faint">{hint}</span>}
      <span className="mt-1.5 block">{children}</span>
    </label>
  )
}

const inputClass =
  'w-full rounded-xl border border-line bg-page px-3 py-2.5 text-[14px] text-ink outline-none focus:border-accent'

interface UpdateStatus {
  state: string
  version?: string
  message?: string
  progress?: number
  downloaded_bytes?: number
  total_bytes?: number
  download_url?: string
  apk_url?: string
  notes?: string
  can_pause?: boolean
  can_resume?: boolean
}

interface YfUpdateShell {
  appVersion?: () => string
  checkUpdate?: () => boolean
  downloadUpdate?: () => boolean
  pauseUpdate?: () => boolean
  resumeUpdate?: () => boolean
  currentUpdateStatus?: () => string
  installDownloaded?: () => boolean
}

const DOWNLOAD_PAGE = 'https://www.yumeshio.cn/uploads/yumefriend/'

function parseUpdateStatus(value: UpdateStatus | string): UpdateStatus {
  if (typeof value !== 'string') return value
  try {
    return JSON.parse(value) as UpdateStatus
  } catch {
    return { state: 'error', message: value }
  }
}

function formatBytes(value: number | undefined): string {
  const bytes = Number(value ?? 0)
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 MB'
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export default function SettingsScreen({ onBack }: Props) {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [device, setDevice] = useState<SyncDeviceInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [hint, setHint] = useState('')
  const [theme, setTheme] = useState(readThemeChoice())
  const [chatBase, setChatBase] = useState('')
  const [chatModel, setChatModel] = useState('')
  const [chatKey, setChatKey] = useState('')
  const [ttsEngine, setTtsEngine] = useState('system')
  const [ttsBase, setTtsBase] = useState('')
  const [ttsModel, setTtsModel] = useState('')
  const [ttsVoice, setTtsVoice] = useState('alloy')
  const [ttsKey, setTtsKey] = useState('')
  const [sttBase, setSttBase] = useState('')
  const [sttModel, setSttModel] = useState('')
  const [sttKey, setSttKey] = useState('')
  const [appVersion, setAppVersion] = useState('7.0.0')
  const [update, setUpdate] = useState<UpdateStatus>({ state: 'idle', message: '' })

  const load = async () => {
    try {
      const [s, sync] = await Promise.all([api.getSettings(), api.syncStatus()])
      setSettings(s)
      setDevice(sync.device)
      const chat = s.purposes?.chat ?? {}
      setChatBase(String(chat.base_url || ''))
      setChatModel(String(chat.model || ''))
      const tts = (s.purposes?.tts ?? {}) as Record<string, unknown>
      setTtsEngine(String(tts.engine || 'system'))
      setTtsBase(String(tts.base_url || ''))
      setTtsModel(String(tts.model || ''))
      setTtsVoice(String(tts.voice || 'alloy'))
      const stt = (s.purposes?.stt ?? {}) as Record<string, unknown>
      setSttBase(String(stt.base_url || ''))
      setSttModel(String(stt.model || ''))
      setHint('')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }

  useEffect(() => {
    void load()
    const target = window as unknown as {
      yfOnUpdateStatus?: (value: UpdateStatus | string) => void
    }
    target.yfOnUpdateStatus = (value) => {
      setUpdate(parseUpdateStatus(value))
    }
    const shell = (window as unknown as { yfShell?: YfUpdateShell }).yfShell
    try {
      if (shell?.appVersion) setAppVersion(shell.appVersion())
      const current = shell?.currentUpdateStatus?.()
      if (current) setUpdate(parseUpdateStatus(current))
    } catch {
      // ignore
    }
    return () => {
      delete target.yfOnUpdateStatus
    }
  }, [])

  const patch = async (body: Record<string, unknown>, flash = '已保存') => {
    setBusy(true)
    try {
      setSettings(await api.patchSettings(body))
      setHint(flash)
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  const saveChat = () =>
    patch(
      {
        purposes: {
          chat: {
            base_url: chatBase.trim(),
            model: chatModel.trim(),
            ...(chatKey.trim() ? { api_key: chatKey.trim() } : {}),
          },
        },
      },
      '聊天模型已保存'
    )

  const saveTts = () =>
    patch(
      {
        purposes: {
          tts: {
            engine: ttsEngine,
            base_url: ttsBase.trim(),
            model: ttsModel.trim(),
            voice: ttsVoice.trim() || 'alloy',
            ...(ttsKey.trim() ? { api_key: ttsKey.trim() } : {}),
          },
        },
      },
      '语音合成配置已保存'
    )

  const saveStt = () =>
    patch(
      {
        purposes: {
          stt: {
            base_url: sttBase.trim(),
            model: sttModel.trim(),
            ...(sttKey.trim() ? { api_key: sttKey.trim() } : {}),
          },
        },
      },
      '语音识别配置已保存'
    )

  const owner = settings?.autonomy_owner_device_id ?? ''
  const isOwner = Boolean(device?.device_id && owner === device.device_id)

  return (
    <div className="flex min-h-dvh flex-col bg-page">
      <header className="flex items-center gap-3 border-b border-line/70 px-4 pb-3 pt-[max(1rem,env(safe-area-inset-top))]">
        <button type="button" onClick={onBack} className="rounded-full border border-line bg-card px-3 py-2 text-sm">
          返回
        </button>
        <div>
          <h1 className="text-lg font-semibold text-ink">设置</h1>
          <p className="text-xs text-muted">本机配置 · 不依赖电脑</p>
        </div>
      </header>

      <main className="flex-1 space-y-4 overflow-auto px-4 py-4">
        <Section
          title="数据模式 / 服务器"
          hint="本地局域网手动同步，或切到服务器模式自动增量交换。服务器地址和共享密钥在这里配置。"
        >
          <ServerSyncCard />
        </Section>

        <Section title="主设备" hint="主动消息、到点提醒、停歇反思只由一台设备执行。默认手机。">
          <p className="text-[13px] text-muted">
            这台设备：<span className="text-ink">{device?.device_name || '本机'}</span>
          </p>
          <p className="text-[12px] text-faint">
            当前负责：{owner ? (isOwner ? '这台设备' : '另一台设备') : '还没指定'}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy || !device || isOwner}
              onClick={() => void patch({ autonomy_owner_device_id: device?.device_id || '' }, '这台设备开始负责')}
              className="flex-1 rounded-xl bg-accent px-3 py-2.5 text-[13px] text-white disabled:opacity-40"
            >
              这台设备负责
            </button>
            <button
              type="button"
              disabled={busy || !isOwner}
              onClick={() => void patch({ autonomy_owner_device_id: '' }, '这台设备已待机')}
              className="flex-1 rounded-xl border border-line px-3 py-2.5 text-[13px] text-ink disabled:opacity-40"
            >
              设成待机
            </button>
          </div>
        </Section>

        <Section title="她的行为">
          <Field label="主动消息检查间隔（分钟）">
            <input
              className={inputClass}
              type="number"
              min={1}
              value={settings?.active_message_frequency ?? 60}
              disabled={busy || !settings}
              onChange={(e) => void patch({ active_message_frequency: Number(e.target.value) })}
            />
          </Field>
          <Field label="我平时叫什么">
            <input
              className={inputClass}
              value={settings?.my_name ?? '我'}
              disabled={busy || !settings}
              onChange={(e) => void patch({ my_name: e.target.value })}
            />
          </Field>
          <Field label="深度思考" hint="代价是更慢、更贵；聊天闲聊建议关。">
            <select
              className={inputClass}
              value={settings?.deep_thinking ? '1' : '0'}
              disabled={busy || !settings}
              onChange={(e) => void patch({ deep_thinking: e.target.value === '1' })}
            >
              <option value="0">关</option>
              <option value="1">开</option>
            </select>
          </Field>
        </Section>

        <Section title="聊天模型" hint="这台设备自己的 OpenAI 兼容接口；地址 / 模型 / Key 都不同步。">
          <Field label="服务地址">
            <input className={inputClass} value={chatBase} onChange={(e) => setChatBase(e.target.value)} placeholder="https://api.deepseek.com/v1" />
          </Field>
          <Field label="模型名">
            <input className={inputClass} value={chatModel} onChange={(e) => setChatModel(e.target.value)} placeholder="deepseek-flash" />
          </Field>
          <Field label="API Key" hint={settings?.purposes?.chat?.api_key_set ? '已经保存了一个 key；留空 = 不改' : '还没保存 key'}>
            <input className={inputClass} type="password" value={chatKey} onChange={(e) => setChatKey(e.target.value)} placeholder="留空 = 不改" />
          </Field>
          <button type="button" disabled={busy} onClick={() => void saveChat()} className="w-full rounded-xl bg-accent px-3 py-2.5 text-[13px] text-white disabled:opacity-40">
            保存聊天模型
          </button>
        </Section>

        <Section title="语音合成（她说话）" hint="云端优先；没配置就走 Android 系统 TTS。">
          <Field label="引擎">
            <select className={inputClass} value={ttsEngine} onChange={(e) => setTtsEngine(e.target.value)}>
              <option value="system">Android 系统语音</option>
              <option value="cloud">云端 OpenAI 兼容</option>
            </select>
          </Field>
          {ttsEngine === 'cloud' && (
            <>
              <Field label="服务地址">
                <input className={inputClass} value={ttsBase} onChange={(e) => setTtsBase(e.target.value)} placeholder="https://api.example.com/v1" />
              </Field>
              <Field label="模型名">
                <input className={inputClass} value={ttsModel} onChange={(e) => setTtsModel(e.target.value)} placeholder="tts-1" />
              </Field>
              <Field label="音色">
                <input className={inputClass} value={ttsVoice} onChange={(e) => setTtsVoice(e.target.value)} placeholder="alloy" />
              </Field>
              <Field label="API Key" hint={settings?.purposes?.tts?.api_key_set ? '已经保存；留空 = 不改' : '还没保存'}>
                <input className={inputClass} type="password" value={ttsKey} onChange={(e) => setTtsKey(e.target.value)} placeholder="留空 = 不改" />
              </Field>
            </>
          )}
          <button type="button" disabled={busy} onClick={() => void saveTts()} className="w-full rounded-xl bg-accent px-3 py-2.5 text-[13px] text-white disabled:opacity-40">
            保存语音合成
          </button>
        </Section>

        <Section title="语音识别（你说话）" hint="云端优先；没配置时用系统语音输入。">
          <Field label="服务地址">
            <input className={inputClass} value={sttBase} onChange={(e) => setSttBase(e.target.value)} placeholder="https://api.example.com/v1" />
          </Field>
          <Field label="模型名">
            <input className={inputClass} value={sttModel} onChange={(e) => setSttModel(e.target.value)} placeholder="whisper-1" />
          </Field>
          <Field label="API Key" hint={settings?.purposes?.stt?.api_key_set ? '已经保存；留空 = 不改' : '还没保存'}>
            <input className={inputClass} type="password" value={sttKey} onChange={(e) => setSttKey(e.target.value)} placeholder="留空 = 不改" />
          </Field>
          <button type="button" disabled={busy} onClick={() => void saveStt()} className="w-full rounded-xl bg-accent px-3 py-2.5 text-[13px] text-white disabled:opacity-40">
            保存语音识别
          </button>
        </Section>

        <Section title="外观">
          <Field label="主题">
            <select
              className={inputClass}
              value={theme}
              onChange={(e) => {
                const next = e.target.value as 'light' | 'dark' | 'system'
                setTheme(next)
                applyTheme(next)
              }}
            >
              <option value="system">跟随系统</option>
              <option value="light">浅色</option>
              <option value="dark">深色</option>
            </select>
          </Field>
        </Section>

        <Section title="版本更新" hint="从 yumeBlog 静态目录检查；不经过 GitHub，不需要梯子。">
          <p className="text-[13px] text-muted">
            当前版本：<span className="text-ink">{appVersion}</span>
          </p>
          <button
            type="button"
            disabled={update.state === 'checking' || update.state === 'downloading'}
            onClick={() => {
              const shell = (window as unknown as { yfShell?: YfUpdateShell }).yfShell
              if (shell?.checkUpdate) {
                shell.checkUpdate()
              } else {
                setUpdate({ state: 'desktop', message: '桌面端请在桌面版里更新。' })
              }
            }}
            className="w-full rounded-xl border border-line bg-surface px-3 py-2.5 text-[13px] text-ink disabled:opacity-40"
          >
            {update.state === 'checking'
              ? '正在检查…'
              : update.state === 'downloading'
                ? `正在下载${typeof update.progress === 'number' && update.progress >= 0 ? ` ${update.progress}%` : ''}…`
                : '检查更新'}
          </button>
          {update.state === 'downloading' && (
            <div className="space-y-1.5">
              <div className="h-2 overflow-hidden rounded-full bg-page">
                {typeof update.progress === 'number' && update.progress >= 0 ? (
                  <div
                    className="h-full rounded-full bg-accent transition-[width] duration-300"
                    style={{ width: `${Math.max(0, Math.min(100, update.progress))}%` }}
                  />
                ) : (
                  <div className="h-full w-1/3 animate-pulse rounded-full bg-accent" />
                )}
              </div>
              <p className="text-[11px] text-muted">
                {typeof update.progress === 'number' && update.progress >= 0
                  ? `${update.progress}% · ${formatBytes(update.downloaded_bytes)} / ${formatBytes(update.total_bytes)}`
                  : `已下载 ${formatBytes(update.downloaded_bytes)}`}
              </p>
            </div>
          )}
          {update.notes && (
            <pre className="max-h-[26vh] overflow-y-auto whitespace-pre-wrap rounded-xl border border-line bg-page px-3 py-2 font-sans text-[11.5px] leading-relaxed text-muted">
              {update.notes}
            </pre>
          )}
          {update.message && <p className="text-[11px] leading-relaxed text-muted">{update.message}</p>}
          {(update.state === 'available' || update.state === 'available_mobile') && (
            <button
              type="button"
              onClick={() => {
                const shell = (window as unknown as { yfShell?: YfUpdateShell }).yfShell
                shell?.downloadUpdate?.()
              }}
              className="w-full rounded-xl bg-accent px-3 py-2.5 text-[13px] font-medium text-white"
            >
              下载更新
            </button>
          )}
          {update.state === 'downloading' && (
            <button
              type="button"
              onClick={() => {
                const shell = (window as unknown as { yfShell?: YfUpdateShell }).yfShell
                shell?.pauseUpdate?.()
              }}
              className="w-full rounded-xl border border-line bg-surface px-3 py-2.5 text-[13px] text-ink"
            >
              暂停下载
            </button>
          )}
          {(update.state === 'paused' || (update.state === 'error' && update.can_resume)) && (
            <button
              type="button"
              onClick={() => {
                const shell = (window as unknown as { yfShell?: YfUpdateShell }).yfShell
                shell?.resumeUpdate?.()
              }}
              className="w-full rounded-xl border border-accent bg-accent/10 px-3 py-2.5 text-[13px] text-accent"
            >
              继续下载
            </button>
          )}
          {update.state === 'downloaded' && (
            <button
              type="button"
              onClick={() => {
                const shell = (window as unknown as { yfShell?: YfUpdateShell }).yfShell
                if (!shell?.installDownloaded?.()) {
                  setUpdate((prev) => ({ ...prev, message: '没找到已下载的安装包，请重新检查更新。' }))
                }
              }}
              className="w-full rounded-xl border border-accent bg-accent/10 px-3 py-2.5 text-[13px] text-accent"
            >
              重新打开安装器
            </button>
          )}
          <a
            href={update.download_url || DOWNLOAD_PAGE}
            target="_blank"
            rel="noreferrer"
            className="block rounded-xl border border-line bg-surface px-3 py-2.5 text-center text-[13px] text-ink"
          >
            手动下载页
          </a>
          <p className="text-[11px] leading-relaxed text-faint">
            下载中可以暂停，之后会从断点继续；离开这一页也会保留进度。
            第一次安装可能需要允许「安装未知应用」。如果自动安装没弹出来，可点上面的手动下载页。
          </p>
        </Section>

        <Section title="用户声明与协议" hint="使用本软件即表示你理解以下内容。">
          {LEGAL_DOCS.map((doc) => (
            <details
              key={doc.id}
              className="rounded-xl border border-line bg-page/50 px-3 py-2"
            >
              <summary className="cursor-pointer text-[13px] font-medium text-ink">
                {doc.title}
              </summary>
              <pre className="mt-2 max-h-[34dvh] overflow-y-auto whitespace-pre-wrap border-t border-line/60 pt-2 font-sans text-[11px] leading-[1.8] text-muted">
                {doc.body}
              </pre>
            </details>
          ))}
        </Section>

        {hint && <p className="px-1 text-[12px] text-muted">{hint}</p>}
      </main>
    </div>
  )
}
