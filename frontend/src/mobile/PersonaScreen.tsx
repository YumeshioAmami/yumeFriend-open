/**
 * 手机壳：某个朋友的**人格**（v3.0 / B8）。
 *
 * 手机上做这些（用户点名"人格界面全留着"、"手机上换头像肯定更常见"）：
 *   · 名字 / 人格 / 说话习惯 —— 改完按保存
 *   · **头像**：上传（相册/拍照）→ **裁剪器**（跟电脑同一份 `shared/ImageCropper`）→ 保存
 *   · **背景**（沉浸模式那张）：上传 / 删除
 *   · **音色**：选一个 + **试听** + 「她的回复都用语音」开关
 *   · **立绘**：只**看**（她有哪些情绪）—— **导入留在电脑上**（要整个目录，手机没这个概念，D140）
 */

import { useEffect, useRef, useState } from 'react'

import Avatar from '../shared/Avatar'
import ImageCropper from '../shared/ImageCropper'
import StyleProfileEditor from '../shared/StyleProfileEditor'
import TemperatureField from '../shared/TemperatureField'
import { api, type FriendDetail, type LlmBase, type Portrait } from '../shared/api'

/** v3.9（D160）：人格页「直接填一个端点」的选项值。 */
const CUSTOM_BASE = '__custom__'

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="mt-4 rounded-2xl border border-line/70 bg-surface/70 p-4">
      <h2 className="text-[13px] font-medium text-ink">{title}</h2>
      {hint && <p className="mt-1 text-[11px] leading-snug text-faint">{hint}</p>}
      <div className="mt-2">{children}</div>
    </section>
  )
}

const inputClass =
  'w-full rounded-xl border border-line bg-surface px-3 py-2.5 text-[14px] text-ink outline-none focus:border-accent/60'

