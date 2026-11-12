/**
 * 设置（api.md §5 / D80）：**分页 + 按用途配模型**。
 *
 * 几条别破坏的约定：
 * · **只有「聊天」是必须的**（D83）：其它用途没配 = **没这个能力**，
 *   显示成「未启用 · 可选」，**不许**写成红色警告；相关入口由 `purposes_enabled` 决定给不给。
 * · **api_key 明文不回前端**（api.md §5.1）：留空 = 不改。
 * · 界面缩放是**本机偏好**（localStorage）：点一下立刻生效，不用按保存。
 * · 尺寸**只用百分比，别用 vh/vw**——zoom 会把 vh/vw 一起乘掉（D80 第 1 条）。
 * · 视觉语言沿用 D66/D69：暖色、单一强调色（伴侣橙）、自绘 SVG、制动效。
 */

import { useEffect, useState, type ReactElement } from 'react'

import {
  api,
  type EmbeddingDownloadState,
  type PurposeConfig,
  type Settings,
} from '../shared/api'
import {
  IconBell,
  IconChat,
  IconChip,
  IconDatabase,
  IconEye,
  IconGamepad,
  IconGear,
  IconImage,
  IconInfo,
  IconMic,
  IconRefresh,
  IconSpark,
  IconStack,
  IconSun,
  IconWave,
} from '../shared/icons'
import MobilePanel from './MobilePanel'
import Live2DPanel from './Live2DPanel'
import PortraitPacksPanel from './PortraitPacksPanel'
import SyncPanel from './SyncPanel'
import { UI_SCALE_CHOICES } from './scale'
import VoicePanel from './VoicePanel'
import FeatureCenter from './FeatureCenter'
import { EmbeddingAdvancedPanel, EmbeddingToggleCard } from './EmbeddingSettings'
import { UpdatePanel } from './UpdatePanel'
import LegalDocs from './LegalDocs'

export type TabId =
  | 'appearance'
  | 'models'
  | 'embedding'
  | 'voice'
  | 'live2d'
  | 'portraits'
  | 'behavior'
  | 'features'
  | 'memory'
  | 'sync'
  | 'mobile'
  | 'update'
  | 'about'

/**
 * v3.5（D151）：**模型预设**的编辑草稿。
 *
 * 一个预设 = 一个命名端点（服务地址 / 密钥 / 模型 / 能力），可以被**任意朋友**挑去当"嗓子"。
 * `api_key` 留空 = 不改（沿用已存的），跟 `purposes` 的规矩一致（api.md §5.1）。
 */
interface PresetDraft {
  name: string
  base_url: string
  model: string
  protocol: 'openai' | 'anthropic' | 'gemini'
  api_key: string
  api_key_set?: boolean
  /** 这个端点能不能看图（决定要不要把图 inline 给它） */
  vision: boolean
  /** 支不支持思考链（决定 deep_thinking 开关对它有没有用） */
  reasoning: boolean
  /** v3.10（D161）：网络怎么走 —— 'auto' 直连优先（默认）/ '' 跟随系统 / 'none' 只直连 */
  proxy: string
}

interface IconProps {
  size?: number
  className?: string
}

const TABS: { id: TabId; label: string; Icon: (p: IconProps) => ReactElement }[] = [
  { id: 'appearance', label: '外观', Icon: IconSun },
  { id: 'models', label: '模型', Icon: IconChip },
  { id: 'embedding', label: '语义检索', Icon: IconSpark },
  { id: 'voice', label: '语音', Icon: IconWave },
  { id: 'live2d', label: 'Live2D', Icon: IconImage },
  { id: 'portraits', label: '立绘包', Icon: IconImage },
  { id: 'behavior', label: '行为', Icon: IconGear },
  { id: 'features', label: '功能', Icon: IconGamepad },
  { id: 'memory', label: '记忆', Icon: IconStack },
  { id: 'sync', label: '同步', Icon: IconDatabase },
  { id: 'mobile', label: '手机端', Icon: IconBell },
  { id: 'update', label: '更新', Icon: IconRefresh },
  { id: 'about', label: '关于', Icon: IconInfo },
]

interface PurposeMeta {
  id: string
  label: string
  hint: string
  Icon: (p: IconProps) => ReactElement
  kind: 'llm' | 'tts'
  /** 这个用途的配置在哪个页签里（v2.1：语音相关的都挪到「语音」页，别让人两页跑） */
  page: 'models' | 'voice'
  /** 唯一必须的那个（聊天）。 */
  required?: boolean
  /** 支持"跟随聊天"（视觉）。 */
  follows?: boolean
  /** 还没做的用途，标一下属于哪一批（别让用户以为坏了）。 */
  soon?: string
}

const PURPOSES: PurposeMeta[] = [
  {
    id: 'chat',
    label: '聊天',
    hint: 'OpenAI 兼容 / Anthropic / Gemini 原生协议都行——这是唯一必须配的',
    Icon: IconChat,
    kind: 'llm',
    page: 'models',
    required: true,
  },
  {
    id: 'vision',
    label: '视觉',
    hint: '看图用的模型；可以「跟随聊天」（现在的聊天模型本身就能看图）',
    Icon: IconEye,
    kind: 'llm',
    page: 'models',
    follows: true,
  },
  {
    id: 'tts',
    label: '语音合成',
    hint: '让朋友会说话：本地 GPT-SoVITS 或云端服务',
    Icon: IconWave,
    kind: 'tts',
    page: 'voice',
  },
  // 语音识别（T2.1-2）是**本地 Paraformer**、不用配 key —— 所以这里没有它的卡片，
  // 说明和启停都在「语音」页（见下面的「本地语音服务」卡片）
  { id: 'image', label: '图片生成', hint: '让它画图（带参考图就是图生图）', Icon: IconImage, kind: 'llm', page: 'models' },
]

/**
 * 网络走法（D52 / D58）：**这三个按钮必须留着**。
 *
 * 直连最快；梯子开着反而会拖累直连（实测：关梯子 8/8 成功 vs 开梯子 3/6），
 * 所以"自动"是直连优先、不通才换梯子，并且会**记住哪条路好用**。
 * 空字符串 = 跟随系统 / 环境变量（老默认；清空那个输入框就能回到这个状态）。
 */
const PROXY_CHOICES: { label: string; value: string; hint: string; needsUrl?: boolean }[] = [
  { label: '直连', value: 'none', hint: '不走梯子，最快（梯子关着时用这个）' },
  { label: '自动', value: 'auto', hint: '直连优先，不通自动换梯子（会记住哪条好用）' },
  { label: '走梯子', value: '', hint: '强制走梯子（端口按你自己的梯子改）', needsUrl: true },
]

