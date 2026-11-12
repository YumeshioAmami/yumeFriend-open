/** 改人格（A3）：朋友建好之后还能改 —— 人格是这产品的灵魂，写好锁死不合适。
 *
 * 只改这三个字段（api.md §1.2）：名字 / 人格 / 说话习惯。
 * 系统永不"修正"用户写的东西（宪法§10 / D11），这里也不做任何校验或补全。
 */

import { useEffect, useState } from 'react'

import {
  api,
  type FriendDetail,
  type Live2DModelSummary,
  type LlmBase,
  type PortraitPack,
  type Voice,
} from '../shared/api'
import Avatar from '../shared/Avatar'
import ImageCropper from '../shared/ImageCropper'
import StyleProfileEditor from '../shared/StyleProfileEditor'
import TemperatureField from '../shared/TemperatureField'
import { ACCENT } from '../shared/ui'

/** v3.9（D160）：「她的模型」下拉里"直接填一个"的那个选项值。 */
const CUSTOM_BASE = '__custom__'

const TEMPLATE = [
  '# 身份',
  '你是谁、多大、和用户什么关系',
  '',
  '# 性格',
  '脾气怎么样、在意什么、讨厌什么',
  '',
  '# 说话习惯',
  '句子长短、爱不爱用括号写动作、有没有口头禅',
  '',
  '# 你对用户的态度',
  '亲近还是疏远、会不会损他',
  '',
].join('\n')