export default function PersonaScreen({
  friendId,
  onBack,
  onChanged,
}: {
  friendId: string
  onBack: () => void
  onChanged: () => void
}) {
  const [friend, setFriend] = useState<FriendDetail | null>(null)
  const [name, setName] = useState('')
  const [personality, setPersonality] = useState('')
  const [instructions, setInstructions] = useState('')
  // v6.0（D298）：声音样本（一行一段）
  const [voiceSamples, setVoiceSamples] = useState('')
  // v4.3（D249）：她的温度（null = 跟随默认）
  const [temperature, setTemperature] = useState<number | null>(null)
  // v7.12（D341）：生活线开关（手机只改开关，不推进生活）
  const [lifeEnabled, setLifeEnabled] = useState(false)
  const [chatPacingEnabled, setChatPacingEnabled] = useState(false)
  const [humanizerEnabled, setHumanizerEnabled] = useState(false)
  // v3.5（D151）：可选的模型端点（= 模型预设）
  const [bases, setBases] = useState<LlmBase[]>([])
  // v3.9（D160）：手机上也能**直接填**服务地址 / 密钥 / 模型（保存成一条预设）
  const [customPanel, setCustomPanel] = useState(false)
  const [customUrl, setCustomUrl] = useState('')
  const [customModel, setCustomModel] = useState('')
  const [customKey, setCustomKey] = useState('')
  const [customName, setCustomName] = useState('')
  const [customVision, setCustomVision] = useState(true)
  const [portraits, setPortraits] = useState<Portrait[]>([])
  const [batchItems, setBatchItems] = useState<{ file: File; emotion: string; preview: string }[]>([])
  const [batchBusy, setBatchBusy] = useState(false)
  const [cropping, setCropping] = useState<File | null>(null)
  const [hint, setHint] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const avatarRef = useRef<HTMLInputElement>(null)
  const batchRef = useRef<HTMLInputElement>(null)
  const backgroundRef = useRef<HTMLInputElement>(null)

  async function load() {
    try {
      const detail = await api.getFriend(friendId)
      setFriend(detail)
      setName(detail.name)
      setPersonality(detail.personality ?? '')
      setInstructions(detail.custom_instructions ?? '')
      setVoiceSamples((detail.voice_examples ?? []).join('\n'))
      setTemperature(detail.temperature ?? null)
      setLifeEnabled(detail.life_enabled ?? false)
      setChatPacingEnabled(detail.chat_pacing_enabled ?? false)
      setHumanizerEnabled(detail.humanizer_enabled ?? false)
      setPortraits(await api.listPortraits(friendId))
      setBases((await api.getSettings()).llm_bases ?? [])
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }

  useEffect(() => {
    void load()
  }, [friendId])

  async function saveBasics() {
    setBusy(true)
    try {
      await api.updateFriend(friendId, {
        name: name.trim() || '朋友',
        personality,
        custom_instructions: instructions,
        voice_examples: voiceSamples
          .split('\n')
          .map((line) => line.trim())
          .filter(Boolean),
        // v4.3（D249）：null = 恢复默认
        temperature,
        life_enabled: lifeEnabled,
        chat_pacing_enabled: chatPacingEnabled,
        humanizer_enabled: humanizerEnabled,
      })
      setHint('保存好了')
      onChanged()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  /** v3.9（D160）：把"直接填的端点"存成一条模型预设，并把这个朋友指过去。 */
  async function saveCustomBase() {
    if (!customUrl.trim() || !customModel.trim()) {
      setHint('自定义模型还差一点：服务地址和模型名都要填')
      return
    }
    setBusy(true)
    try {
      const presetName = customName.trim() || `自定义-${customModel.trim().slice(0, 12)}`
      const entry: Record<string, unknown> = {
        name: presetName,
        base_url: customUrl.trim(),
        model: customModel.trim(),
        // v3.10（D161）：默认"直连优先"（跟用途卡一致），不是"跟随系统代理"
        proxy: 'auto',
        max_retries: 1,
        capabilities: { vision: customVision },
        ...(customKey.trim() ? { api_key: customKey.trim() } : {}),
      }
      const others = bases
        .filter((base) => base.name !== presetName)
        .map(({ api_key_set: _drop, ...rest }) => rest)
      await api.patchSettings({ llm_bases: [...others, entry] })
      await api.updateFriend(friendId, { llm_base: presetName })
      setFriend(await api.getFriend(friendId))
      setBases((await api.getSettings()).llm_bases ?? [])
      setCustomPanel(false)
      setHint(`以后走「${presetName}」`)
      onChanged()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function avatar(file: File | undefined) {
    if (!file) return
    setBusy(true)
    try {
      setFriend(await api.uploadAvatar(friendId, file))
      setHint('头像换好了')
      onChanged()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function background(file: File | undefined) {
    if (!file) return
    setBusy(true)
    setHint('正在处理（会裁成 16:9）…')
    try {
      await api.uploadBackground(friendId, file)
      await load()
      setHint('背景换好了')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  function pickBatch(files: FileList | null) {
    if (!files?.length) return
    const next = Array.from(files)
      .filter((file) => file.type.startsWith('image/'))
      .map((file) => ({
        file,
        emotion: file.name.replace(/\.[^.]+$/, '').slice(0, 24) || '正常',
        preview: URL.createObjectURL(file),
      }))
    setBatchItems(next)
  }

  async function uploadBatch() {
    if (!batchItems.length) return
    setBatchBusy(true)
    setHint('正在导入立绘…')
    try {
      await api.uploadPortraits(
        friendId,
        batchItems.map((item) => ({ file: item.file, emotion: item.emotion.trim() || '正常' })),
      )
      for (const item of batchItems) URL.revokeObjectURL(item.preview)
      setBatchItems([])
      await load()
      onChanged()
      setHint('立绘批量导入完成')
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBatchBusy(false)
    }
  }

  const globalPortraitPack = Boolean(
    friend?.portrait_pack_id && !friend.portrait_pack_id.startsWith('legacy_'),
  )

  return (
    <div className="min-h-dvh bg-page pb-[max(16px,env(safe-area-inset-bottom))]">
      <header className="sticky top-0 z-10 flex items-center gap-2 border-b border-line/70 bg-page/95 px-2 pb-2.5 pt-[max(8px,env(safe-area-inset-top))] backdrop-blur">
        <button
          type="button"
          onClick={onBack}
          aria-label="返回"
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-[22px] text-muted active:bg-surface-hover"
        >
          ‹
        </button>
        <h1 className="flex-1 truncate text-[17px] font-medium text-ink">
          {friend?.name ?? '…'} · 人格
        </h1>
      </header>

      {hint && <p className="px-4 pt-2 text-[11.5px] text-muted">{hint}</p>}

      <div className="px-4">
        <Section title="头像" hint="点「换头像」从相册/拍照选，下一步能自己框">
          <div className="flex items-center gap-4">
            <Avatar name={name || '她'} url={friend?.avatar_url} size={72} />
            <div className="flex flex-1 flex-col gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => avatarRef.current?.click()}
                className="h-11 rounded-xl border border-line bg-surface text-[13.5px] text-ink active:bg-surface-hover disabled:opacity-40"
              >
                换头像
              </button>
              {friend?.avatar_url && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    void api.deleteAvatar(friendId).then(() => {
                      setHint('头像删了（回退首字母色块）')
                      return load()
                    })
                  }}
                  className="h-11 rounded-xl border border-line bg-surface text-[13.5px] text-muted active:bg-surface-hover"
                >
                  删掉头像
                </button>
              )}
            </div>
          </div>
        </Section>

        <Section title="她是谁" hint="名字、人格、说话习惯 —— 改完按下面「保存」">
          <label className="block">
            <span className="mb-1 block text-[11.5px] text-muted">名字</span>
            <input value={name} onChange={(event) => setName(event.target.value)} className={inputClass} />
          </label>
          <label className="mt-3 block">
            <span className="mb-1 block text-[11.5px] text-muted">人格（她是谁、怎么想事）</span>
            <textarea
              value={personality}
              onChange={(event) => setPersonality(event.target.value)}
              rows={6}
              className={`${inputClass} leading-relaxed`}
            />
          </label>
          <label className="mt-3 block">
            <span className="mb-1 block text-[11.5px] text-muted">说话习惯（口头禅、用词忌讳）</span>
            <textarea
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              rows={4}
              className={`${inputClass} leading-relaxed`}
            />
          </label>
          <label className="mt-3 block">
            <span className="mb-1 block text-[11.5px] text-muted">
              声音样本（一行一段；学语气，不是套句式）
            </span>
            <textarea
              value={voiceSamples}
              onChange={(event) => setVoiceSamples(event.target.value)}
              rows={4}
              placeholder={'行，那不排了。你这种人就别上闹钟。\n我没死。你别把话收得那么快。'}
              className={`${inputClass} leading-relaxed`}
            />
          </label>
          {/* v4.3（D249）：她的温度（越高越跳；不想要就不管它，默认就行） */}
          <div className="mt-3">
            <TemperatureField value={temperature} onChange={setTemperature} disabled={busy} />
            <label className="mt-3 flex items-start gap-3 rounded-xl border border-line bg-surface/60 p-3">
              <input
                type="checkbox"
                checked={lifeEnabled}
                onChange={(event) => setLifeEnabled(event.target.checked)}
                disabled={busy}
                className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
              />
              <span>
                <span className="block text-[12px] text-ink">生活线（仅电脑端）</span>
                <span className="mt-0.5 block text-[10.5px] leading-snug text-faint">
                  手机不推进生活；打开后，她会在电脑上做真实的小事。
                </span>
              </span>
            </label>
            <label className="mt-2 flex items-start gap-3 rounded-xl border border-line bg-surface/60 p-3">
              <input
                type="checkbox"
                checked={chatPacingEnabled}
                onChange={(event) => setChatPacingEnabled(event.target.checked)}
                disabled={busy}
                className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
              />
              <span>
                <span className="block text-[12px] text-ink">轻聊/深聊分流</span>
                <span className="mt-0.5 block text-[10.5px] leading-snug text-faint">
                  日常短句回得短；倒心事时才展开。
                </span>
              </span>
            </label>
            <label className="mt-2 flex items-start gap-3 rounded-xl border border-line bg-surface/60 p-3">
              <input
                type="checkbox"
                checked={humanizerEnabled}
                onChange={(event) => setHumanizerEnabled(event.target.checked)}
                disabled={busy}
                className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
              />
              <span>
                <span className="block text-[12px] text-ink">表达学习 + 偶发手滑</span>
                <span className="mt-0.5 block text-[10.5px] leading-snug text-faint">
                  学你最近的短说法；轻聊时偶尔一个同音小错字。
                </span>
              </span>
            </label>
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={() => void saveBasics()}
            className="mt-3 h-12 w-full rounded-2xl text-[14px] text-white disabled:opacity-40"
            style={{ background: 'var(--color-accent)' }}
          >
            保存
          </button>
        </Section>

        <div className="mt-4">
          <StyleProfileEditor friendId={friendId} mobile onChanged={onChanged} />
        </div>

        {/* v3.5（D151）：每个朋友可以有自己的模型端点（聪明的 / 不设限的，各取所需） */}
        <Section title="她的模型" hint="不选 = 跟随全局；选了 = 她的回复（含主动消息）都走这个端点">
          <select
            value={friend?.llm_base ?? ''}
            disabled={busy}
            onChange={(event) => {
              const llmBase = event.target.value
              // v3.9（D160）：选「＋ 自定义」只是把面板亮出来，**先不动库**
              //（不然会把 '__custom__' 这种假名字写进朋友，下一句话直接报错）
              if (llmBase === CUSTOM_BASE) {
                setCustomPanel(true)
                return
              }
              setCustomPanel(false)
              setBusy(true)
              void api
                .updateFriend(friendId, { llm_base: llmBase })
                .then(() => api.getFriend(friendId))
                .then((detail) => {
                  setFriend(detail)
                  setHint(llmBase ? `以后走「${llmBase}」` : '改回跟随全局了')
                  onChanged()
                })
                .catch((exc: unknown) =>
                  setHint(exc instanceof Error ? exc.message : String(exc)),
                )
                .finally(() => setBusy(false))
            }}
            className="h-11 w-full rounded-xl border border-line bg-surface px-2 text-[13px] text-ink"
          >
            <option value="">跟随全局（默认）</option>
            {bases.map((base) => (
              <option key={base.name} value={base.name}>
                {base.name}（{base.model || '未填模型'}）
              </option>
            ))}
            <option value={CUSTOM_BASE}>＋ 自定义（直接填地址 / 密钥 / 模型）</option>
          </select>
          {customPanel ? (
            <div className="mt-2 space-y-1.5 rounded-xl border border-line/60 bg-page/40 p-2.5">
              <input
                value={customUrl}
                onChange={(event) => setCustomUrl(event.target.value)}
                placeholder="服务地址（https://.../v1）"
                className="h-10 w-full rounded-xl border border-line bg-surface px-2.5 text-[12.5px] text-ink"
              />
              <input
                value={customModel}
                onChange={(event) => setCustomModel(event.target.value)}
                placeholder="模型名"
                className="h-10 w-full rounded-xl border border-line bg-surface px-2.5 text-[12.5px] text-ink"
              />
              <input
                type="password"
                value={customKey}
                onChange={(event) => setCustomKey(event.target.value)}
                placeholder="密钥（sk-...）"
                className="h-10 w-full rounded-xl border border-line bg-surface px-2.5 text-[12.5px] text-ink"
              />
              <div className="flex items-center gap-2">
                <input
                  value={customName}
                  onChange={(event) => setCustomName(event.target.value)}
                  placeholder="给它起个名（可留空）"
                  className="h-10 min-w-0 flex-1 rounded-xl border border-line bg-surface px-2.5 text-[12.5px] text-ink"
                />
                <label className="flex shrink-0 items-center gap-1 text-[12px] text-muted">
                  <input
                    type="checkbox"
                    className="accent-[var(--color-accent)]"
                    checked={customVision}
                    onChange={(event) => setCustomVision(event.target.checked)}
                  />
                  能看图
                </label>
              </div>
              <button
                type="button"
                disabled={busy}
                onClick={() => void saveCustomBase()}
                className="h-11 w-full rounded-xl text-[13.5px] text-white disabled:opacity-40"
                style={{ background: 'var(--color-accent)' }}
              >
                保存这条模型
              </button>
            </div>
          ) : (
            <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
              想留聪明的就用「跟随全局」；要换模型可以挑现成的，或者选「＋ 自定义」**在这里直接填**。
              选中的端点要是没配好，她会**报错**而不是偷偷换回全局。
            </p>
          )}
        </Section>

        <Section title="她的声音" hint="手机端用「设置 → 语音」里选的云端/系统声音。">
          <div className="mt-3 flex items-center justify-between gap-3">
            <span className="text-[13.5px] text-ink">她的回复都用语音</span>
            <button
              type="button"
              role="switch"
              aria-checked={friend?.voice_reply_enabled ?? false}
              disabled={busy}
              onClick={() => {
                const next = !(friend?.voice_reply_enabled ?? false)
                setBusy(true)
                void api
                  .updateFriend(friendId, { voice_reply_enabled: next })
                  .then(() => api.getFriend(friendId))
                  .then((detail) => setFriend(detail))
                  .catch((exc: unknown) => setHint(exc instanceof Error ? exc.message : String(exc)))
                  .finally(() => setBusy(false))
              }}
              className={`relative h-7 w-12 shrink-0 rounded-full transition-colors disabled:opacity-40 ${
                friend?.voice_reply_enabled ? 'bg-accent' : 'bg-line-strong'
              }`}
            >
              <span
                className={`absolute top-0.5 h-6 w-6 rounded-full bg-white shadow transition-transform ${
                  friend?.voice_reply_enabled ? 'translate-x-[22px]' : 'translate-x-0.5'
                }`}
              />
            </button>
          </div>
          <p className="mt-1 text-[11px] text-faint">
            没配云端时不发声会用 Android 系统语音；声音和电脑上的 GPT-SoVITS 不同。
          </p>
        </Section>

        <Section title="沉浸模式背景" hint="她坐在什么环境里（会裁成 16:9）">
          {friend?.background_url ? (
            <img
              src={friend.background_url}
              alt=""
              className="h-28 w-full rounded-xl border border-line/70 object-cover"
            />
          ) : (
            <p className="text-[12px] text-faint">还没有背景（用默认氛围底）</p>
          )}
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => backgroundRef.current?.click()}
              className="h-11 flex-1 rounded-xl border border-line bg-surface text-[13.5px] text-ink active:bg-surface-hover disabled:opacity-40"
            >
              换背景
            </button>
            {friend?.background_url && (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  void api.deleteBackground(friendId).then(() => {
                    setHint('背景删了')
                    return load()
                  })
                }}
                className="h-11 flex-1 rounded-xl border border-line bg-surface text-[13.5px] text-muted active:bg-surface-hover"
              >
                删掉背景
              </button>
            )}
          </div>
        </Section>

        <Section
          title={`她的立绘（${portraits.length} 个情绪）`}
          hint={
            globalPortraitPack
              ? '这是全局立绘包：上传/删除请在电脑端「设置 → 立绘包」里管理。'
              : '旧版朋友目录可以多选图片批量导入；全局立绘包请在电脑端设置里管理。'
          }
        >
          {portraits.length ? (
            <div className="flex flex-wrap gap-1.5">
              {portraits.map((item) => (
                <span
                  key={item.emotion}
                  className="rounded-full border border-line bg-surface px-2.5 py-1 text-[12px] text-muted"
                >
                  {item.emotion}
                </span>
              ))}
            </div>
          ) : (
            <p className="text-[12px] text-faint">她还没有立绘 —— 点下面的「批量导入立绘」。</p>
          )}
          {!globalPortraitPack && (
            <button
              type="button"
              disabled={batchBusy}
              onClick={() => batchRef.current?.click()}
              className="mt-3 h-11 w-full rounded-xl border border-line bg-surface text-[13.5px] text-ink active:bg-surface-hover disabled:opacity-40"
            >
              批量导入立绘（旧版目录）
            </button>
          )}
          {!globalPortraitPack && batchItems.length > 0 && (
            <div className="mt-3 space-y-2">
              {batchItems.map((item, index) => (
                <div key={`${item.file.name}-${index}`} className="flex items-center gap-2 rounded-xl border border-line bg-page p-2">
                  <img src={item.preview} alt="" className="h-12 w-12 rounded-lg object-cover" />
                  <input
                    value={item.emotion}
                    onChange={(event) => {
                      const value = event.target.value
                      setBatchItems((prev) =>
                        prev.map((row, rowIndex) => (rowIndex === index ? { ...row, emotion: value } : row)),
                      )
                    }}
                    className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-2 py-2 text-[13px] text-ink outline-none focus:border-accent"
                  />
                </div>
              ))}
              <button
                type="button"
                disabled={batchBusy}
                onClick={() => void uploadBatch()}
                className="h-11 w-full rounded-xl bg-accent text-[13.5px] text-white disabled:opacity-40"
              >
                {batchBusy ? '正在导入…' : `导入这 ${batchItems.length} 张`}
              </button>
            </div>
          )}
        </Section>
      </div>

      <input
        ref={batchRef}
        type="file"
        accept="image/*"
        multiple
        className="hidden"
        onChange={(event) => pickBatch(event.target.files)}
      />
      <input
        ref={avatarRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0]
          if (file) setCropping(file)
        }}
      />
      <input
        ref={backgroundRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(event) => void background(event.target.files?.[0])}
      />

      {cropping && (
        <ImageCropper
          file={cropping}
          onCancel={() => setCropping(null)}
          onConfirm={(cropped) => {
            setCropping(null)
            void avatar(cropped)
          }}
        />
      )}
    </div>
  )
}