const DEFAULT_PROXY_URL = 'http://127.0.0.1:8080'

function Segmented<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
}) {
  return (
    <div className="flex items-center gap-0.5 rounded-full border border-line/70 bg-surface/60 p-0.5">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={`rounded-full px-2.5 py-0.5 text-[11px] transition-colors duration-200 ${
            value === opt.value
              ? 'bg-accent/10 text-accent ring-1 ring-accent/25'
              : 'text-faint hover:text-muted'
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

function Field({
  label,
  value,
  placeholder,
  type = 'text',
  onChange,
}: {
  label: string
  value: string
  placeholder?: string
  type?: string
  onChange: (v: string) => void
}) {
  return (
    <label className="flex items-center gap-2">
      <span className="w-[68px] shrink-0 text-right text-[11px] text-faint">{label}</span>
      <input
        type={type}
        className="min-w-0 flex-1 rounded-lg border border-line bg-surface/80 px-2 py-1 text-[11.5px] outline-none transition-colors focus:border-accent/60"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )
}

interface TestState {
  state: 'idle' | 'busy' | 'ok' | 'fail'
  text?: string
}

function PurposeCard({
  meta,
  config,
  enabled,
  onPatch,
  onTest,
  test,
}: {
  meta: PurposeMeta
  config: PurposeConfig
  enabled: boolean
  onPatch: (patch: PurposeConfig) => void
  onTest: () => void
  test?: TestState
}) {
  const { Icon } = meta
  const following = Boolean(meta.follows && config.follow_chat)
  const showFields = !following && !meta.soon

  const dotColor = test?.state === 'fail' ? 'bg-red-400' : enabled ? 'bg-accent' : 'bg-faint'
  const statusText = test?.state === 'fail' ? '连不上' : enabled ? '已启用' : '未启用 · 可选'

  return (
    <div className="group rounded-2xl border border-line/70 bg-surface/40 p-3 transition-all duration-200 hover:-translate-y-px hover:border-accent/40 hover:bg-surface/70 hover:shadow-[0_4px_16px_rgba(15,23,42,0.06)]">
      <div className="flex items-start gap-2.5">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-chip/60 text-muted transition-colors duration-200 group-hover:text-accent">
          <Icon size={16} />
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[12.5px] font-medium text-ink">{meta.label}</span>
            {meta.required && (
              <span className="rounded-full bg-accent/10 px-1.5 text-[10px] text-accent">必须</span>
            )}
            {meta.soon && (
              <span className="rounded-full bg-chip px-1.5 text-[10px] text-faint">{meta.soon} 才做</span>
            )}
            <span className="ml-auto flex items-center gap-1.5 text-[10.5px] text-faint">
              <span className={`h-1.5 w-1.5 rounded-full ${dotColor}`} aria-hidden />
              {statusText}
            </span>
          </div>
          <p className="mt-0.5 text-[11px] leading-snug text-faint">{meta.hint}</p>

          {meta.soon ? (
            <p className="mt-2 text-[11px] text-faint">
              这项还没做，先在这儿占个位置——不影响聊天。
            </p>
          ) : (
            <div className="mt-2 space-y-1.5">
              <div className="flex flex-wrap items-center gap-1.5">
                {meta.follows && (
                  <Segmented
                    value={following ? 'follow' : 'own'}
                    options={[
                      { value: 'follow', label: '跟随聊天' },
                      { value: 'own', label: '单独配置' },
                    ]}
                    onChange={(v) => onPatch({ follow_chat: v === 'follow' })}
                  />
                )}
                {!following && meta.kind === 'llm' && meta.id !== 'image' && (
                  <label className="flex items-center gap-1 text-[10.5px] text-faint">
                    协议
                    <select
                      value={config.protocol ?? 'openai'}
                      onChange={(e) =>
                        onPatch({
                          protocol: e.target.value as 'openai' | 'anthropic' | 'gemini',
                        })
                      }
                      className="rounded-lg border border-line bg-surface/80 px-1.5 py-0.5 text-[10.5px] text-ink outline-none focus:border-accent/60"
                    >
                      <option value="openai">OpenAI 兼容</option>
                      <option value="anthropic">Anthropic</option>
                      <option value="gemini">Gemini</option>
                    </select>
                  </label>
                )}
                {!following && meta.kind === 'tts' && (
                  <Segmented
                    value={config.engine === 'cloud' ? 'cloud' : 'gpt-sovits'}
                    options={[
                      { value: 'gpt-sovits', label: '本地 GPT-SoVITS' },
                      { value: 'cloud', label: '云端' },
                    ]}
                    onChange={(v) => onPatch({ engine: v })}
                  />
                )}
              </div>

              {showFields && meta.kind === 'llm' && (
                <>
                  <Field
                    label="服务地址"
                    value={config.base_url ?? ''}
                    placeholder={
                      config.protocol === 'anthropic'
                        ? 'https://api.anthropic.com/v1'
                        : config.protocol === 'gemini'
                          ? 'https://generativelanguage.googleapis.com/v1beta'
                          : 'https://.../v1'
                    }
                    onChange={(v) => onPatch({ base_url: v })}
                  />
                  <Field
                    label="密钥"
                    type="password"
                    value={config.api_key ?? ''}
                    placeholder={config.api_key_set ? '已保存 · 留空不改' : 'sk-...'}
                    onChange={(v) => onPatch({ api_key: v })}
                  />
                  <Field
                    label="模型名"
                    value={config.model ?? ''}
                    placeholder={
                      config.protocol === 'anthropic'
                        ? 'claude-sonnet-4-5'
                        : config.protocol === 'gemini'
                          ? 'gemini-2.5-pro'
                          : 'qwen/...'
                    }
                    onChange={(v) => onPatch({ model: v })}
                  />
                  {/* D129 图生图：强度 = "像参考图多少"（只对图片生成这一项有意义） */}
                  {meta.id === 'image' && (
                    <Field
                      label="参考图强度"
                      value={config.strength === undefined || config.strength === null ? '' : String(config.strength)}
                      placeholder="0.7（0.5 很贴参考图，0.85 很自由）"
                      onChange={(v) => {
                        const trimmed = v.trim()
                        onPatch({ strength: trimmed ? Number(trimmed) : undefined })
                      }}
                    />
                  )}
                  {/* 网络走法（D52 / D58）：这不是装饰，是"直连不通 / 梯子拖慢"的开关 */}
                  <div className="pt-0.5">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="w-[68px] shrink-0 text-right text-[11px] text-faint">
                        网络走法
                      </span>
                      {PROXY_CHOICES.map((choice) => {
                        const current = config.proxy ?? ''
                        const isActive = choice.needsUrl
                          ? Boolean(current) && current !== 'none' && current !== 'auto'
                          : current === choice.value
                        return (
                          <button
                            key={choice.label}
                            type="button"
                            title={choice.hint}
                            onClick={() =>
                              onPatch({
                                proxy: choice.needsUrl
                                  ? isActive
                                    ? current
                                    : DEFAULT_PROXY_URL
                                  : choice.value,
                              })
                            }
                            className={`rounded-full border px-2.5 py-0.5 text-[11px] transition-colors ${
                              isActive
                                ? 'border-accent text-accent'
                                : 'border-line-strong text-muted hover:border-accent/50 hover:text-accent'
                            }`}
                          >
                            {choice.label}
                          </button>
                        )
                      })}
                      <span className="text-[10.5px] text-faint">
                        {(() => {
                          const current = config.proxy ?? ''
                          if (current === 'none') return PROXY_CHOICES[0].hint
                          if (current === 'auto') return PROXY_CHOICES[1].hint
                          if (current) return PROXY_CHOICES[2].hint
                          return '跟随系统 / 环境变量（清空下面这行就是它）'
                        })()}
                      </span>
                    </div>
                    <div className="mt-1 flex items-center gap-2">
                      <span className="w-[68px] shrink-0" />
                      <input
                        className="min-w-0 flex-1 rounded-lg border border-line bg-surface/80 px-2 py-1 text-[11.5px] outline-none transition-colors focus:border-accent/60"
                        placeholder="none / auto / http://127.0.0.1:8080"
                        value={config.proxy ?? ''}
                        onChange={(e) => onPatch({ proxy: e.target.value })}
                      />
                    </div>
                  </div>
                </>
              )}

                {showFields && meta.kind === 'tts' && (
                <>
                  <Field
                    label="服务地址"
                    value={config.base_url ?? ''}
                    placeholder="http://127.0.0.1:9880"
                    onChange={(v) => onPatch({ base_url: v })}
                  />
                  {config.engine !== 'cloud' && (
                    <p className="text-[10.5px] leading-relaxed text-faint">
                      GPT-SoVITS 的「程序目录」在下面「音色库」里填（扫描导入也要用它）——
                      这里就不重复放一个输入框了。
                    </p>
                  )}
                </>
              )}

              {meta.id !== 'image' && (
                <div className="flex items-center gap-2 pt-0.5">
                <button
                  type="button"
                  onClick={onTest}
                  disabled={test?.state === 'busy'}
                  className="rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
                >
                  {test?.state === 'busy' ? '测试中…' : '测试连接'}
                </button>
                {test?.text && (
                  <span
                    className={`text-[10.5px] ${
                      test.state === 'ok'
                        ? 'text-accent'
                        : test.state === 'fail'
                          ? 'text-red-500'
                          : 'text-faint'
                    }`}
                  >
                    {test.text}
                  </span>
                )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** 普通设置里的「聊天模型」卡片：只留连接模型必须的字段，不展示代理等底层项。 */
function SimpleChatCard({
  config,
  onPatch,
  onTest,
  test,
}: {
  config: PurposeConfig
  onPatch: (patch: PurposeConfig) => void
  onTest: () => void
  test?: TestState
}) {
  return (
    <div className="rounded-2xl border border-line/70 bg-surface/70 p-4">
      <div className="flex items-start gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent/10 text-accent">
          <IconChat size={17} />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[13px] font-medium text-ink">聊天模型</h3>
          <p className="mt-1 text-[11.5px] leading-relaxed text-faint">
            她平时说话用的模型。OpenAI 兼容 / Anthropic / Gemini 都行，填服务地址、密钥和模型名即可。
          </p>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <select
          value={config.protocol ?? 'openai'}
          onChange={(event) =>
            onPatch({
              protocol: event.target.value as 'openai' | 'anthropic' | 'gemini',
            })
          }
          className="rounded-full border border-line-strong bg-surface/80 px-2.5 py-1 text-[10.5px] text-muted outline-none focus:border-accent/60"
        >
          <option value="openai">OpenAI 兼容</option>
          <option value="anthropic">Anthropic</option>
          <option value="gemini">Gemini</option>
        </select>
      </div>

      <div className="mt-2 space-y-1.5">
        <Field
          label="服务地址"
          value={config.base_url ?? ''}
          placeholder={
            config.protocol === 'anthropic'
              ? 'https://api.anthropic.com/v1'
              : config.protocol === 'gemini'
                ? 'https://generativelanguage.googleapis.com/v1beta'
                : 'https://.../v1'
          }
          onChange={(value) => onPatch({ base_url: value })}
        />
        <Field
          label="密钥"
          type="password"
          value={config.api_key ?? ''}
          placeholder={config.api_key_set ? '已保存 · 留空不改' : 'sk-...'}
          onChange={(value) => onPatch({ api_key: value })}
        />
        <Field
          label="模型名"
          value={config.model ?? ''}
          placeholder={
            config.protocol === 'anthropic'
              ? 'claude-sonnet-4-5'
              : config.protocol === 'gemini'
                ? 'gemini-2.5-pro'
                : 'deepseek-chat / qwen...'
          }
          onChange={(value) => onPatch({ model: value })}
        />
      </div>

      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          onClick={onTest}
          disabled={test?.state === 'busy'}
          className="rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
        >
          {test?.state === 'busy' ? '测试中…' : '测试连接'}
        </button>
        {test?.text && (
          <span className={`text-[10.5px] ${test.state === 'fail' ? 'text-red-500' : 'text-muted'}`}>
            {test.text}
          </span>
        )}
        <span className="ml-auto text-[10.5px] text-faint">代理 / 更多端点 → 高级设置</span>
      </div>
    </div>
  )
}

/** 普通设置：商业软件式的懒人页。 */
function SimpleSettingsView({
  uiScale,
  onScaleChange,
  chatConfig,
  onChatPatch,
  onChatTest,
  chatTest,
  embeddingConfig,
  onEmbeddingPatch,
  onEmbeddingDownloaded,
  autoStart,
  onAutoStartChange,
  warmupEnabled,
  onWarmupChange,
  frequency,
  onFrequencyChange,
  budget,
  onBudgetChange,
  onOpenAdvancedTab,
  onOpenAdvanced,
}: {
  uiScale: number
  onScaleChange: (scale: number) => void
  chatConfig: PurposeConfig
  onChatPatch: (patch: PurposeConfig) => void
  onChatTest: () => void
  chatTest?: TestState
  embeddingConfig: PurposeConfig
  onEmbeddingPatch: (patch: PurposeConfig) => void
  onEmbeddingDownloaded: (info: EmbeddingDownloadState) => void
  autoStart: boolean
  onAutoStartChange: (value: boolean) => void
  warmupEnabled: boolean
  onWarmupChange: (value: boolean) => void
  frequency: number
  onFrequencyChange: (value: number) => void
  budget: number
  onBudgetChange: (value: number) => void
  onOpenAdvancedTab: (tab: TabId) => void
  onOpenAdvanced: () => void
}) {
  return (
    <div className="mx-auto max-w-[620px] space-y-3">
      <div className="rounded-2xl border border-accent/20 bg-accent/5 px-4 py-3">
        <h3 className="text-[13px] font-medium text-ink">常用设置</h3>
        <p className="mt-1 text-[11.5px] leading-relaxed text-faint">
          这里只放日常会用到的。需要代理、用途拆分、Live2D 参数、同步细节这些，切到「高级设置」。
        </p>
      </div>

      <SimpleChatCard
        config={chatConfig}
        onPatch={onChatPatch}
        onTest={onChatTest}
        test={chatTest}
      />

      <EmbeddingToggleCard
        config={embeddingConfig}
        onPatch={onEmbeddingPatch}
        onDownloaded={onEmbeddingDownloaded}
      />

      <div className="rounded-2xl border border-line/70 bg-surface/70 p-4">
        <h3 className="text-[13px] font-medium text-ink">外观</h3>
        <p className="mt-1 text-[11.5px] text-faint">只影响这台电脑的显示，点一下立刻生效。</p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {UI_SCALE_CHOICES.map((choice) => (
            <button
              key={choice.value}
              type="button"
              onClick={() => onScaleChange(choice.value)}
              className={`rounded-full border px-3 py-1 text-[11.5px] transition-colors ${
                Math.abs(uiScale - choice.value) < 0.001
                  ? 'border-accent text-accent'
                  : 'border-line-strong text-muted hover:border-accent/50 hover:text-accent'
              }`}
            >
              {choice.label}
            </button>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {[
            ['live2d', 'Live2D 模型'],
            ['portraits', '立绘包'],
            ['voice', '语音'],
            ['sync', '同步'],
            ['update', '软件更新'],
          ].map(([id, label]) => (
            <button
              key={id}
              type="button"
              onClick={() => onOpenAdvancedTab(id as TabId)}
              className="rounded-xl border border-line bg-surface px-3 py-1.5 text-[11.5px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
            >
              {label} →
            </button>
          ))}
        </div>
      </div>

      <div className="rounded-2xl border border-line/70 bg-surface/70 p-4">
        <h3 className="text-[13px] font-medium text-ink">日常</h3>
        <div className="mt-2 space-y-2">
          <label className="flex items-center justify-between gap-3 text-[12px] text-muted">
            <span>
              开机自启
              <span className="mt-0.5 block text-[10.5px] text-faint">开机后自己起来，手机才连得上</span>
            </span>
            <input
              type="checkbox"
              checked={autoStart}
              onChange={(event) => onAutoStartChange(event.target.checked)}
            />
          </label>
          <label className="flex items-center justify-between gap-3 text-[12px] text-muted">
            <span>
              定时预热
              <span className="mt-0.5 block text-[10.5px] text-faint">隔一会儿发个不花 token 的空请求</span>
            </span>
            <input
              type="checkbox"
              checked={warmupEnabled}
              onChange={(event) => onWarmupChange(event.target.checked)}
            />
          </label>
          <label className="flex items-center justify-between gap-3 text-[12px] text-muted">
            <span>主动消息频率（分钟）</span>
            <input
              type="number"
              min={1}
              max={1440}
              value={frequency}
              onChange={(event) => onFrequencyChange(Number(event.target.value))}
              className="w-[90px] rounded-xl border border-line bg-surface/80 px-2.5 py-1 text-[12px] outline-none focus:border-accent/60"
            />
          </label>
          <label className="flex items-center justify-between gap-3 text-[12px] text-muted">
            <span>记忆注入预算（token）</span>
            <input
              type="number"
              min={500}
              max={100000}
              step={500}
              value={budget}
              onChange={(event) => onBudgetChange(Number(event.target.value))}
              className="w-[110px] rounded-xl border border-line bg-surface/80 px-2.5 py-1 text-[12px] outline-none focus:border-accent/60"
            />
          </label>
        </div>
      </div>

      <UpdatePanel />

      <button
        type="button"
        onClick={onOpenAdvanced}
        className="w-full rounded-2xl border border-dashed border-line-strong px-3 py-2.5 text-[12px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
      >
        需要更细的控制？进入高级设置 →
      </button>
    </div>
  )
}

export default function SettingsModal({
  settings,
  uiScale,
  onScaleChange,
  onOpenGameMode,
  initialTab,
  onOpenTutorial,
  onClose,
  onSaved,
}: {
  settings: Settings
  uiScale: number
  onScaleChange: (scale: number) => void
  /** v5.1（D287）：游戏设置已经搬到全屏游戏模式，这里只留入口。 */
  onOpenGameMode?: () => void
  /** D458：从教程/快捷入口打开时直接定位到指定页。 */
  initialTab?: TabId
  onOpenTutorial?: (tab: TabId) => void
  onClose: () => void
  onSaved: () => void
}) {
  const [mode, setMode] = useState<'simple' | 'advanced'>(() =>
    initialTab || localStorage.getItem('yf.settings.mode') === 'advanced'
      ? 'advanced'
      : 'simple',
  )
  const [tab, setTab] = useState<TabId>(initialTab ?? 'models')
  const [purposes, setPurposes] = useState<Record<string, PurposeConfig>>(() => ({
    ...(settings.purposes ?? {}),
  }))
  const [frequency, setFrequency] = useState(settings.active_message_frequency)
  const [budget, setBudget] = useState(settings.memory_token_budget ?? 3000)
  const [deepThinking, setDeepThinking] = useState(settings.deep_thinking)
  const [showToolCalls, setShowToolCalls] = useState(settings.show_tool_calls)
  // v2（D87）：定时预热
  const [warmupEnabled, setWarmupEnabled] = useState(settings.warmup_enabled ?? true)
  const [warmupMinutes, setWarmupMinutes] = useState(settings.warmup_interval_minutes ?? 5)
  // v2.1（D99）：文件改动要不要逐笔点头
  const [fileWriteApproval, setFileWriteApproval] = useState(settings.file_write_approval ?? true)
  // v2.1（D100）+ v2.2（D103）：看屏（= 她主动找你说话之前先瞟一眼屏幕）
  const [screenWatch, setScreenWatch] = useState(settings.screen_watch_enabled ?? false)
  // v3.15（2026-09-26）：开机自启（做成设置项，默认关 —— 不替用户塞自启）
  const [autoStart, setAutoStart] = useState(settings.auto_start ?? false)
  // v3.0（D141）：消息语音保留多少天（0 = 永久留着）
  const [voiceKeepDays, setVoiceKeepDays] = useState(settings.voice_keep_days ?? 30)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tests, setTests] = useState<Record<string, TestState>>({})
  // v3.5（D151）：模型预设（哪个朋友用哪个，在「朋友 → 编辑人格 → 她的模型」里选）
  const [presets, setPresets] = useState<PresetDraft[]>(() =>
    (settings.llm_bases ?? []).map((base) => {
      const caps = (base.capabilities ?? {}) as { vision?: boolean; reasoning?: boolean }
      return {
        name: base.name,
        base_url: base.base_url ?? '',
        model: base.model ?? '',
        protocol: base.protocol === 'anthropic' || base.protocol === 'gemini' ? base.protocol : 'openai',
        api_key: '',
        api_key_set: base.api_key_set,
        vision: Boolean(caps.vision),
        reasoning: Boolean(caps.reasoning),
        // v3.10（D161）：老数据里 proxy='' 表示"跟随系统" —— 界面上如实显示它，
        // 但新加/新保存的预设默认给 'auto'（直连优先），跟用途卡一致。
        proxy: base.proxy ?? 'auto',
      }
    }),
  )

  const enabledFlags = settings.purposes_enabled ?? {}

  useEffect(() => {
    localStorage.setItem('yf.settings.mode', mode)
  }, [mode])

  useEffect(() => {
    if (!initialTab) return
    setMode('advanced')
    setTab(initialTab)
  }, [initialTab])

  function patchPurpose(id: string, patch: PurposeConfig) {
    setPurposes((prev) => ({ ...prev, [id]: { ...(prev[id] ?? {}), ...patch } }))
  }

  function patchPreset(index: number, patch: Partial<PresetDraft>) {
    setPresets((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)))
  }

  async function testPurpose(meta: PurposeMeta) {
    setTests((prev) => ({ ...prev, [meta.id]: { state: 'busy' } }))
    try {
      const { api_key_set: _drop, ...draft } = purposes[meta.id] ?? {}
      const result = await api.testPurpose(meta.id, draft as Record<string, unknown>)
      setTests((prev) => ({
        ...prev,
        [meta.id]: result.ok
          ? { state: 'ok', text: `通了 · ${result.latency_ms ?? '?'} ms` }
          : { state: 'fail', text: result.error ?? '连不上' },
      }))
    } catch (e) {
      setTests((prev) => ({
        ...prev,
        [meta.id]: { state: 'fail', text: e instanceof Error ? e.message : String(e) },
      }))
    }
  }

  async function save(overrides?: Record<string, PurposeConfig>) {
    if (busy) return
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      // api_key_set 只是界面标记，别存进库；api_key 没动过就不带（后端=沿用旧值）
      const payload: Record<string, Record<string, unknown>> = {}
      const mergedPurposes = { ...purposes, ...(overrides ?? {}) }
      for (const [name, conf] of Object.entries(mergedPurposes)) {
        const { api_key_set: _drop, ...rest } = conf
        payload[name] = rest as Record<string, unknown>
      }
      await api.patchSettings({
        purposes: payload,
        // v3.5（D151）：模型预设（名字空的行直接丢掉；api_key 留空 = 不改）
        llm_bases: presets
          .filter((preset) => preset.name.trim())
          .map((preset) => ({
            name: preset.name.trim(),
            base_url: preset.base_url.trim(),
            model: preset.model.trim(),
            protocol: preset.protocol,
            proxy: preset.proxy,
            max_retries: 1,
            capabilities: { vision: preset.vision, reasoning: preset.reasoning },
            ...(preset.api_key.trim() ? { api_key: preset.api_key.trim() } : {}),
          })),
        active_message_frequency: frequency,
        deep_thinking: deepThinking,
        show_tool_calls: showToolCalls,
        memory_token_budget: budget,
        warmup_enabled: warmupEnabled,
        warmup_interval_minutes: warmupMinutes,
        file_write_approval: fileWriteApproval,
        screen_watch_enabled: screenWatch,
        auto_start: autoStart,
        voice_keep_days: voiceKeepDays,
      })
      setMessage('已保存')
      onSaved()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  /** 普通页下载完增强模型：自动落库并启用，不用再让用户点一次保存。 */
  function handleEmbeddingDownloaded(info: EmbeddingDownloadState) {
    const next: PurposeConfig = {
      ...(purposes.embedding ?? {}),
      enabled: true,
      provider: 'local',
      model: info.model,
      dim: info.dim,
      min_similarity: purposes.embedding?.min_similarity ?? 0.45,
      allow_download: false,
    }
    patchPurpose('embedding', next)
    void save({ embedding: next })
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim/40 p-3 backdrop-blur-sm">
      {/* ⚠️ 高度用百分比：zoom 会把 vh 一起乘掉（D80 第 1 条） */}
      <div className="flex h-[86%] w-[820px] max-w-[96%] flex-col overflow-hidden rounded-2xl border border-line bg-page shadow-2xl">
        <div className="flex shrink-0 items-center gap-3 border-b border-line/60 px-4 py-2.5">
          <h2 className="text-[14px] font-medium text-ink">设置</h2>
          <Segmented
            value={mode}
            options={[
              { value: 'simple', label: '普通' },
              { value: 'advanced', label: '高级' },
            ]}
            onChange={setMode}
          />
          {onOpenTutorial && (
            <button
              type="button"
              onClick={() => onOpenTutorial(tab)}
              title="打开当前页教程"
              aria-label="打开教程"
              className="flex h-5 w-5 items-center justify-center rounded-full border border-line-strong text-[11px] text-faint transition-colors hover:border-accent/50 hover:text-accent"
            >
              ?
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="ml-auto rounded-lg px-2 py-0.5 text-faint transition-colors hover:bg-chip hover:text-muted"
          >
            ✕
          </button>
        </div>

        <div className="flex min-h-0 flex-1">
          {mode === 'advanced' && (
            <nav className="flex w-[112px] shrink-0 flex-col gap-0.5 border-r border-line/60 p-2">
            {TABS.map(({ id, label, Icon }) => {
              const active = tab === id
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => setTab(id)}
                  className={`group relative flex items-center gap-2 rounded-xl px-2.5 py-1.5 text-left text-[12px] transition-all duration-200 hover:-translate-y-px hover:bg-surface/80 hover:shadow-[0_2px_10px_rgba(0,0,0,0.05)] ${
                    active ? 'bg-surface/90 font-medium text-ink' : 'text-muted hover:text-ink'
                  }`}
                >
                  {active && (
                    <span
                      className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-r bg-accent"
                      aria-hidden
                    />
                  )}
                  <Icon size={14} className={active ? 'text-accent' : ''} />
                  {label}
                </button>
              )
            })}
            </nav>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto p-3.5">
            {mode === 'simple' ? (
              <SimpleSettingsView
                uiScale={uiScale}
                onScaleChange={onScaleChange}
                chatConfig={purposes.chat ?? {}}
                onChatPatch={(patch) => patchPurpose('chat', patch)}
                onChatTest={() => void testPurpose(PURPOSES[0])}
                chatTest={tests.chat}
                embeddingConfig={purposes.embedding ?? {}}
                onEmbeddingPatch={(patch) => patchPurpose('embedding', patch)}
                onEmbeddingDownloaded={handleEmbeddingDownloaded}
                autoStart={autoStart}
                onAutoStartChange={setAutoStart}
                warmupEnabled={warmupEnabled}
                onWarmupChange={setWarmupEnabled}
                frequency={frequency}
                onFrequencyChange={setFrequency}
                budget={budget}
                onBudgetChange={setBudget}
                onOpenAdvancedTab={(nextTab) => {
                  setMode('advanced')
                  setTab(nextTab)
                }}
                onOpenAdvanced={() => setMode('advanced')}
              />
            ) : (
              <>
            {tab === 'appearance' && (
              <section>
                <h3 className="text-[12px] font-medium text-ink">界面缩放</h3>
                <p className="mt-0.5 text-[11px] text-faint">
                  只影响这台电脑的显示。点一下立刻生效（不用按保存）；也可用 Ctrl+滚轮 或 Ctrl+加减号。
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {UI_SCALE_CHOICES.map((choice) => (
                    <button
                      key={choice.value}
                      type="button"
                      onClick={() => onScaleChange(choice.value)}
                      className={`rounded-full border px-2.5 py-0.5 text-[11.5px] transition-colors ${
                        Math.abs(uiScale - choice.value) < 0.001
                          ? 'border-accent text-accent'
                          : 'border-line-strong text-muted hover:border-accent/50 hover:text-accent'
                      }`}
                    >
                      {choice.label}
                    </button>
                  ))}
                </div>
                <p className="mt-3 text-[11px] text-faint">
                  白天 / 夜间 / 跟随系统 在主窗口右上角切换。
                </p>
              </section>
            )}

            {tab === 'models' && (
              <section className="space-y-2.5">
                <div>
                  <h3 className="text-[12px] font-medium text-ink">按用途配模型（选择协议）</h3>
                  <p className="mt-0.5 text-[11px] text-faint">
                    任何家的 OpenAI 兼容端点，或 Anthropic / Gemini 原生协议都能填；
                    只有「聊天」是必须的，其它都是可选能力。
                    语音合成 / 语音识别在「语音」页配（v2.1 起归到那儿，不用两页跑）。
                  </p>
                </div>
                {PURPOSES.filter((meta) => meta.page === 'models').map((meta) => (
                  <PurposeCard
                    key={meta.id}
                    meta={meta}
                    config={purposes[meta.id] ?? {}}
                    enabled={Boolean(enabledFlags[meta.id])}
                    onPatch={(patch) => patchPurpose(meta.id, patch)}
                    onTest={() => void testPurpose(meta)}
                    test={tests[meta.id]}
                  />
                ))}

                {/* v3.5（D151）：模型预设 —— 给"某个朋友单独用"的端点（聪明的不设限的，各取所需） */}
                <div className="rounded-2xl border border-line/70 bg-surface/70 p-3">
                  <h3 className="text-[12px] font-medium text-ink">模型预设（给单个朋友用）</h3>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-faint">
                    这里只是"备选端点"；**哪个朋友用哪个**在「朋友 → ⋯ → 编辑人格 → 她的模型」里选。
                    没选的朋友就跟随上面「聊天」那一份。
                  </p>
                  {presets.map((preset, index) => (
                    <div key={index} className="mt-2 rounded-xl border border-line/60 bg-page/40 p-2.5">
                      <div className="flex items-center gap-2">
                        <input
                          value={preset.name}
                          onChange={(e) => patchPreset(index, { name: e.target.value })}
                        placeholder="名字（给朋友选的时候显示，比如 chat）"
                          className="min-w-0 flex-1 rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
                        />
                        <button
                          type="button"
                          onClick={() => setPresets((prev) => prev.filter((_, i) => i !== index))}
                          className="shrink-0 rounded-xl border border-line bg-surface px-2.5 py-1.5 text-[11.5px] text-muted hover:border-red-300 hover:text-red-500"
                        >
                          删掉
                        </button>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <input
                          value={preset.base_url}
                          onChange={(e) => patchPreset(index, { base_url: e.target.value })}
                          placeholder="服务地址（https://.../v1）"
                          className="min-w-[200px] flex-1 rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
                        />
                        <input
                          value={preset.model}
                          onChange={(e) => patchPreset(index, { model: e.target.value })}
                          placeholder="模型名"
                          className="min-w-[160px] flex-1 rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
                        />
                        <select
                          value={preset.protocol}
                          onChange={(e) =>
                            patchPreset(index, {
                              protocol: e.target.value as 'openai' | 'anthropic' | 'gemini',
                            })
                          }
                          className="rounded-xl border border-line bg-surface/80 px-2 py-1.5 text-[11.5px] text-ink outline-none focus:border-accent/60"
                        >
                          <option value="openai">OpenAI 兼容</option>
                          <option value="anthropic">Anthropic</option>
                          <option value="gemini">Gemini</option>
                        </select>
                      </div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-2">
                        <input
                          type="password"
                          value={preset.api_key}
                          onChange={(e) => patchPreset(index, { api_key: e.target.value })}
                          placeholder={preset.api_key_set ? '已保存 · 留空不改' : 'sk-...'}
                          className="min-w-[200px] flex-1 rounded-xl border border-line bg-surface/80 px-2.5 py-1.5 text-[12px] outline-none focus:border-accent/60"
                        />
                        <label className="flex items-center gap-1 text-[11px] text-muted">
                          <input
                            type="checkbox"
                            className="accent-[var(--color-accent)]"
                            checked={preset.vision}
                            onChange={(e) => patchPreset(index, { vision: e.target.checked })}
                          />
                          能看图
                        </label>
                        <label className="flex items-center gap-1 text-[11px] text-muted">
                          <input
                            type="checkbox"
                            className="accent-[var(--color-accent)]"
                            checked={preset.reasoning}
                            onChange={(e) => patchPreset(index, { reasoning: e.target.checked })}
                          />
                          支持思考链
                        </label>
                        {/* v3.10（D161）：网络怎么走也让人看得见、改得了（默认"直连优先"） */}
                        <label className="flex items-center gap-1 text-[11px] text-muted">
                          网络
                          <select
                            value={preset.proxy}
                            onChange={(e) => patchPreset(index, { proxy: e.target.value })}
                            className="rounded-lg border border-line bg-surface/80 px-1.5 py-0.5 text-[11px] text-ink outline-none focus:border-accent/60"
                          >
                            <option value="auto">直连优先</option>
                            <option value="">跟随系统</option>
                            <option value="none">只直连</option>
                          </select>
                        </label>
                      </div>
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={() =>
                      setPresets((prev) => [
                        ...prev,
                        {
                          name: '',
                          base_url: '',
                          model: '',
                          protocol: 'openai',
                          api_key: '',
                          vision: false,
                          reasoning: false,
                          proxy: 'auto',
                        },
                      ])
                    }
                    className="mt-2 rounded-xl border border-line bg-surface px-3 py-1.5 text-[12px] text-ink transition-colors hover:border-accent/60"
                  >
                    + 加一个预设
                  </button>
                </div>
              </section>
            )}

            {tab === 'embedding' && (
              <EmbeddingAdvancedPanel
                config={purposes.embedding ?? {}}
                onPatch={(patch) => patchPurpose('embedding', patch)}
              />
            )}

            {tab === 'voice' && (
              <section className="space-y-3">
                <div>
                  <h3 className="text-[12px] font-medium text-ink">语音</h3>
                  <p className="mt-0.5 text-[11px] text-faint">
                    语音相关的都在这儿：合成 / 识别的模型配置、两个本地服务的启停、音色库。
                  </p>
                </div>
                {/* v2.1：从「模型」页挪过来的语音合成卡片（程序目录仍只在音色库里填一处） */}
                {PURPOSES.filter((meta) => meta.page === 'voice').map((meta) => (
                  <PurposeCard
                    key={meta.id}
                    meta={meta}
                    config={purposes[meta.id] ?? {}}
                    enabled={Boolean(enabledFlags[meta.id])}
                    onPatch={(patch) => patchPurpose(meta.id, patch)}
                    onTest={() => void testPurpose(meta)}
                    test={tests[meta.id]}
                  />
                ))}
                {/* 语音识别：本地 Paraformer，不用配 key —— 说清楚它是什么、去哪儿启停 */}
                <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
                  <div className="flex items-center gap-2">
                    <IconMic size={15} className="text-muted" />
                    <span className="text-[12px] text-ink">语音识别（按住说话）</span>
                    <span className="text-[10.5px] text-faint">本地 Paraformer · 不用填 key</span>
                  </div>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
                    用的是 GPT-SoVITS runtime 里自带的模型，纯 CPU（不占显存）、语音不出本机。
                    服务的启停和状态在下面那张「本地语音服务」卡片里。
                  </p>
                </div>
                <VoicePanel
                  sovitsDir={purposes.tts?.sovits_dir ?? ''}
                  onSovitsDirChange={(dir) => patchPurpose('tts', { sovits_dir: dir })}
                />
                {/* v3.0（D141）：语音文件很占地方（每分钟 ≈ 5.6 MB）→ 每天自动清 */}
                <label className="mt-3 block">
                  <span className="mb-1 block text-[11px] text-muted">消息语音保留多久</span>
                  <select
                    className="w-[260px] rounded-xl border border-line bg-surface/80 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/60"
                    value={voiceKeepDays}
                    onChange={(e) => setVoiceKeepDays(Number(e.target.value))}
                  >
                    <option value={7}>7 天（省空间）</option>
                    <option value={30}>30 天（默认）</option>
                    <option value={90}>90 天</option>
                    <option value={0}>永久留着（只按总量上限清）</option>
                  </select>
                  <p className="mt-1 text-[10.5px] leading-relaxed text-faint">
                    每天会自动清：过期的、以及没有任何消息引用的孤儿文件。超过 1 GB 时从最旧的开始删；
                    <span className="text-muted">最近 200 条消息的语音永远保留</span>
                    （删掉的语音在气泡上显示「重新合成」，字一个都不少）。
                  </p>
                </label>
              </section>
            )}

            {tab === 'live2d' && <Live2DPanel />}

            {tab === 'portraits' && <PortraitPacksPanel />}

            {tab === 'behavior' && (
              <section className="space-y-3">
                <h3 className="text-[12px] font-medium text-ink">行为</h3>
                {onOpenGameMode && (
                  <button
                    type="button"
                    onClick={onOpenGameMode}
                    className="group flex w-full items-center gap-3 rounded-2xl border border-accent/25 bg-accent/5 px-3 py-2.5 text-left transition-colors hover:border-accent/55 hover:bg-accent/10"
                  >
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-accent/10 text-accent">
                      <IconGamepad size={16} />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-[12px] font-medium text-ink">
                        游戏设置已移到「游戏」全屏模式
                      </span>
                      <span className="mt-0.5 block text-[10.5px] leading-relaxed text-faint">
                        说话节拍、游戏快脑、作弊开关、技能、蓝图、数据和日志都在那里。
                      </span>
                    </span>
                    <span className="shrink-0 text-[11px] text-accent transition-transform group-hover:translate-x-0.5">
                      打开 →
                    </span>
                  </button>
                )}
                <label className="block">
                  <span className="mb-1 block text-[11px] text-muted">
                    主动消息频率（分钟；60 ≈ 一天 4~8 条）
                  </span>
                  <input
                    type="number"
                    min={1}
                    max={1440}
                    className="w-[160px] rounded-xl border border-line bg-surface/80 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/60"
                    value={frequency}
                    onChange={(e) => setFrequency(Number(e.target.value))}
                  />
                </label>
                <div className="flex flex-wrap gap-4">
                  <label className="flex items-center gap-2 text-[12px] text-muted">
                    <input
                      type="checkbox"
                      checked={deepThinking}
                      onChange={(e) => setDeepThinking(e.target.checked)}
                    />
                    思考链（默认关；开了更慢更贵，内容不落库，D23）
                  </label>
                  <label className="flex items-center gap-2 text-[12px] text-muted">
                    <input
                      type="checkbox"
                      checked={showToolCalls}
                      onChange={(e) => setShowToolCalls(e.target.checked)}
                    />
                    聊天里显示工具调用（调试用，D31）
                  </label>
                </div>
                {/* v2（D87）：定时预热 */}
                <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
                  <label className="flex items-center gap-2 text-[12px] text-muted">
                    <input
                      type="checkbox"
                      checked={warmupEnabled}
                      onChange={(e) => setWarmupEnabled(e.target.checked)}
                    />
                    定时预热（隔一会儿发个不花 token 的空请求，把连接和上游捂热）
                  </label>
                  <label className="mt-2 flex items-center gap-2 text-[12px] text-muted">
                    <span>间隔</span>
                    <input
                      type="number"
                      min={1}
                      max={120}
                      className="w-[90px] rounded-xl border border-line bg-surface/80 px-2.5 py-1 text-[12.5px] outline-none focus:border-accent/60 disabled:opacity-40"
                      value={warmupMinutes}
                      disabled={!warmupEnabled}
                      onChange={(e) => setWarmupMinutes(Number(e.target.value))}
                    />
                    <span>分钟</span>
                  </label>
                  <p className="mt-1 text-[10.5px] leading-relaxed text-faint">
                    就是"列一下模型列表"这种最小请求，不花 token。留着它，让连接和上游一直
                    保持热着；关掉就只在开软件时预热一次。
                  </p>
                </div>
                {/* v2.1（D99）：文件改动逐笔点头 */}
                <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
                  <label className="flex items-start gap-2 text-[12px] text-muted">
                    <input
                      type="checkbox"
                      className="mt-[3px]"
                      checked={fileWriteApproval}
                      onChange={(e) => setFileWriteApproval(e.target.checked)}
                    />
                    <span>
                      文件改动要我点头
                      <span className="mt-0.5 block text-[10.5px] leading-relaxed text-faint">
                        她读文件随便读，不打扰你；写 / 挪 / 删会弹一张卡片问你，
                        你点「允许」才真的动。关掉这一项 = 免授权（她说改就改，谨慎）。
                      </span>
                    </span>
                  </label>
                </div>
                {/* v2.1（D100）+ v2.2（D103）：看屏 = 主动消息的第一步 */}
                <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
                  <label className="flex items-start gap-2 text-[12px] text-muted">
                    <input
                      type="checkbox"
                      className="mt-[3px]"
                      checked={screenWatch}
                      onChange={(e) => setScreenWatch(e.target.checked)}
                    />
                    <span>
                      看屏（她主动找你说话之前，先瞟一眼你在干嘛）
                      <span className="mt-0.5 block text-[10.5px] leading-relaxed text-faint">
                        只在「她要主动找你说话」的那一刻看一眼 —— 所以频率就是上面那个
                        「主动消息频率」，不会在后台偷偷看。看过之后她可以选择不说话
                        （那就完全不打扰你，只把看到的留着）；截图会发给云端视觉模型，
                      一次约 1500 token。
                      </span>
                    </span>
                  </label>
                  <label className="flex cursor-pointer items-start gap-2 text-[13px] text-ink">
                    <input
                      type="checkbox"
                      className="mt-[3px]"
                      checked={autoStart}
                      onChange={(e) => setAutoStart(e.target.checked)}
                    />
                    <span>
                      开机自启（开机后 yumeFriend 自己起来）
                      <span className="mt-0.5 block text-[10.5px] leading-relaxed text-faint">
                        打开后会在 Windows 的「启动」文件夹里放一个
                        <code className="mx-1">yumeFriend-autostart.cmd</code>
                        （内容是"启动 yumeFriend"，你可以自己打开看）；
                        关掉这个开关就把那个文件删掉。打包版指向 exe 自己；
                        手机端要能连上，靠的就是它别被忘在关机状态。
                      </span>
                    </span>
                  </label>
                  <p className="mt-1 text-[10.5px] leading-relaxed text-faint">
                    她看到的东西会用在这次主动消息里（"你那游戏打俩小时了？"）；
                    没说的话也会留着（半小时内有效），下次聊天时自然带上。
                  </p>
                </div>
              </section>
            )}

            {tab === 'features' && <FeatureCenter onChanged={onSaved} />}

            {tab === 'memory' && (
              <section className="space-y-3">
                <h3 className="text-[12px] font-medium text-ink">记忆</h3>
                <label className="block">
                  <span className="mb-1 block text-[11px] text-muted">记忆注入预算（token，默认 3000）</span>
                  <input
                    type="number"
                    min={500}
                    max={100000}
                    step={500}
                    className="w-[160px] rounded-xl border border-line bg-surface/80 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/60"
                    value={budget}
                    onChange={(e) => setBudget(Number(e.target.value))}
                  />
                </label>
                <p className="text-[11px] text-faint">记忆的浏览 / 编辑在各个朋友的「记忆」里。</p>
              </section>
            )}

            {tab === 'mobile' && <MobilePanel />}
      {tab === 'sync' && <SyncPanel />}
            {tab === 'update' && <UpdatePanel />}

            {tab === 'about' && (
              <section className="space-y-3">
                <h3 className="text-[12px] font-medium text-ink">关于</h3>
                <p className="text-[11.5px] text-muted">
                  当前聊天模型：<span className="text-ink">{settings.purposes?.chat?.model ?? '未配置'}</span>
                </p>
                <p className="text-[11px] text-faint">
                  排查问题时：主窗口左下角「日志」里有实时日志和一键诊断。
                </p>
                <LegalDocs />
              </section>
            )}
              </>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2 border-t border-line/60 px-4 py-2.5">
          {message && <span className="text-[11.5px] text-muted">{message}</span>}
          {error && <span className="text-[11.5px] text-red-600">出错了：{error}</span>}
          <div className="ml-auto flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-xl border border-line-strong px-3 py-1.5 text-[12.5px] text-muted transition-colors hover:border-stone-400"
            >
              关闭
            </button>
            {(mode === 'simple' ||
              (tab !== 'live2d' && tab !== 'portraits' && tab !== 'update')) && (
              <button
                type="button"
                onClick={() => void save()}
                disabled={busy}
                className="rounded-xl bg-accent px-4 py-1.5 text-[12.5px] font-medium text-white transition-opacity disabled:opacity-40"
              >
                {busy ? '保存中…' : '保存'}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