export default function PersonalityModal({
  friend,
  onClose,
  onSaved,
}: {
  friend: FriendDetail
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(friend.name)
  const [personality, setPersonality] = useState(friend.personality)
  const [instructions, setInstructions] = useState(friend.custom_instructions)
  // v6.0（D298）：声音样本 —— 一行一段，学语气，不是句式模板
  const [voiceSamples, setVoiceSamples] = useState(
    (friend.voice_examples ?? []).join('\n'),
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [avatarUrl, setAvatarUrl] = useState<string | null>(friend.avatar_url)
  const [avatarBusy, setAvatarBusy] = useState(false)
  const [cropSource, setCropSource] = useState<File | null>(null)
  // v2（D84）：她的样子。生成的那段只是草稿，用户随便改。
  const [description, setDescription] = useState(friend.avatar_description ?? '')
  const [describeBusy, setDescribeBusy] = useState(false)
  const [describeHint, setDescribeHint] = useState<string | null>(null)
  // v2（T2.0-4）：给这个朋友配一个音色（不配 = 不发语音）
  const [voiceId, setVoiceId] = useState<string>(friend.voice_id ?? '')
  // v2（D85）：开了 → **每条回复都配语音**（不是"偶尔发一条"）
  const [voiceReply, setVoiceReply] = useState<boolean>(friend.voice_reply_enabled)
  const [voices, setVoices] = useState<Voice[]>([])
  // v7.43（D441）：呈现方式 + 全局模型 / 立绘包
  const [visualMode, setVisualMode] = useState<'live2d' | 'portrait'>(
    friend.visual_mode ?? 'live2d',
  )
  const [live2dModelId, setLive2dModelId] = useState(friend.live2d_model_id ?? '')
  const [portraitPackId, setPortraitPackId] = useState(friend.portrait_pack_id ?? '')
  const [live2dModels, setLive2dModels] = useState<Live2DModelSummary[]>([])
  const [portraitPacks, setPortraitPacks] = useState<PortraitPack[]>([])
  const [portraitHint, setPortraitHint] = useState<string | null>(null)
  const [backgroundUrl, setBackgroundUrl] = useState<string | null>(friend.background_url ?? null)
  // v3.5（D151）：这个朋友自己的"嗓子"（空 = 跟随全局「聊天」用途）
  const [llmBase, setLlmBase] = useState<string>(friend.llm_base ?? '')
  // v4.3（D249）：她的温度（null = 跟随默认）
  const [temperature, setTemperature] = useState<number | null>(friend.temperature ?? null)
  // v7.12（D341）：生活线只在电脑端推进；手机端只读
  const [lifeEnabled, setLifeEnabled] = useState<boolean>(friend.life_enabled ?? false)
  const [chatPacingEnabled, setChatPacingEnabled] = useState<boolean>(
    friend.chat_pacing_enabled ?? false,
  )
  const [humanizerEnabled, setHumanizerEnabled] = useState<boolean>(
    friend.humanizer_enabled ?? false,
  )
  const [bases, setBases] = useState<LlmBase[]>([])
  // v3.9（D160）：**人格页可以直接填端点**（服务地址 / 密钥 / 模型），
  // 不用先去「设置 → 模型」建预设 —— 保存时它会作为一条"模型预设"落库，
  // 所以下次别的朋友也能直接选中它（一个地方存，不重复塞 key）。
  const [customUrl, setCustomUrl] = useState('')
  const [customModel, setCustomModel] = useState('')
  const [customKey, setCustomKey] = useState('')
  const [customName, setCustomName] = useState('')
  const [customVision, setCustomVision] = useState(true)

  useEffect(() => {
    void api
      .listVoices()
      .then((result) => setVoices(result.voices))
      .catch(() => setVoices([])) // 音色库读不到也不影响改人格
    // 可选的模型端点（= 模型预设）；读不到就只显示「跟随全局」
    void api
      .getSettings()
      .then((row) => setBases(row.llm_bases ?? []))
      .catch(() => setBases([]))
    void api
      .listLive2DModels()
      .then(setLive2dModels)
      .catch(() => setLive2dModels([]))
    void api
      .listPortraitPacks()
      .then((packs) => {
        setPortraitPacks(packs)
        setPortraitPackId((current) => {
          if (current) return current
          return packs.find((pack) => pack.friend_id === friend.id)?.id ?? ''
        })
      })
      .catch(() => setPortraitPacks([]))
  }, [])

  async function pickBackground(file: File) {
    setPortraitHint(null)
    try {
      const updated = await api.uploadBackground(friend.id, file)
      setBackgroundUrl(updated.background_url ?? null)
      onSaved()
    } catch (e) {
      setPortraitHint(e instanceof Error ? e.message : String(e))
    }
  }

  async function dropBackground() {
    try {
      const updated = await api.deleteBackground(friend.id)
      setBackgroundUrl(updated.background_url ?? null)
      onSaved()
    } catch (e) {
      setPortraitHint(e instanceof Error ? e.message : String(e))
    }
  }

  async function regenerateDescription() {
    if (describeBusy) return
    setDescribeBusy(true)
    setDescribeHint(null)
    try {
      const result = await api.describeAvatar(friend.id)
      if (result.ok === false) {
        setDescribeHint(result.hint ?? '没能生成')
      } else {
        setDescription(result.avatar_description ?? '')
      }
    } catch (e) {
      setDescribeHint(e instanceof Error ? e.message : String(e))
    } finally {
      setDescribeBusy(false)
    }
  }

  async function pickAvatar(file: File) {
    if (avatarBusy) return
    setAvatarBusy(true)
    setError(null)
    try {
      const updated = await api.uploadAvatar(friend.id, file)
      setAvatarUrl(updated.avatar_url)
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setAvatarBusy(false)
    }
  }

  async function dropAvatar() {
    if (avatarBusy) return
    setAvatarBusy(true)
    setError(null)
    try {
      const updated = await api.deleteAvatar(friend.id)
      setAvatarUrl(updated.avatar_url)
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setAvatarBusy(false)
    }
  }

  async function save() {
    if (busy || !name.trim()) return
    setBusy(true)
    setError(null)
    try {
      let chosenBase = llmBase
      if (llmBase === CUSTOM_BASE) {
        // v3.9（D160）：人格页直接填的端点 → 先作为"模型预设"落库（同名则更新），再把朋友指过去
        if (!customUrl.trim() || !customModel.trim()) {
          setError('自定义模型还差一点：服务地址和模型名都要填')
          setBusy(false)
          return
        }
        const presetName =
          customName.trim() || `自定义-${customModel.trim().slice(0, 12)}`
        const entry: Record<string, unknown> = {
          name: presetName,
          base_url: customUrl.trim(),
          model: customModel.trim(),
          // v3.10（D161）：默认**直连优先**（跟用途卡一致），而不是"跟随系统代理"。
          // 以前写 '' = 永远走系统代理 —— 用户在开梯子的机器上，所有请求都被代理绕一圈。
          proxy: 'auto',
          max_retries: 1,
          capabilities: { vision: customVision },
          // 留空 = 不改已存的（同名更新时保护旧 key）
          ...(customKey.trim() ? { api_key: customKey.trim() } : {}),
        }
        const others = bases
          .filter((base) => base.name !== presetName)
          .map(({ api_key_set: _drop, ...rest }) => rest)
        await api.patchSettings({ llm_bases: [...others, entry] })
        chosenBase = presetName
        setLlmBase(presetName)
      }
      await api.updateFriend(friend.id, {
        name: name.trim(),
        personality,
        custom_instructions: instructions,
        voice_examples: voiceSamples
          .split('\n')
          .map((line) => line.trim())
          .filter(Boolean),
        avatar_description: description,
        voice_id: voiceId,
        // 没配音色就没有"用语音"这回事（后端也这么理解）
        voice_reply_enabled: voiceId ? voiceReply : false,
        // v3.5（D151）：空字符串 = 跟随全局「聊天」用途
        llm_base: chosenBase,
        // v4.3（D249）：null = 恢复默认
        temperature,
        life_enabled: lifeEnabled,
        chat_pacing_enabled: chatPacingEnabled,
        humanizer_enabled: humanizerEnabled,
        visual_mode: visualMode,
        live2d_model_id: live2dModelId || null,
        portrait_pack_id: portraitPackId || null,
      })
      onSaved()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim/40 p-3 backdrop-blur-sm">
      <div className="flex h-[86%] w-[640px] max-w-[96%] flex-col overflow-hidden rounded-2xl border border-line bg-page shadow-2xl">
        <div className="flex shrink-0 items-center justify-between border-b border-line/60 px-5 py-3">
          <h2 className="text-[15px] font-medium text-ink">编辑人格</h2>

          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-2 py-0.5 text-faint hover:bg-chip hover:text-muted"
          >
            ✕
          </button>
        </div>

        {/* ⚠️ 唯一的滚动区：**上面那几块（头像 / 样子 / 声音 / 名字）也放在里面** ——
            以前它们挂在滚动区外面，占掉 82% 高度里的大半，把人格输入框挤成一条 42px 的缝
            （2026-09-29 实测复现：clientHeight=42 / scrollHeight=422）。 */}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-3">
        {/* 头像（v2，T2.0-3）：没头像就是首字母色块，绝不空框 */}
        <div className="flex items-center gap-3 rounded-2xl border border-line/70 bg-surface/40 p-2.5">
          <Avatar name={name || friend.name} url={avatarUrl} size={48} />
          <div className="min-w-0 flex-1">
            <p className="text-[11.5px] text-ink">头像</p>
            <p className="mt-0.5 text-[10.5px] text-faint">
              随便什么图都行，会自动裁成方形（256×256）。没有头像就用名字首字。
            </p>
          </div>
          <label className="shrink-0 cursor-pointer rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent">
            {avatarBusy ? '处理中…' : '换一张'}
            <input
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0]
                e.target.value = '' // 选同一个文件也要能再触发
                if (file) setCropSource(file) // 先让用户自己选裁剪区域
              }}
            />
          </label>
          {avatarUrl && (
            <button
              type="button"
              onClick={() => void dropAvatar()}
              className="shrink-0 rounded-full px-2 py-0.5 text-[11px] text-faint transition-colors hover:text-red-500"
            >
              删掉
            </button>
          )}
        </div>
        {/* v2（D84）：她的样子——被注入 system prompt，所以她"知道自己长什么样" */}
        <div className="rounded-2xl border border-line/70 bg-surface/40 p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11.5px] text-ink">她的样子</span>
            <span className="text-[10.5px] text-faint">
              会告诉她"自己长什么样"（写动作、照镜子时才不会穿帮）
            </span>
            <button
              type="button"
              onClick={() => void regenerateDescription()}
              disabled={describeBusy || !avatarUrl}
              className="ml-auto shrink-0 rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
            >
              {describeBusy ? '生成中…' : '重新生成'}
            </button>
          </div>
          <textarea
            className="mt-1.5 h-[64px] w-full resize-none rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] leading-relaxed outline-none transition-colors focus:border-accent/60"
            placeholder={avatarUrl ? '（还没生成。点「重新生成」，或者自己写两句）' : '先传个头像，才能看图生成'}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          {describeHint && <p className="mt-1 text-[10.5px] text-faint">{describeHint}</p>}
        </div>

        {/* v2（T2.0-4）：她的声音。不配也能用——只是不会发语音 */}
        <div className="rounded-2xl border border-line/70 bg-surface/40 p-2.5">
          {/* v3.5（D151）：每个朋友可以有自己的模型端点（聪明的不设限的，各取所需） */}
          <div className="mb-2 flex flex-wrap items-center gap-2 border-b border-line/50 pb-2">
            <span className="text-[11.5px] text-ink">她的模型</span>
            <select
              value={llmBase}
              onChange={(e) => setLlmBase(e.target.value)}
              className="rounded-xl border border-line bg-surface/80 px-2 py-1 text-[12px] outline-none focus:border-accent/60"
            >
            <option value="">跟随全局（默认）</option>
            {bases.map((base) => (
              <option key={base.name} value={base.name}>
                {base.name}（{base.model || '未填模型'}）
              </option>
            ))}
            <option value={CUSTOM_BASE}>＋ 自定义（直接填地址 / 密钥 / 模型）</option>
          </select>
          <span className="text-[10.5px] leading-relaxed text-faint">
            每个朋友可以不一样：想留聪明的就用「跟随全局」；要换模型可以挑现成的，
            也可以直接选「＋ 自定义」在这里填（保存后会出现在「设置 → 模型」里，别的朋友也能选）。
            选中的端点若没配好，会**报错**而不是偷偷换回去。
          </span>
        </div>
        {llmBase === CUSTOM_BASE && (
          <div className="mb-2 rounded-xl border border-line/60 bg-page/40 p-2.5">
            <div className="flex flex-wrap items-center gap-1.5">
              <input
                value={customUrl}
                onChange={(e) => setCustomUrl(e.target.value)}
                placeholder="服务地址（https://.../v1）"
                className="min-w-[220px] flex-1 rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
              />
              <input
                value={customModel}
                onChange={(e) => setCustomModel(e.target.value)}
                placeholder="模型名"
                className="min-w-[170px] flex-1 rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
              />
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <input
                type="password"
                value={customKey}
                onChange={(e) => setCustomKey(e.target.value)}
                placeholder="密钥（sk-...）"
                className="min-w-[200px] flex-1 rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
              />
              <input
                value={customName}
                onChange={(e) => setCustomName(e.target.value)}
                placeholder="给它起个名（可留空）"
                className="w-[150px] rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
              />
              <label className="flex items-center gap-1 text-[11px] text-muted">
                <input
                  type="checkbox"
                  className="accent-[var(--color-accent)]"
                  checked={customVision}
                  onChange={(e) => setCustomVision(e.target.checked)}
                />
                能看图
              </label>
            </div>
          </div>
        )}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11.5px] text-ink">她的声音</span>
            <select
              value={voiceId}
              onChange={(e) => setVoiceId(e.target.value)}
              className="rounded-xl border border-line bg-surface/80 px-2 py-1 text-[12px] outline-none focus:border-accent/60"
            >
              <option value="">不配（纯文字）</option>
              {voices.map((voice) => (
                <option key={voice.id} value={voice.id}>
                  {voice.name}
                  {voice.files_ok ? '' : '（路径失效）'}
                </option>
              ))}
            </select>
            <span className="text-[10.5px] text-faint">
              {voices.length === 0
                ? '音色库还是空的——去「设置 → 语音」里扫描导入'
                : '音色在「设置 → 语音」里管理（试听、语速）'}
            </span>
          </div>
          {/* v2（D85）：这是"每句都配"的开关，不是"偶尔发一条" */}
          <label
            className={`mt-2 flex items-start gap-2 rounded-xl border px-2.5 py-1.5 transition-colors ${
              voiceId
                ? 'cursor-pointer border-line/70 hover:border-accent/50'
                : 'cursor-not-allowed border-line/50 opacity-55'
            }`}
          >
            <input
              type="checkbox"
              className="mt-[3px] accent-[var(--color-accent)]"
              checked={voiceId ? voiceReply : false}
              disabled={!voiceId}
              onChange={(e) => setVoiceReply(e.target.checked)}
            />
            <span className="min-w-0">
              <span className="block text-[11.5px] text-ink">她的回复都用语音</span>
              <span className="mt-0.5 block text-[10.5px] leading-relaxed text-faint">
                开了之后她<b className="text-muted">每一句</b>回复都会配上这个音色
                （合成要几秒，出来后在消息下面就能点开回放）；关了就是纯文字。
              </span>
            </span>
          </label>
        </div>

        <label className="block">
          <span className="mb-1 block text-[11px] text-muted">名字</span>
          <input
            className="w-[200px] rounded-xl border border-line bg-surface/80 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/60"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>

        <label className="flex flex-col">
          <span className="mb-1 flex items-baseline gap-2 text-[11px] text-muted">
            <span>人格（随便写，系统不会改它一个字）</span>
            <button
              type="button"
              onClick={() => setPersonality(TEMPLATE)}
              className="text-[11px] text-accent hover:underline"
            >
              用模板开头
            </button>
          </span>
          <textarea
            className="h-[150px] resize-y rounded-xl border border-line bg-surface/85 px-3 py-2 text-[13px] leading-relaxed outline-none focus:border-accent/60"
            placeholder="性格、说话方式、癖好、对用户的态度……"
            value={personality}
            onChange={(e) => setPersonality(e.target.value)}
          />
        </label>

        <label className="mt-2 block">
          <span className="mb-1 block text-[11px] text-muted">说话习惯（补充规则，可留空）</span>
          <input
            className="w-full rounded-xl border border-line bg-surface/80 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/60"
            placeholder="比如：别用颜文字 / 每句话不超过 20 字"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        </label>

        <label className="mt-2 block">
          <span className="mb-1 block text-[11px] text-muted">
            声音样本（一行一段；学语气、句长、用词，不是套句式）
          </span>
          <textarea
            className="h-[96px] w-full resize-y rounded-xl border border-line bg-surface/80 px-3 py-2 text-[12.5px] leading-relaxed outline-none focus:border-accent/60"
            placeholder={'行，那不排了。你这种人就别上闹钟。\n我没死。你别把话收得那么快。'}
            value={voiceSamples}
            onChange={(e) => setVoiceSamples(e.target.value)}
          />
        </label>

        <StyleProfileEditor friendId={friend.id} onChanged={onSaved} />

        {/* v4.3（D249）：她的温度 —— 越高越跳（Rick 那种可以调高点） */}
        <TemperatureField value={temperature} onChange={setTemperature} disabled={busy} />

        <label className="flex items-start gap-3 rounded-2xl border border-line/70 bg-surface/40 p-3">
          <input
            type="checkbox"
            checked={lifeEnabled}
            onChange={(e) => setLifeEnabled(e.target.checked)}
            disabled={busy}
            className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
          />
          <span className="min-w-0">
            <span className="block text-[11.5px] text-ink">生活线（仅电脑端）</span>
            <span className="mt-0.5 block text-[10.5px] leading-relaxed text-muted">
              打开后，她会在电脑上做真实的小事（读网页 / 看 B 站热门），并把真实看到的记下来。
              手机端只聊天、看状态，不推进生活。
            </span>
          </span>
        </label>

        <div className="grid gap-2 rounded-2xl border border-line/70 bg-surface/40 p-3 sm:grid-cols-2">
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={chatPacingEnabled}
              onChange={(e) => setChatPacingEnabled(e.target.checked)}
              disabled={busy}
              className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
            />
            <span>
              <span className="block text-[11.5px] text-ink">轻聊/深聊分流</span>
              <span className="mt-0.5 block text-[10.5px] leading-relaxed text-muted">
                日常默认 1~3 个短气泡；明显倒心事时才展开。
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={humanizerEnabled}
              onChange={(e) => setHumanizerEnabled(e.target.checked)}
              disabled={busy}
              className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
            />
            <span>
              <span className="block text-[11.5px] text-ink">表达学习 + 偶发手滑</span>
              <span className="mt-0.5 block text-[10.5px] leading-relaxed text-muted">
                学他最近的短说法；轻聊时偶尔一个同音小错字。
              </span>
            </span>
          </label>
        </div>

        {/* v7.43（D441）：呈现方式二选一；立绘上传移到了全局设置 */}
        <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11.5px] text-ink">呈现方式（二选一）</span>
            <span className="text-[10.5px] text-faint">
              Live2D 会动；立绘包是静态图。上传/管理都在「设置」里。
            </span>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => setVisualMode('live2d')}
              className={`rounded-xl border px-3 py-2 text-left transition-colors ${
                visualMode === 'live2d'
                  ? 'border-accent bg-accent/10'
                  : 'border-line/70 bg-page/40 hover:border-accent/40'
              }`}
            >
              <span className="block text-[11.5px] font-medium text-ink">Live2D 模型</span>
              <span className="mt-0.5 block text-[10px] text-faint">人格会动、有表情和口型</span>
            </button>
            <button
              type="button"
              onClick={() => setVisualMode('portrait')}
              className={`rounded-xl border px-3 py-2 text-left transition-colors ${
                visualMode === 'portrait'
                  ? 'border-accent bg-accent/10'
                  : 'border-line/70 bg-page/40 hover:border-accent/40'
              }`}
            >
              <span className="block text-[11.5px] font-medium text-ink">立绘包</span>
              <span className="mt-0.5 block text-[10px] text-faint">静态立绘，按情绪换图</span>
            </button>
          </div>

          {visualMode === 'live2d' ? (
            <div className="mt-2">
              <label className="block text-[10.5px] text-faint">使用的 Live2D 模型</label>
              <select
                value={live2dModelId}
                onChange={(e) => setLive2dModelId(e.target.value)}
                className="mt-1 w-full rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
              >
                <option value="">旧版：按朋友目录 / 默认模型</option>
                {live2dModels.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name}
                    {model.used_by.length > 0
                      ? `（${model.used_by.map((item) => item.name).join('、')}）`
                      : ''}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[10px] text-faint">
                模型的参数/标签在「设置 → Live2D」里调；这里只选用哪一个。
              </p>
            </div>
          ) : (
            <div className="mt-2">
              <label className="block text-[10.5px] text-faint">使用的立绘包</label>
              <select
                value={portraitPackId}
                onChange={(e) => setPortraitPackId(e.target.value)}
                className="mt-1 w-full rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
              >
                <option value="">旧版：按朋友自己的立绘目录</option>
                {portraitPacks.map((pack) => (
                  <option key={pack.id} value={pack.id}>
                    {pack.name}（{pack.emotions} 个情绪）
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[10px] text-faint">
                立绘包的上传、扫描和删除在「设置 → 立绘包」里完成。
              </p>
            </div>
          )}
          {portraitHint && <p className="mt-1 text-[10.5px] text-faint">{portraitHint}</p>}
        </div>

        {/* v2.2（D107）：沉浸模式的背景图（服务端自动按 16:9 裁） */}
        <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
          <div className="flex items-center gap-2">
            <span className="text-[11.5px] text-ink">背景（沉浸模式用）</span>
            <span className="text-[10.5px] text-faint">
              {backgroundUrl ? '已设置 · 上传会覆盖' : '没设就用默认暖色底'}
            </span>
            {backgroundUrl && (
              <button
                type="button"
                onClick={() => void dropBackground()}
                className="ml-auto rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted hover:border-accent/60 hover:text-accent"
              >
                删掉
              </button>
            )}
          </div>
          <div className="mt-2 flex items-center gap-3">
            <label className="cursor-pointer rounded-full border border-line-strong px-3 py-0.5 text-[11px] text-muted hover:border-accent/60 hover:text-accent">
              选一张图…
              <input
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  e.target.value = ''
                  if (file) void pickBackground(file)
                }}
              />
            </label>
            {backgroundUrl && (
              <img
                src={backgroundUrl}
                alt="背景预览"
                className="h-[54px] w-[96px] rounded-lg border border-line/60 object-cover"
              />
            )}
            <span className="text-[10.5px] leading-relaxed text-faint">
              会自动按 16:9 居中裁切（横构图最好看）
            </span>
          </div>
        </div>
        </div>

        <div className="shrink-0 border-t border-line/60 px-5 py-3">
        {error && <p className="mb-2 text-[11.5px] text-red-600">出错了：{error}</p>}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl border border-line-strong px-3 py-1.5 text-[12.5px] text-muted hover:border-stone-400"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => void save()}
            disabled={busy || !name.trim()}
            className="rounded-xl px-4 py-1.5 text-[12.5px] font-medium text-white disabled:opacity-40"
            style={{ background: ACCENT }}
          >
            保存
          </button>
        </div>
        </div>
      </div>
    </div>
      {/* 裁剪：让用户自己选区域（拖动 + 缩放），而不是系统居中拍脑袋 */}
      {cropSource && (
        <ImageCropper
          file={cropSource}
          onCancel={() => setCropSource(null)}
          onConfirm={(cropped) => {
            setCropSource(null)
            void pickAvatar(cropped)
          }}
        />
      )}
    </>
  )
}
