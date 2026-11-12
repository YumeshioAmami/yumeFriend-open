/** 后端 API 封装（只调 /api/*，开发期由 Vite 代理到 127.0.0.1:8001）。 */

import { readSse, type SseFrame } from './sse'
import { markOffline, markOnline } from './netStatus'
import { shrinkImage } from './shrinkImage'
import type { Live2DModelMeta, Live2DModelSettings } from './live2dPresets'

export interface Friend {
  id: string
  name: string
  presence: boolean
  current_mode: 'normal' | 'roleplay' | 'tool'
  current_role: string | null
  unread_count: number
  last_message_preview: string | null
  last_message_at: string | null
  has_unfinished: boolean
  /** v2：有头像才有值（带 ?v= 破缓存）；null = 界面用首字母色块 */
  avatar_url: string | null
  /** v2.2（D107）：沉浸模式的背景图（null = 用默认氛围底） */
  background_url?: string | null
  /** v2.2（D109）：她真有立绘的情绪（前端给表情兜底用） */
  portrait_emotions?: string[]
  /** v2（T2.0-4）：配了哪个音色（null = 没配，通话里给提示用） */
  voice_id?: string | null
  /** v2（T2.0-5）：她的回复都用语音 */
  voice_reply_enabled?: boolean
  /** v2.2（D110）：立绘的缩放 / 位移（沉浸模式里拖出来的） */
  portrait_transform?: { scale: number; x: number; y: number } | null
  /** v7.12（D341）：生活线开关（只有电脑端会推进活动） */
  life_enabled?: boolean
  /** v7.13（D344）：轻聊/深聊分流 */
  chat_pacing_enabled?: boolean
  /** v7.13（D344）：表达学习 + 低概率错字 */
  humanizer_enabled?: boolean
  /** v7.43（D441）：呈现方式：Live2D 模型 / 立绘包 */
  visual_mode?: 'live2d' | 'portrait'
  live2d_model_id?: string | null
  portrait_pack_id?: string | null
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export interface FriendDetail extends Friend {
  personality: string
  custom_instructions: string
  /** v2（D84）：她的样子——上传头像时自动生成一段，也能自己改 */
  avatar_description: string | null
  /** v2（T2.0-4）：用哪个音色（null = 不发声） */
  voice_id: string | null
  /** v2（D85）：开了 → 她的每条回复都配语音 */
  voice_reply_enabled: boolean
  /** v3.5（D151）：这个朋友用哪个模型预设（`settings.llm_bases` 的 name）；null = 跟随全局聊天 */
  llm_base: string | null
  /** v4.3（D249）：她的"温度"（越高越跳）；null = 跟随默认（主对话 0.7 / 主动消息 0.8） */
  temperature: number | null
  /** v6.0（D298）：声音样本（学语气，不是模板） */
  voice_examples: string[]
  /** v7.12（D341）：生活线开关 */
  life_enabled: boolean
  /** v7.13（D344）：轻聊/深聊分流 */
  chat_pacing_enabled: boolean
  /** v7.13（D344）：表达学习 + 低概率错字 */
  humanizer_enabled: boolean
}

export interface Live2DModelSummary {
  id: string
  name: string
  model_file: string
  source: string
  used_by: { id: string; name: string }[]
  has_settings: boolean
  /** D445：仓库内置的默认模型。 */
  is_default: boolean
}

export interface Live2DRuntimeStatus {
  available: boolean
  installed: boolean
  source: 'installed' | 'bundled' | null
  size: number | null
  sha256: string | null
  official_download_page: string
  license_urls: {
    proprietary: string
    open: string
  }
}

export interface Live2DRuntimeProgress {
  received: number
  total: number
}

export interface PortraitPack {
  id: string
  name: string
  legacy: boolean
  friend_id: string | null
  emotions: number
  bytes: number
  items: Portrait[]
  used_by: { id: string; name: string }[]
}

export interface SpaceProfileZone {
  lines: string[]
  expression: string
}

export type SpaceIdleKind = 'window' | 'read' | 'tail' | 'phone' | 'stretch'
export type SpaceObjectKey = 'book' | 'mug' | 'cushion' | 'notebook' | 'plush'
export interface SpaceOutfit {
  top: string
  bottom: string
  shoes: string
  accessory: string
}

export interface SpaceIdleActivity {
  kind: SpaceIdleKind
  text: string
}

export interface SpaceNoticeReaction {
  lines: string[]
  expression: string
}

export interface SpaceProfile {
  version: number
  attention: {
    curiosity: number
    patience: number
    initiative: number
  }
  idle: SpaceIdleActivity[]
  notice: SpaceNoticeReaction
  call: SpaceNoticeReaction
  zones: Record<'head' | 'ear' | 'tail' | 'hand' | 'body', SpaceProfileZone>
  objects: Record<SpaceObjectKey, SpaceProfileZone>
}

/** v7.21（D374）：小世界房间状态 / 事件 / 自主动作。 */
export interface SpaceState {
  friend_id: string
  layout: Record<string, [number, number, number]>
  mode: 'free' | 'together'
  current_action: Record<string, unknown>
  prop_states: Record<string, string>
  world: {
    environment: 'room' | 'white'
    objects: {
      id: string
      asset: string
      position: [number, number, number]
      rotation: number
      scale: number
      state?: Record<string, unknown>
    }[]
  }
  appearance: { outfit: SpaceOutfit; model?: string | null }
  updated_at: string
}

export interface SpaceEvent {
  id: string
  friend_id: string
  kind: string
  target: string | null
  detail: Record<string, unknown>
  notable: boolean
  created_at: string
}

export interface SpaceAction {
  kind:
    | 'go_to'
    | 'sit'
    | 'lie'
    | 'use'
    | 'arrange'
    | 'idle'
    | 'wait'
    | 'approach'
    | 'set_state'
    | 'pick_up'
    | 'put_down'
    | 'push'
  target: string | null
  duration_seconds: number
  text?: string | null
  id?: string
  state?: Record<string, unknown>
  strength?: number
}

/** v7.26（D384 Phase A）：她在小世界里能读到的用户在场快照。 */
export interface SpaceViewerSnapshot {
  camera: [number, number, number]
  look: [number, number, number]
  hand: [number, number, number]
  hand_active: boolean
  hand_holding?: string | null
  hand_contact_zone?: string | null
  hand_contact_kind?: 'hold' | 'drag' | 'push' | 'pull' | null
  hand_contact_speed?: number
  hand_contact_seconds?: number
  last_moved_at?: number
}

export interface SpaceRuntime {
  friend_id: string
  attention: 'busy' | 'notice' | 'engaged'
  intent: Record<string, unknown>
  interrupt_stack: Record<string, unknown>[]
  opening_stage: 'waiting' | 'observing' | 'understood' | 'realized' | 'daily'
  opening_target: string | null
  opening_seen: boolean
  discovery_score: number
  wonder_said: boolean
  last_discovery_at: string | null
  last_deliberation_at: string | null
  next_deliberation_at: string | null
  goal: string | null
  plan: string[]
  plan_step: number
  last_action_result: Record<string, unknown>
  last_perception_at: string | null
  updated_at: string
}

export interface SpaceKnowledge {
  id: string
  friend_id: string
  object_key: string
  label: string
  status: 'unknown' | 'observed' | 'understood'
  user_explanation: string
  novelty: number
  salience: number
  ask_pressure: number
  ask_count: number
  ignored_count: number
  last_seen_at: string | null
  last_observed_at: string | null
  last_asked_at: string | null
  last_answered_at: string | null
  updated_at: string
}

export interface SpaceDiscovery {
  score: number
  understood: number
  pending_ask: {
    kind?: string
    target?: string
    label?: string
    reason?: string
    line?: string
    source?: 'local' | 'llm'
  } | null
  wonder_said: boolean
}

export interface SpacePerceptionObject {
  key: string
  label: string
  position: [number, number, number]
  moving: boolean
}

export interface SpaceAskIntent {
  object_key: string
  label: string
  salience: number
  ask_pressure: number
  reason: string
}

export interface SpaceSelfState {
  position: [number, number, number]
  hand?: [number, number, number] | null
  holding?: string | null
  posture?: 'stand' | 'sit' | 'lie'
  sitting?: boolean
  lying?: boolean
  support_key?: string | null
  moving: boolean
  still_seconds: number
  target: string | null
  at_target: boolean
}

export type SpaceChatStreamEvent =
  | { type: 'thought'; delta: string }
  | { type: 'speak'; delta: string }
  | {
      type: 'done'
      line: string | null
      thought: string
      emotion: string
      action: SpaceAction | null
      runtime: SpaceRuntime | null
      knowledge?: SpaceKnowledge[]
      discovery?: SpaceDiscovery | null
    }
  | { type: 'error'; message: string }

export interface SpaceSurface {
  key: string
  label: string
  kind: string
  center: [number, number, number]
  top: number
  half: [number, number]
  rotation: number
  walkable: boolean
  sittable: boolean
  lieable: boolean
}

export interface SpaceWorldEntity {
  key: string
  label: string
  kind: string
  asset?: string
  position: [number, number, number] | null
  zone: { key: string; label: string }
  support: string
  state: Record<string, unknown>
  moving: boolean
  distance_to_me: number | null
  distance_to_user: number | null
  known: string
  affordances: string[]
  last_event: string | null
}

export interface SpaceWorldModel {
  friend_id: string
  time: string
  environment: string
  room: { width: number; depth: number; height: number }
  zones: { key: string; label: string; center: [number, number, number]; radius: number }[]
  senses: {
    time_phase: { key: string; label: string }
    light: {
      level: number
      room_light: string
      curtain: string
      window: string
      description: string
    }
    sound: { description: string; moving: string[] }
    temperature: { celsius: number; description: string }
    air: { description: string }
    summary: string[]
  }
  self: {
    position: [number, number, number] | null
    hand?: [number, number, number] | null
    holding?: string | null
    posture?: 'stand' | 'sit' | 'lie'
    sitting?: boolean
    lying?: boolean
    support_key?: string | null
    outfit?: SpaceOutfit
    moving: boolean
    still_seconds: number
    target: string | null
    at_target: boolean
    activity: string | null
    mood: string | null
  }
  surfaces?: SpaceSurface[]
  user: {
    camera: [number, number, number] | null
    hand: [number, number, number] | null
    hand_active: boolean
    hand_holding: string | null
    hand_contact_zone?: string | null
    hand_contact_kind?: 'hold' | 'drag' | 'push' | 'pull' | null
    hand_contact_speed?: number
    hand_contact_seconds?: number
    look: [number, number, number] | null
  }
  entities: SpaceWorldEntity[]
  recent_events: { kind: string; target: string | null; text: string }[]
  recent_chat: {
    role: string
    content: string
    at: string
    emotion?: string
    proactive?: boolean
  }[]
  space_chat: {
    role: string
    content: string
    at: string
    emotion?: string
    proactive?: boolean
  }[]
  shared_state: {
    intent: string
    inner_note: string
    open_loops: string[]
    energy: number
    affect: Record<string, number>
  }
  goal: string | null
  plan: string[]
  plan_step: number
  last_action_result: Record<string, unknown>
}

export interface SpaceDeliberationFocus {
  object_key: string
  label: string
  salience: number
  ask_pressure: number
  reason: string
}

export type StyleSectionKey =
  | 'length'
  | 'tone'
  | 'diction'
  | 'structure'
  | 'situations'
  | 'keep'

export interface StyleRule {
  id: string
  text: string
  hits: number
  created_at: string
  updated_at: string
}

export interface StyleProfile {
  version: number
  sections: Record<StyleSectionKey, StyleRule[]>
}

export interface StyleProfileResponse {
  profile: StyleProfile
  version: number
  labels: Record<StyleSectionKey, string>
  limits: Record<StyleSectionKey, number>
}

export interface StyleChangeInput {
  action: 'add' | 'update' | 'remove'
  section: StyleSectionKey
  lesson?: string
  target_id?: string
  why?: string
}

export interface StyleEvent {
  id: string
  event_type: string
  summary: string | null
  changes: Array<Record<string, unknown>>
  user_text: string | null
  assistant_excerpt: string | null
  before_version: number
  after_version: number
  created_at: string
}

export interface FriendCreateBody {
  name: string
  personality?: string
  custom_instructions?: string
  /** v6.0（D298）：声音样本 */
  voice_examples?: string[]
  /** v4.3（D249）：她的温度（0.0~1.5）；不传 = 跟随默认 */
  temperature?: number | null
  /** v7.12（D341）：生活线开关（默认关） */
  life_enabled?: boolean
  chat_pacing_enabled?: boolean
  humanizer_enabled?: boolean
}

export interface SyncDeviceInfo {
  device_id: string
  device_name: string
  device_kind: string
}

export interface SyncSessionInfo {
  ok?: boolean
  running?: boolean
  code?: string
  port?: number
  ip?: string
  device_id?: string
  device_name?: string
  device_kind?: string
}

export interface SyncPeer {
  device_id: string
  name: string
  kind: string
  ip: string
  port: number
}

export interface SyncRunResult {
  ok: boolean
  direction?: SyncDirection
  remote?: { device_id?: string; device_name?: string }
  pulled?: Record<string, number>
  pushed?: Record<string, number>
}

export interface LifeActivityView {
  id: string
  friend_id: string
  device_id: string
  kind: string
  title: string
  status: 'running' | 'completed' | 'failed' | 'cancelled' | string
  started_at: string
  ends_at: string | null
  finished_at: string | null
  summary: string
  observations: Array<Record<string, unknown>>
  tool_calls: Array<Record<string, unknown>>
  updated_at: string
}

export interface LifeStatus {
  ok?: boolean
  friend_id: string
  enabled: boolean
  paused_by_chat: boolean
  paused_until: string | null
  last_observation: string
  current_activity: LifeActivityView | null
  recent: LifeActivityView[]
}

export interface ServerSyncStatus {
  ok?: boolean
  mode: 'local' | 'server'
  base_url: string
  token_set: boolean
  cursor: string
  last_sync_at: string
  last_error: string
  paused: boolean
  running: boolean
  interval_seconds: number
}

export type SyncDirection = 'both' | 'pull' | 'push'

export interface SpeechStatus {
  configured: boolean
  mode: 'cloud' | 'system'
}

/** v5.0（P3 / D250）：游戏面板 */
export interface GameEventView {
  id: string
  type: string
  payload: Record<string, unknown>
  created_at: string
}

export interface GamePlanTask {
  index: number
  action: string
  args: Record<string, unknown>
  checks: unknown[]
  status: string
  result: unknown
}

export interface GamePlan {
  id: string
  condition: unknown
  expected: unknown
  result: unknown
  checks: unknown[]
  status: string
  tasks: GamePlanTask[]
}

export interface GameStatus {
  active: boolean
  id?: string
  friend_id?: string | null
  game_kind?: string | null
  state?: string
  /** 本局开始的 Unix 时间（秒）；前端用它显示真实时长。 */
  started_at?: number
  mode?: string
  mode_label?: string
  mode_supported?: boolean
  brain?: {
    lane?: string
    model?: string
    action?: string | null
    latency_ms?: number | null
    prompt_tokens?: number | null
    completion_tokens?: number | null
  } | null
  error?: string | null
  adapter_alive?: boolean
  capabilities?: {
    game: string
    adapter_version: string
    supported_versions: string[]
    actions: string[]
  } | null
  tasks?: Record<string, { action?: string; status?: string; args?: Record<string, unknown> }>
  /** 旧版单字符串计划；和 plan_steps 一起兼容现有数据。 */
  plan?: string
  plan_steps?: string[]
  /** D315：真正的结构化计划；旧字段只做兼容。 */
  current_plan?: GamePlan | null
  plans?: GamePlan[]
  last_task?: {
    action?: string
    status?: string
    result?: unknown
  } | null
  /** v5.1（D290）：作弊 / 难度配置。 */
  profile?: string
  cheat_enabled?: boolean
  cheat_abilities?: string[]
  blueprint_job?: Record<string, unknown> | null
  reflexes?: { modes: Record<string, GameReflexMode> }
  recent_events?: GameEventView[]
  version_compat?: GameVersionCompat | null
}

export interface GameVersionCompat {
  pack_version: string
  pack_protocol: number | null
  server_version: string
  server_protocol: number | null
  mode: 'exact' | 'same_protocol' | 'experimental' | string
  warning: string | null
}

export interface MinecraftVersionDownloadState {
  state: 'idle' | 'downloading' | 'verifying' | 'extracting' | 'done' | 'error' | string
  version: string | null
  requested_version?: string
  downloaded_bytes: number
  total_bytes: number
  percent: number
  error: string | null
  installed: boolean
  updated_at: string
}

export interface MinecraftVersionInfo {
  version: string
  protocol: number | null
  url: string
  sha256: string
  size: number
  tier: 'stable' | 'compatible' | 'installed' | string
  installed: boolean
  installed_size: number | null
}

export interface MinecraftVersionStatus {
  manifest_ok: boolean
  manifest_source: string
  manifest_error: string | null
  minecraft_data_version: string
  mineflayer_version: string
  generated_at: string
  latest_version: string | null
  installed_count: number
  versions: MinecraftVersionInfo[]
  download: MinecraftVersionDownloadState
}

export interface UsageTotals {
  calls: number
  usage_missing_calls: number
  input_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cache_hit_tokens: number
  cache_miss_tokens: number
  total_tokens: number
}

export interface UsageSummary {
  days: number
  since: string
  totals: UsageTotals
  by_model: Record<string, UsageTotals>
  by_purpose: Record<string, UsageTotals>
  by_day: Record<string, UsageTotals>
  recent: Array<Record<string, unknown>>
}

export type GameProfile = 'survival' | 'assisted' | 'operator' | 'creative'

export interface GameCheatConfig {
  profile: GameProfile
  master_enabled: boolean
  abilities: Record<string, boolean>
  active: boolean
  enabled_abilities: string[]
  profiles: GameProfile[]
  abilities_supported: string[]
  profile_labels: Record<string, string>
}

export interface GameReflexMode {
  enabled: boolean
  priority: number
  interrupts: boolean
  cooldown: number
}

export interface GameReflexConfig {
  modes: Record<string, GameReflexMode>
  modes_supported: string[]
  active_modes: string[]
}

/** v5.1（D288）：技能书。 */
export interface GameSkill {
  id: string
  friend_id: string | null
  name: string
  description: string
  params: Record<string, unknown>
  preconditions: unknown[]
  steps: Array<{ action: string; args?: Record<string, unknown>; checks?: unknown[] }>
  postconditions: unknown[]
  versions: Record<string, unknown>
  mods: unknown[]
  stats: Record<string, unknown>
  enabled: boolean
  source: 'builtin' | 'learned' | string
  created_at: string
  updated_at: string
}

export interface GameSkillFailure {
  id: string
  friend_id: string
  plan_id: string | null
  skill_name: string | null
  reason: string
  detail: string
  created_at: string
}

export interface GameBlueprint {
  id: string
  friend_id: string | null
  name: string
  description: string
  blueprint: Record<string, unknown>
  source: string
  created_at: string
  updated_at: string
}

export interface GameCreativeRecord {
  id: string
  friend_id: string
  brief: string
  design: Record<string, unknown>
  anchor: Record<string, unknown>
  size: Record<string, unknown>
  before: unknown[]
  after: unknown[]
  diff: Array<{ x: number; y: number; z: number; before: string; after: string }>
  rating: number | null
  state: string
  created_at: string
  updated_at: string
}

export interface GameStartBody {
  friend_id: string
  game_kind?: string
  host?: string
  port?: number
  username?: string
  /** `auto` = 先探测服务器版本；也可强制指定资源包版本。 */
  version?: string
}

export interface GameSnapshot {
  state?: string
  version?: string
  mode?: string
  player?: {
    name?: string
    x?: number | null
    y?: number | null
    z?: number | null
    health?: number
    food?: number
  }
  inventory?: Array<{ item: string; count: number }>
  inventory_by_item?: Array<{ item: string; count: number; food: boolean }>
  inventory_empty_slots?: number | null
  inventory_full?: boolean | null
  dropped_items?: Array<{
    name: string
    count: number
    distance: number
    x: number
    y: number
    z: number
  }>
  local_grid?: Array<{
    x: number
    z: number
    ground: { name: string; y: number } | null
    feet: string | null
    head: string | null
  }>
  nearby?: Array<{ name: string; distance: number | null }>
  following?: string | null
  tasks?: Array<{
    task_id: string
    action?: string
    status?: string
    ends_at?: number
    seconds?: number
    progress?: { current?: number; target?: number; unit?: string }
  }>
}

/** v2（T2.0-7）：搜网来源（气泡下面渲染成小链接）。 */
export interface Source {
  no: number
  title: string
  url: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  mode_at_send: string | null
  is_proactive: boolean
  stream_status: string | null
  tools_used: string | null
  /** v2（D85）：这条回复的语音状态（null = 没有） */
  audio_status: 'pending' | 'ready' | 'failed' | null
  /** 能播时的音频地址（文件不在就是 null） */
  audio_url: string | null
  audio_duration_ms: number | null
  audio_error: string | null
  /** v2（T2.0-6）：用户发的图（没有就是 null） */
  image_url: string | null
  /** 这个朋友的**第几张图**（提示词占位和 look_image 用它） */
  image_no: number | null
  /** v2（T2.0-7）：这一轮搜网的来源（没有就是 null） */
  sources: Source[] | null
  /** v2.2（D102）：她这条回复用的表情（沉浸模式用它选立绘 + 标情绪） */
  mood?: string | null
  /** v2.2（D113）：她**每段**给的情绪（按段顺序，沉浸模式读到哪段换哪张脸） */
  moods?: string[]
  /** v2.2（D105）：分段语音（沉浸模式"一段一段说"）；没有就是空数组 */
  audio_segments?: AudioSegment[]
  /** v6.0（D298）：一条回复内部的多个气泡；空数组 = 普通单气泡 */
  segments?: string[]
  created_at: string
}

/** v2.2（D105）：一段语音（带这一段的原文，直接当字幕用）。 */
export interface AudioSegment {
  index: number
  url: string
  duration_ms: number | null
  text: string
  /** D112：这一段的情绪（她每段末尾给了标记） */
  mood?: string | null
}

/** v2.2（D102/D107）：一个立绘情绪（文件名就是情绪）。 */
export interface Portrait {
  emotion: string
  suffix: string
  bytes: number
  size: number[]
  updated_at: string
}

/** 扫描候选（还没导入）。 */
export interface PortraitCandidate {
  emotion: string
  path: string
  suffix: string
  bytes: number
  duplicated: boolean
}

export interface Memory {
  id: string
  friend_id: string
  content: string
  tag: 'normal' | 'rp' | 'rp_play_style' | 'notebook'
  layer: 'pinned' | 'permanent' | 'long_term' | 'short_term'
  context: string | null
  importance: number
  refreshed_at: string
  expires_at: string | null
  created_at: string
}

/** 记忆改动留痕（D230）：朋友 / 系统 / 用户动过哪条、旧值 → 新值。 */
export interface MemoryEvent {
  id: string
  memory_id: string | null
  actor: 'agent' | 'system' | 'user'
  action: 'create' | 'update' | 'delete'
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  created_at: string
}

export interface LlmBase {
  name: string
  base_url: string
  model: string
  protocol?: 'openai' | 'anthropic' | 'gemini'
  api_key_set?: boolean
  proxy?: string
  max_retries?: number
  capabilities?: Record<string, unknown>
}

export interface Settings {
  active_message_frequency: number
  deep_thinking: boolean
  show_tool_calls: boolean
  llm_active: string | null
  llm_bases: LlmBase[]
  /** v2（D80）：按用途的模型配置。api_key 已打码，只有 api_key_set。 */
  purposes: Record<string, PurposeConfig>
  /** v2（D83）：每个用途现在能不能用——前端拿它决定给不给入口。 */
  purposes_enabled: Record<string, boolean>
  memory_token_budget: number | null
  /** v2（D87）：定时预热（隔一会儿发个不花钱的空请求，把连接和上游捂热） */
  warmup_enabled: boolean
  warmup_interval_minutes: number
  /** v2.1（D99）：文件改动要不要逐笔点头（关掉 = 免授权） */
  file_write_approval: boolean
  /** v2.1（D100）+ v2.2（D103）：看屏（= 她主动找你说话之前先瞟一眼屏幕） */
  screen_watch_enabled: boolean
  auto_start: boolean
  /** v5.0（D255）：游戏里她主动说话的间隔（秒；0 = 关） */
  game_chat_interval_seconds: number
  /** v3.0（D141）：消息语音保留多少天（0 = 永久留着，只按总量上限清） */
  voice_keep_days: number
  /** v3.0（B9 / D142）：沉浸模式里"我"叫什么（两端一致） */
  my_name: string
  /** v7.0（D303）：当前负责主动消息 / 提醒的设备 id */
  autonomy_owner_device_id: string | null
  updated_at: string
}

/** v7.20（D373）：静态功能模块（设置 → 功能）。 */
export interface FeatureInfo {
  id: string
  name: string
  description: string
  category: string
  core: boolean
  default_enabled: boolean
  enabled: boolean
  ui_entries: string[]
}

/** v3.46（D231）：向量检索的状态（设置页「向量检索」卡片用）。 */
export interface EmbeddingStatus {
  enabled: boolean
  configured: boolean
  provider: 'local' | 'local_file' | 'openai'
  protocol: 'openai' | 'gemini'
  model: string
  dim: number
  ready: boolean
  cache_dir: string
  cache_mb: number
  indexed: number
  total: number
  min_similarity: number
  installed: boolean
  download: EmbeddingDownloadState
}

/** D450：资源页上的可选向量模型 / 下载进度。 */
export interface EmbeddingDownloadState {
  state: 'idle' | 'downloading' | 'verifying' | 'extracting' | 'done' | 'error'
  downloaded_bytes: number
  total_bytes: number
  percent: number
  error: string | null
  updated_at: string | null
  model: string
  dim: number
  size_mb: number
  url: string
  sha256: string
  installed: boolean
}

/** D456：桌面自动更新。 */
export interface UpdateDownloadState {
  state: 'idle' | 'downloading' | 'extracting' | 'ready' | 'applying' | 'error'
  progress: number
  downloaded_bytes: number
  total_bytes: number
  error: string | null
  updated_at: string | null
  staging_dir: string | null
}

export interface UpdateStatus {
  ok: boolean
  supported: boolean
  can_auto_update: boolean
  writable: boolean
  current_version: string
  latest_version: string
  update_available: boolean
  notes: string
  download_url: string
  sha256: string
  size: number
  resource_url: string
  error: string | null
  download: UpdateDownloadState
}

export interface UpdateHistoryItem {
  at: string
  event: 'downloaded' | 'applying' | 'applied' | 'failed' | string
  version: string
  from_version: string
  message?: string
}

/**
 * 一个用途的配置（v2，D80）。
 *
 * **只有 chat 是必须的**（D83）；其它没配 = 没这个能力，不是错误。
 * 形状有两套：
 *   · LLM 那套（chat / vision / stt / image）：base_url + model + api_key
 *   · 语音合成：engine + base_url（+ 音色、步数，T2.0-4 再加）
 */
export interface PurposeConfig {
  mode?: 'api' | 'local'
  base_url?: string
  model?: string
  protocol?: 'openai' | 'anthropic' | 'gemini'
  api_key_set?: boolean
  api_key?: string
  proxy?: string
  /** D450：embedding 的自由配置。 */
  provider?: 'local' | 'local_file' | 'openai'
  dim?: number
  min_similarity?: number
  cache_dir?: string
  allow_download?: boolean
  model_path?: string
  pooling?: 'mean' | 'cls'
  max_length?: number
  /** 只在没有 api_key 时出现（打码字段）。 */
  follow_chat?: boolean
  enabled?: boolean
  engine?: string
  sovits_dir?: string
  sample_steps?: number
  /** D129 图生图：参考图强度（像参考图多少；不填 = 0.7） */
  strength?: number
  default_voice_id?: string | null
}

export interface LogEntry {
  seq: number
  time: string
  level: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'
  where: string
  message: string
}

/** 小纸条 / 念头（D192）：有寿命的一次性惦记（跟长期记忆不是一回事）。 */
export interface Thought {
  id: string
  content: string
  kind: 'casual' | 'due'
  state: 'pending' | 'awaiting' | 'done' | 'expired'
  due_at: string | null
  said_at: string | null
  expires_at: string | null
  created_at: string
}

/** 日程（v4.0 / D243）：母表唯一；`date` / `time` 都是**本地值**（D244）。 */
export interface Schedule {
  id: string
  friend_id: string
  title: string
  nature: 'start' | 'due' | 'appointment' | 'remind'
  /** 本地日历日 YYYY-MM-DD */
  date: string
  /** 本地时刻 HH:MM；null = 只到日（不响，她找机会提） */
  time: string | null
  advance_minutes: number
  state: 'pending' | 'active' | 'settled' | 'archived'
  owner: 'user' | 'friend'
  source: string
  first_said_at: string | null
  settled_at: string | null
  settled_reason: string | null
  created_at: string
  updated_at: string | null
}

/** v2（T2.0-4）：一个音色 = 三个外部路径 + 台词（见 data_model.md 的【v2】4）。 */
export interface Voice {
  id: string
  name: string
  gpt_weights_path: string
  sovits_weights_path: string
  ref_audio_path: string
  prompt_text: string
  speed: number
  /** 三个文件现在还在吗（不在 → 界面标"路径失效"） */
  files_ok: boolean
  /** 有几个朋友在用（删之前提示用） */
  used_by: number
  created_at: string
  updated_at: string
}

/** 扫描出来的候选（还没入库）。 */
export interface VoiceCandidate {
  name: string
  gpt_weights_path: string
  sovits_weights_path: string
  ref_audio_path: string
  prompt_text: string
  speed: number
  already_imported: boolean
}

export interface TtsStatus {
  available: boolean
  engine: string
  base_url: string
  error: string | null
  sample_steps: number
}

/** v2.1（T2.1-2）：两个本地语音服务的状态。 */
export type SpeechServiceName = 'tts' | 'stt'

export interface SpeechService {
  state: 'stopped' | 'starting' | 'running'
  label: string
  port: number
  /** 是不是 yumeFriend 自己起的（不是的话「停止」要交给用户自己的窗口） */
  ours: boolean
  hint: string
}

/** v2.1（T2.1-3）：一份知识库资料（列表里**不带全文**）。 */
export interface KnowledgeDoc {
  id: string
  friend_id: string
  name: string
  kind: 'txt' | 'md' | 'pdf' | 'docx' | 'image'
  bytes: number
  chars: number
  status: 'ready' | 'failed'
  error: string | null
  created_at: string
  updated_at: string
}

export interface KnowledgeDocDetail extends KnowledgeDoc {
  text: string
}

export interface RecoverableFriend {
  id: string
  name: string
  deleted_at: string
}

/** 发消息时的流式事件（与后端 LLM协议§3.2 一一对应）。 */
export type ChatEvent =
  /** D110：流一开始就把两边消息 id 给前端（这样分段语音能**精确**绑到这一轮） */
  | { event: 'start'; userMessageId: string; assistantMessageId: string }
  | { event: 'content'; delta: string }
  /** D112/D115：她说完一段时给的表情标记 + 这一刻在干净文本里的偏移 */
  | { event: 'mood'; mood: string; at: number }
  | { event: 'reasoning'; delta: string }
  | { event: 'tool_call'; id: string; name: string; args: unknown }
  /** v6.0（D298）：她这一轮要分下一条短消息了 */
  | { event: 'segment_break' }
  | {
      event: 'done'
      userMessageId: string
      assistantMessageId: string
      /** 后端最终切好的气泡；没有走 segment_break 的段落也能在前端按节奏播放 */
      segments: string[]
      /** 与 segments 一一对齐的最终情绪；避免历史刷新后表情跳变 */
      moods: string[]
    }
  | { event: 'error'; code: string; message: string }

/** 请求超时（D196）：手机在外面走流量时，请求可能"发出去没人回" —— 不能无限等。 */
const REQUEST_TIMEOUT_MS = 15_000
/** 网络类错误的重试次数（4xx/5xx **不重试** —— 那是后端明确回答）。 */
const REQUEST_RETRIES = 2

function isNetworkError(exc: unknown): boolean {
  if (exc instanceof DOMException) return exc.name === 'AbortError'
  return exc instanceof TypeError // fetch 网络失败就是 TypeError
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let lastError: unknown = null
  for (let attempt = 0; attempt <= REQUEST_RETRIES; attempt += 1) {
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
    try {
      const res = await fetch(`/api${path}`, {
        headers: { 'Content-Type': 'application/json' },
        ...init,
        signal: controller.signal,
      })
      markOnline()
      return await parse<T>(res)
    } catch (exc) {
      if (!isNetworkError(exc)) throw exc
      // 网络断了 / 超时：**标记断开**（界面弹横幅）+ 退避重试
      // （用户实测："断一下就再也拉不下来" —— 以前一次失败就永远失败）
      lastError = exc
      markOffline()
      if (attempt < REQUEST_RETRIES) {
        await new Promise((resolve) => window.setTimeout(resolve, 400 * 2 ** attempt))
      }
    } finally {
      window.clearTimeout(timer)
    }
  }
  throw lastError instanceof Error ? lastError : new Error('网络断了')
}

/**
 * 传文件用这个：**不能自己设 Content-Type**（浏览器要带 boundary）。
 *
 * 2026-09-27（D215）：网络层失手（`fetch` reject，比如手机过隧道那一下被掐）**自动重传一次**
 * —— 用户看到的"经常传不上去"多半就是这种一次性抖动；重传还不行就给一句人话，
 * 而不是把 `Failed to fetch` 丢给用户。HTTP 4xx/5xx 是后端明确拒绝，**不重试**。
 */
async function upload<T>(path: string, file: File, field = 'file'): Promise<T> {
  const form = new FormData()
  form.append(field, file)
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const res = await fetch(`/api${path}`, { method: 'POST', body: form })
      return await parse<T>(res)
    } catch (exc) {
      if (!(exc instanceof TypeError)) throw exc // 后端说话了 → 原样抛
    }
  }
  throw new Error('传不上去：网络断了（离电脑近一点 / 换个网再点一次发送）')
}

async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { error?: { message?: string } } | null
    throw new Error(body?.error?.message ?? `HTTP ${res.status}`)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function toChatEvent(frame: SseFrame): ChatEvent | null {
  const d = frame.data
  switch (frame.event) {
    case 'start':
      return {
        event: 'start',
        userMessageId: String(d.user_message_id ?? ''),
        assistantMessageId: String(d.assistant_message_id ?? ''),
      }
    case 'content':
      return { event: 'content', delta: String(d.delta ?? '') }
    case 'mood':
      return { event: 'mood', mood: String(d.mood ?? ''), at: Number(d.at ?? 0) }
    case 'reasoning':
      return { event: 'reasoning', delta: String(d.delta ?? '') }
    case 'tool_call':
      return {
        event: 'tool_call',
        id: String(d.id ?? ''),
        name: String(d.name ?? ''),
        args: d.arguments,
      }
    case 'segment_break':
      return { event: 'segment_break' }
    case 'done':
      return {
        event: 'done',
        userMessageId: String(d.user_message_id ?? ''),
        assistantMessageId: String(d.assistant_message_id ?? ''),
        segments: Array.isArray(d.segments)
          ? d.segments.map((item) => String(item ?? '')).filter((item) => item.trim() !== '')
          : [],
        moods: Array.isArray(d.moods) ? d.moods.map((item) => String(item ?? '')) : [],
      }
    case 'error':
      return { event: 'error', code: String(d.code ?? 'ERROR'), message: String(d.message ?? '') }
    default:
      return null
  }
}

export const api = {
  syncStatus: (): Promise<{ ok: boolean; device: SyncDeviceInfo; session: SyncSessionInfo }> =>
    request('/sync/status'),

  syncStart: (body: { name?: string; kind?: string } = {}): Promise<SyncSessionInfo> =>
    request('/sync/start', { method: 'POST', body: JSON.stringify(body) }),

  syncStop: (): Promise<{ ok: boolean }> => request('/sync/stop', { method: 'POST' }),

  syncPeers: (): Promise<{ ok: boolean; peers: SyncPeer[] }> => request('/sync/peers'),

  syncRun: (
    peer: SyncPeer,
    code: string,
    direction: SyncDirection = 'both',
  ): Promise<SyncRunResult> =>
    request('/sync/run', {
      method: 'POST',
      body: JSON.stringify({ peer, code, direction }),
    }),

  serverSyncStatus: (): Promise<ServerSyncStatus> => request<ServerSyncStatus>('/sync/server'),

  setServerSync: (body: {
    mode: 'local' | 'server'
    base_url?: string
    token?: string
  }): Promise<ServerSyncStatus> =>
    request<ServerSyncStatus>('/sync/server', {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  testServerSync: (body: { base_url: string; token: string }): Promise<Record<string, unknown>> =>
    request<Record<string, unknown>>('/sync/server/test', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  serverSyncNow: (): Promise<Record<string, unknown>> =>
    request<Record<string, unknown>>('/sync/server/now', { method: 'POST' }),

  pauseServerSync: (): Promise<ServerSyncStatus> =>
    request<ServerSyncStatus>('/sync/server/pause', { method: 'POST' }),

  resumeServerSync: (): Promise<ServerSyncStatus> =>
    request<ServerSyncStatus>('/sync/server/resume', { method: 'POST' }),

  pushServerChat: (): Promise<Record<string, unknown>> =>
    request<Record<string, unknown>>('/sync/server/push-chat', { method: 'POST' }),

  pullServerChat: (): Promise<Record<string, unknown>> =>
    request<Record<string, unknown>>('/sync/server/pull-chat', { method: 'POST' }),

  pushServerMemories: (): Promise<Record<string, unknown>> =>
    request<Record<string, unknown>>('/sync/server/push-memories', { method: 'POST' }),

  pullServerMemories: (): Promise<Record<string, unknown>> =>
    request<Record<string, unknown>>('/sync/server/pull-memories', { method: 'POST' }),

  lifeStatus: (friendId: string): Promise<LifeStatus> =>
    request<LifeStatus>(`/life/${friendId}/status`),

  setLife: (friendId: string, enabled: boolean): Promise<LifeStatus> =>
    request<LifeStatus>(`/life/${friendId}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    }),

  speechStatus: (): Promise<SpeechStatus> => request('/speech/status'),

  listFriends: async (): Promise<Friend[]> =>
    (await request<{ friends: Friend[] }>('/friends')).friends,

  getFriend: (id: string): Promise<FriendDetail> => request<FriendDetail>(`/friends/${id}`),

  buildSpaceProfile: (
    friendId: string,
  ): Promise<{ ok: boolean; source: 'llm' | 'fallback'; profile: SpaceProfile; error: string }> =>
    request(`/friends/${friendId}/space-profile`, { method: 'POST' }),

  getSpace: (
    friendId: string,
  ): Promise<{
    ok: boolean
    state: SpaceState
    events: SpaceEvent[]
    runtime: SpaceRuntime
    knowledge: SpaceKnowledge[]
    world_model: SpaceWorldModel
  }> =>
    request<{
      ok: boolean
      state: SpaceState
      events: SpaceEvent[]
      runtime: SpaceRuntime
      knowledge: SpaceKnowledge[]
      world_model: SpaceWorldModel
    }>(
      `/friends/${friendId}/space`,
    ),

  patchSpace: (
    friendId: string,
    body: {
      layout?: Record<string, [number, number, number]>
      mode?: 'free' | 'together'
      current_action?: Record<string, unknown>
      prop_states?: Record<string, string>
      world?: {
        environment: 'room' | 'white'
        objects: {
          id: string
          asset: string
          position: [number, number, number]
          rotation: number
          scale: number
          state?: Record<string, unknown>
        }[]
      }
      appearance?: { outfit?: Partial<SpaceOutfit>; model?: string | null }
    },
  ): Promise<{ ok: boolean; state: SpaceState }> =>
    request<{ ok: boolean; state: SpaceState }>(`/friends/${friendId}/space`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  logSpaceEvent: (
    friendId: string,
    body: { kind: string; target?: string | null; detail?: Record<string, unknown>; notable?: boolean },
  ): Promise<{ ok: boolean; event: SpaceEvent }> =>
    request<{ ok: boolean; event: SpaceEvent }>(`/friends/${friendId}/space/events`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  thinkSpace: (
    friendId: string,
    text?: string,
    viewer?: SpaceViewerSnapshot,
  ): Promise<{
    ok: boolean
    source: 'local' | 'llm'
    thought?: string
    emotion?: string
    line: string | null
    action: SpaceAction
    runtime?: SpaceRuntime
    knowledge?: SpaceKnowledge[]
    discovery?: SpaceDiscovery
    awaiting_reply?: boolean
  }> =>
    request<{
      ok: boolean
      source: 'local' | 'llm'
      thought?: string
      emotion?: string
      line: string | null
      action: SpaceAction
      runtime?: SpaceRuntime
      knowledge?: SpaceKnowledge[]
      discovery?: SpaceDiscovery
      awaiting_reply?: boolean
    }>(
      `/friends/${friendId}/space/think`,
      {
        method: 'POST',
        body: JSON.stringify({
          ...(text ? { text } : {}),
          ...(viewer ? { viewer } : {}),
        }),
      },
    ),

  streamSpaceChat: async function* (
    friendId: string,
    text: string,
    viewer?: SpaceViewerSnapshot,
  ): AsyncGenerator<SpaceChatStreamEvent> {
    const response = await fetch(`/api/friends/${friendId}/space/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        ...(viewer ? { viewer } : {}),
      }),
    })
    if (!response.ok) {
      throw new Error(
        (await response.text().catch(() => '')).trim() || '小世界流式对话失败',
      )
    }
    markOnline()
    for await (const frame of readSse(response)) {
      yield frame.data as unknown as SpaceChatStreamEvent
    }
  },

  perceiveSpace: (
    friendId: string,
    body: {
      viewer: SpaceViewerSnapshot
      self_state: SpaceSelfState
      objects: SpacePerceptionObject[]
      paused: boolean
      personality_curiosity: number
      activity_kind?: string
      activity_target?: string
      activity_label?: string
      mood?: string | null
    },
  ): Promise<{
    ok: boolean
    runtime: SpaceRuntime
    knowledge: SpaceKnowledge[]
    line: string | null
    action: SpaceAction | null
    ask_intent: SpaceAskIntent | null
    deliberation_due: boolean
    focus: SpaceDeliberationFocus | null
    discovery: SpaceDiscovery
  }> =>
    request<{
      ok: boolean
      runtime: SpaceRuntime
      knowledge: SpaceKnowledge[]
      line: string | null
      action: SpaceAction | null
      ask_intent: SpaceAskIntent | null
      deliberation_due: boolean
      focus: SpaceDeliberationFocus | null
      discovery: SpaceDiscovery
    }>(`/friends/${friendId}/space/perceive`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  deliberateSpace: (
    friendId: string,
    body: {
      viewer: SpaceViewerSnapshot
      self_state: SpaceSelfState
      objects: SpacePerceptionObject[]
      paused: boolean
      personality_curiosity: number
      activity_kind?: string
      activity_target?: string
      activity_label?: string
      mood?: string | null
    },
  ): Promise<{
    ok: boolean
    source: 'local' | 'llm'
    thought: string
    reflection?: string
    preference?: string
    emotion?: string
    line: string | null
    action: SpaceAction | null
    ask_intent: SpaceAskIntent | null
    next_think_in: number
    awaiting_reply?: boolean
    discovery: SpaceDiscovery
    runtime: SpaceRuntime
    knowledge: SpaceKnowledge[]
  }> =>
    request<{
      ok: boolean
      source: 'local' | 'llm'
      thought: string
      reflection?: string
      preference?: string
      emotion?: string
      line: string | null
      action: SpaceAction | null
      ask_intent: SpaceAskIntent | null
      next_think_in: number
      awaiting_reply?: boolean
      discovery: SpaceDiscovery
      runtime: SpaceRuntime
      knowledge: SpaceKnowledge[]
    }>(`/friends/${friendId}/space/deliberate`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  askSpace: (
    friendId: string,
    objectKey: string,
    viewer?: SpaceViewerSnapshot,
    context?: {
      activity_kind?: string
      activity_target?: string
      activity_label?: string
      mood?: string | null
    },
  ): Promise<{
    ok: boolean
    source: 'local' | 'llm'
    line: string
    action: SpaceAction
    discovery: SpaceDiscovery
  }> =>
    request<{
      ok: boolean
      source: 'local' | 'llm'
      line: string
      action: SpaceAction
      discovery: SpaceDiscovery
    }>(`/friends/${friendId}/space/ask`, {
      method: 'POST',
      body: JSON.stringify({
        object_key: objectKey,
        ...(viewer ? { viewer } : {}),
        ...(context ?? {}),
      }),
    }),

  reportSpaceActionResult: (
    friendId: string,
    actionId: string,
    ok: boolean,
    detail = '',
  ): Promise<{ ok: boolean; runtime: SpaceRuntime; knowledge: SpaceKnowledge[] }> =>
    request<{ ok: boolean; runtime: SpaceRuntime; knowledge: SpaceKnowledge[] }>(
      `/friends/${friendId}/space/action-result`,
      {
        method: 'POST',
        body: JSON.stringify({ action_id: actionId, ok, detail }),
      },
    ),

  reactSpace: (
    friendId: string,
    body: {
      zone: string
      viewer: SpaceViewerSnapshot
      self_state: SpaceSelfState
      objects: SpacePerceptionObject[]
      activity_label?: string
      mood?: string | null
      interaction?: 'tap' | 'grab' | 'hold' | 'push' | 'pull' | 'drag'
      sustained_seconds?: number
    },
  ): Promise<{
    ok: boolean
    source: 'local' | 'llm'
    thought: string
    emotion: string
    line: string | null
    action: SpaceAction | null
    runtime: SpaceRuntime
    awaiting_reply: boolean
    busy?: boolean
    discovery: SpaceDiscovery
  }> =>
    request<{
      ok: boolean
      source: 'local' | 'llm'
      thought: string
      emotion: string
      line: string | null
      action: SpaceAction | null
      runtime: SpaceRuntime
      awaiting_reply: boolean
      busy?: boolean
      discovery: SpaceDiscovery
    }>(`/friends/${friendId}/space/react`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  uploadSpaceAsset: (
    friendId: string,
    file: File,
  ): Promise<{ ok: boolean; asset: string; url: string }> =>
    upload<{ ok: boolean; asset: string; url: string }>(
      `/friends/${friendId}/space/assets`,
      file,
    ),

  uploadAvatarModel: (
    friendId: string,
    file: File,
  ): Promise<{ ok: boolean; model_url: string }> =>
    upload<{ ok: boolean; model_url: string }>(
      `/friends/${friendId}/avatar-model`,
      file,
    ),

  deleteAvatarModel: (friendId: string): Promise<{ ok: boolean }> =>
    request<{ ok: boolean }>(`/friends/${friendId}/avatar-model`, {
      method: 'DELETE',
    }),

  getStyleProfile: (friendId: string): Promise<StyleProfileResponse> =>
    request<StyleProfileResponse>(`/friends/${friendId}/style`),

  saveStyleChanges: (
    friendId: string,
    changes: StyleChangeInput[],
    summary?: string,
  ): Promise<{ ok: boolean; version: number; applied: unknown[]; profile: StyleProfile }> =>
    request<{ ok: boolean; version: number; applied: unknown[]; profile: StyleProfile }>(
      `/friends/${friendId}/style/changes`,
      {
      method: 'POST',
      body: JSON.stringify({ changes, summary }),
      },
    ),

  listStyleEvents: (friendId: string, limit = 20): Promise<{ events: StyleEvent[] }> =>
    request<{ events: StyleEvent[] }>(`/friends/${friendId}/style/events?limit=${limit}`),

  rollbackStyle: (
    friendId: string,
    eventId: string,
  ): Promise<{ ok: boolean; version: number; event_id: string; profile: StyleProfile }> =>
    request<{ ok: boolean; version: number; event_id: string; profile: StyleProfile }>(
      `/friends/${friendId}/style/rollback`,
      {
      method: 'POST',
      body: JSON.stringify({ event_id: eventId }),
      },
    ),

  resetStyle: (friendId: string): Promise<{ ok: boolean; version: number; event_id: string }> =>
    request(`/friends/${friendId}/style`, { method: 'DELETE' }),

  createFriend: (body: FriendCreateBody): Promise<FriendDetail> =>
    request<FriendDetail>('/friends', { method: 'POST', body: JSON.stringify(body) }),

  setPresence: (id: string, presence: boolean): Promise<FriendDetail> =>
    request<FriendDetail>(`/friends/${id}/presence`, {
      method: 'PATCH',
      body: JSON.stringify({ presence }),
    }),

  setMode: (id: string, mode: string, role: string | null = null) =>
    request<{ friend_id: string; current_mode: string; current_role: string | null }>(
      `/friends/${id}/mode`,
      { method: 'PATCH', body: JSON.stringify({ mode, role }) },
    ),

  softDelete: (id: string): Promise<void> =>
    request<void>(`/friends/${id}`, { method: 'DELETE' }),

  /** 清零未读（api.md §1.5）：正在看着这个朋友时用。 */
  markRead: (id: string): Promise<void> =>
    request<void>(`/friends/${id}/read`, { method: 'POST' }),

  /**
   * v3.14（D182）：**真的**把这一轮从后台掐掉。
   *
   * 以前"停止"就是把这条 SSE 断开 —— 现在断开只等于"我不看了"，她照说
   * （关掉窗口也继续说）。所以停止必须显式告诉后端一声。
   */
  stopMessage: (friendId: string, messageId: string): Promise<{ ok: boolean; stopped: boolean }> =>
    request<{ ok: boolean; stopped: boolean }>(
      `/friends/${friendId}/messages/${messageId}/stop`,
      { method: 'POST' },
    ),

  /** 改名字 / 人格（api.md §1.2；A3 的"改人格"入口）。 */
  updateFriend: (
    id: string,
    body: {
      name?: string
      personality?: string
      custom_instructions?: string
      /** v6.0（D298）：传空数组 = 清空；不传 = 这次不改 */
      voice_examples?: string[]
      avatar_description?: string
      voice_id?: string
      voice_reply_enabled?: boolean
      portrait_transform?: { scale: number; x: number; y: number }
      /** v3.5（D151）：传空字符串 = 跟随全局「聊天」用途 */
      llm_base?: string
      /** v4.3（D249）：她的温度（0.0~1.5）。**给 null = 恢复默认**；不传 = 这次不改 */
      temperature?: number | null
      /** v7.12（D341）：生活线开关 */
      life_enabled?: boolean
      /** v7.13（D344）：轻聊/深聊分流 */
      chat_pacing_enabled?: boolean
      /** v7.13（D344）：表达学习 + 低概率错字 */
      humanizer_enabled?: boolean
      visual_mode?: 'live2d' | 'portrait'
      live2d_model_id?: string | null
      portrait_pack_id?: string | null
    },
  ): Promise<FriendDetail> =>
    request<FriendDetail>(`/friends/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  /** v5.0（P3）：游戏陪伴 */
  gameStatus: (): Promise<GameStatus> => request<GameStatus>('/game/status'),

  /** v7.7.1（D331）：真实 LLM 用量统计。 */
  usageSummary: (days = 1, recentLimit = 0): Promise<UsageSummary> =>
    request<UsageSummary>(`/usage/summary?days=${days}&recent_limit=${recentLimit}`),

  gameStart: (body: GameStartBody): Promise<GameStatus> =>
    request<GameStatus>('/game/start', { method: 'POST', body: JSON.stringify(body) }),

  getMinecraftVersions: (force = false): Promise<MinecraftVersionStatus> =>
    request<MinecraftVersionStatus>(`/game/minecraft/versions?force=${force ? 'true' : 'false'}`),

  downloadMinecraftVersion: (version: string): Promise<MinecraftVersionDownloadState> =>
    request<MinecraftVersionDownloadState>(
      `/game/minecraft/versions/${encodeURIComponent(version)}/download`,
      { method: 'POST' },
    ),

  minecraftVersionDownloadStatus: (
    version?: string,
  ): Promise<MinecraftVersionDownloadState> =>
    request<MinecraftVersionDownloadState>(
      `/game/minecraft/download-status${version ? `?version=${encodeURIComponent(version)}` : ''}`,
    ),

  deleteMinecraftVersion: (version: string): Promise<{ ok: boolean; deleted: boolean }> =>
    request<{ ok: boolean; deleted: boolean }>(
      `/game/minecraft/versions/${encodeURIComponent(version)}`,
      { method: 'DELETE' },
    ),

  probeMinecraft: (
    host: string,
    port: number,
  ): Promise<{ ok: boolean; server_version: string; protocol: number | null; latency_ms: number }> =>
    request('/game/minecraft/probe', {
      method: 'POST',
      body: JSON.stringify({ host, port }),
    }),

  gameStop: (): Promise<{ ok: boolean }> =>
    request<{ ok: boolean }>('/game/stop', { method: 'POST' }),

  gameCommand: (action: string, args: Record<string, unknown> = {}) =>
    request<{ accepted: boolean; task_id: string }>('/game/command', {
      method: 'POST',
      body: JSON.stringify({ action, args }),
    }),

  gameSnapshot: (): Promise<GameSnapshot> => request<GameSnapshot>('/game/snapshot'),

  /** v5.1（D290）：作弊 / 难度配置。persist=true 才会写进“记住”。 */
  gameCheats: (): Promise<GameCheatConfig> => request<GameCheatConfig>('/game/cheats'),

  setGameCheats: (
    body: Partial<Pick<GameCheatConfig, 'profile' | 'master_enabled' | 'abilities'>> & {
      persist?: boolean
    },
  ): Promise<GameCheatConfig> =>
    request<GameCheatConfig>('/game/cheats', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  gameReflexes: (): Promise<GameReflexConfig> =>
    request<GameReflexConfig>('/game/reflexes'),

  setGameReflexes: (body: { modes: Record<string, GameReflexMode> }): Promise<GameReflexConfig> =>
    request<GameReflexConfig>('/game/reflexes', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  gameSkills: (
    friendId: string,
  ): Promise<{ skills: GameSkill[]; failures: GameSkillFailure[] }> =>
    request(`/game/skills?friend_id=${encodeURIComponent(friendId)}`),

  setGameSkillEnabled: (
    skillId: string,
    friendId: string,
    enabled: boolean,
  ): Promise<{ skill: GameSkill }> =>
    request(`/game/skills/${encodeURIComponent(skillId)}/enabled?friend_id=${encodeURIComponent(friendId)}`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),

  deleteGameSkill: (skillId: string, friendId: string): Promise<{ ok: boolean }> =>
    request(
      `/game/skills/${encodeURIComponent(skillId)}?friend_id=${encodeURIComponent(friendId)}`,
      { method: 'DELETE' },
    ),

  runGameSkill: (
    skillId: string,
    friendId: string,
    params: Record<string, unknown> = {},
  ): Promise<{ ok: boolean; skill: string; steps: unknown[]; error: string; reason: string }> =>
    request(`/game/skills/${encodeURIComponent(skillId)}/run?friend_id=${encodeURIComponent(friendId)}`, {
      method: 'POST',
      body: JSON.stringify({ params }),
    }),

  gameBlueprints: (
    friendId: string,
  ): Promise<{
    blueprints: GameBlueprint[]
    creative: GameCreativeRecord[]
    job: Record<string, unknown> | null
  }> => request(`/game/blueprints?friend_id=${encodeURIComponent(friendId)}`),

  buildGameBlueprint: (
    blueprintId: string,
    friendId: string,
    anchor?: { x: number; y: number; z: number },
  ): Promise<Record<string, unknown>> =>
    request(
      `/game/blueprints/${encodeURIComponent(blueprintId)}/build?friend_id=${encodeURIComponent(friendId)}`,
      {
        method: 'POST',
        body: JSON.stringify(anchor ? { anchor } : {}),
      },
    ),

  creativeStart: (
    friendId: string,
    body: Record<string, unknown>,
  ): Promise<Record<string, unknown>> =>
    request(`/game/creative/start?friend_id=${encodeURIComponent(friendId)}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  creativeFinish: (
    recordId: string,
    friendId: string,
    rating?: number,
  ): Promise<Record<string, unknown>> =>
    request(
      `/game/creative/${encodeURIComponent(recordId)}/finish?friend_id=${encodeURIComponent(friendId)}`,
      {
        method: 'POST',
        body: JSON.stringify(rating ? { rating } : {}),
      },
    ),

  rateCreative: (
    recordId: string,
    friendId: string,
    rating: number,
  ): Promise<{ record: GameCreativeRecord }> =>
    request(`/game/creative/${encodeURIComponent(recordId)}/rate?friend_id=${encodeURIComponent(friendId)}`, {
      method: 'POST',
      body: JSON.stringify({ rating }),
    }),

  /** 已删除的朋友（api.md §1 的 /recoverable，30 天内可恢复）。 */
  listRecoverable: async (): Promise<RecoverableFriend[]> =>
    (await request<{ recoverable: RecoverableFriend[] }>('/friends/recoverable')).recoverable,

  restoreFriend: (id: string): Promise<FriendDetail> =>
    request<FriendDetail>(`/friends/${id}/restore`, { method: 'POST' }),

  /** 彻底删除（物理删，连带消息/记忆/提醒 —— 不可逆）。 */
  purgeFriend: (id: string): Promise<void> =>
    request<void>(`/friends/${id}/purge`, { method: 'POST' }),

  /** 日程（api.md【v4.0】/ D243）。 */
  listSchedules: async (
    friendId: string,
    options: { includeArchived?: boolean } = {},
  ): Promise<Schedule[]> =>
    (
      await request<{ schedules: Schedule[] }>(
        `/schedules?friend_id=${friendId}&include_archived=${options.includeArchived ?? false}`,
      )
    ).schedules,

  createSchedule: (body: {
    friend_id: string
    title: string
    date: string
    time?: string | null
    nature?: string
    owner?: string
  }): Promise<Schedule> =>
    request<Schedule>('/schedules', { method: 'POST', body: JSON.stringify(body) }),

  updateSchedule: (id: string, body: Record<string, unknown>): Promise<Schedule> =>
    request<Schedule>(`/schedules/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  deleteSchedule: (id: string): Promise<void> =>
    request<void>(`/schedules/${id}`, { method: 'DELETE' }),

  /** 历史分页（api.md §2.2）；返回按时间正序。 */
  listMessages: (
    friendId: string,
    options: { limit?: number; before?: string } = {},
  ): Promise<{ messages: Message[]; has_more: boolean }> => {
    const params = new URLSearchParams()
    params.set('limit', String(options.limit ?? 50))
    if (options.before) params.set('before', options.before)
    return request<{ messages: Message[]; has_more: boolean }>(
      `/friends/${friendId}/messages?${params.toString()}`,
    )
  },

  /** 发消息：唯一入口就是这条 SSE 流（api.md §2.1）。 */
  async *streamMessage(
    friendId: string,
    content: string,
    signal: AbortSignal,
    imageId?: string,
    screen?: boolean,
  ): AsyncGenerator<ChatEvent> {
    const body: Record<string, unknown> = { content }
    if (imageId) body.image_id = imageId
    if (screen) body.screen = true
    const res = await fetch(`/api/friends/${friendId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    })
    if (!res.ok) {
      const body = (await res.json().catch(() => null)) as { error?: { message?: string } } | null
      yield { event: 'error', code: String(res.status), message: body?.error?.message ?? `HTTP ${res.status}` }
      return
    }
    for await (const frame of readSse(res)) {
      const event = toChatEvent(frame)
      if (event) yield event
    }
  },

  getSettings: (): Promise<Settings> => request<Settings>('/settings'),

  // ── D440：Live2D 模型级设置 ─────────────────────────────────────
  listLive2DModels: async (): Promise<Live2DModelSummary[]> =>
    (await request<{ models: Live2DModelSummary[] }>('/live2d/models')).models,

  getLive2DModelMeta: (modelId: string): Promise<Live2DModelMeta> =>
    request<Live2DModelMeta>(`/live2d/models/${encodeURIComponent(modelId)}/meta`),

  saveLive2DModelSettings: (
    modelId: string,
    settings: Live2DModelSettings,
  ): Promise<{ ok: boolean; settings: Live2DModelSettings }> =>
    request<{ ok: boolean; settings: Live2DModelSettings }>(
      `/live2d/models/${encodeURIComponent(modelId)}/settings`,
      { method: 'PUT', body: JSON.stringify(settings) },
    ),

  importLive2DModel: (
    file: File,
  ): Promise<{ ok: boolean; model: { id: string; name: string; model_file: string } }> =>
    upload('/live2d/models/import', file),

  getLive2DRuntimeStatus: (): Promise<Live2DRuntimeStatus> =>
    request<Live2DRuntimeStatus>('/live2d/runtime/status'),

  downloadLive2DRuntime: async (
    onProgress: (progress: Live2DRuntimeProgress) => void,
  ): Promise<{ size: number; sha256: string }> => {
    const response = await fetch('/api/live2d/runtime/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ accepted_license: true }),
    })
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as {
        detail?: string
        error?: { message?: string }
      } | null
      throw new Error(body?.detail ?? body?.error?.message ?? `HTTP ${response.status}`)
    }
    if (!response.body) throw new Error('浏览器没有返回下载流')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let result: { size: number; sha256: string } | null = null

    const handleLine = (line: string) => {
      if (!line.trim()) return
      const event = JSON.parse(line) as {
        type?: string
        received?: number
        total?: number
        size?: number
        sha256?: string
        message?: string
      }
      if (event.type === 'progress') {
        onProgress({
          received: Number(event.received ?? 0),
          total: Number(event.total ?? 0),
        })
      } else if (event.type === 'done') {
        result = {
          size: Number(event.size ?? 0),
          sha256: String(event.sha256 ?? ''),
        }
      } else if (event.type === 'error') {
        throw new Error(event.message || 'Live2D 运行时下载失败')
      }
    }

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) handleLine(line)
    }
    buffer += decoder.decode()
    if (buffer.trim()) handleLine(buffer)
    if (!result) throw new Error('下载结束，但没有收到完成确认')
    return result
  },

  getFeatures: (): Promise<{ ok: boolean; features: FeatureInfo[] }> =>
    request<{ ok: boolean; features: FeatureInfo[] }>('/features'),

  setFeature: (
    featureId: string,
    enabled: boolean,
  ): Promise<{ ok: boolean; feature: FeatureInfo }> =>
    request<{ ok: boolean; feature: FeatureInfo }>(`/features/${encodeURIComponent(featureId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    }),

  patchSettings: (body: Record<string, unknown>): Promise<Settings> =>
    request<Settings>('/settings', { method: 'PATCH', body: JSON.stringify(body) }),

  testProvider: (name: string): Promise<{ ok: boolean; error?: string; latency_ms?: number }> =>
    request(`/settings/providers/${encodeURIComponent(name)}/test`, { method: 'POST' }),

  /** v2：按用途测连通。可以把"还没保存的草稿"一起传上去（api.md §5.3）。 */
  testPurpose: (
    purpose: string,
    config?: Record<string, unknown>,
  ): Promise<{ ok: boolean; error?: string; latency_ms?: number; model_echo?: string }> =>
    request('/settings/test', {
      method: 'POST',
      body: JSON.stringify(config ? { purpose, config } : { purpose }),
    }),

  setupStatus: (): Promise<{ needs_setup: boolean; missing: string[] }> =>
    request('/settings/setup_status'),

  /** v3.46（D231）：向量检索状态（不加载模型）。 */
  embeddingStatus: (): Promise<EmbeddingStatus> =>
    request<EmbeddingStatus>('/embedding/status'),

  /** v3.46（D231）：重建向量索引（会加载模型；跑在服务端线程里）。 */
  rebuildEmbedding: (): Promise<
    EmbeddingStatus & { ok: boolean; added: number; error?: string }
  > => request('/embedding/backfill', { method: 'POST' }),
  embeddingModel: (): Promise<EmbeddingDownloadState> =>
    request<EmbeddingDownloadState>('/embedding/model'),
  downloadEmbeddingModel: (): Promise<EmbeddingDownloadState> =>
    request<EmbeddingDownloadState>('/embedding/model/download', { method: 'POST' }),
  uploadEmbeddingModel: async (
    files: File[],
  ): Promise<{ ok: boolean; path: string; files: string[] }> => {
    const form = new FormData()
    for (const file of files) {
      const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath
      form.append('files', file, relative || file.name)
    }
    const res = await fetch('/api/embedding/local-model/upload', {
      method: 'POST',
      body: form,
    })
    return parse<{ ok: boolean; path: string; files: string[] }>(res)
  },
  updateStatus: (force = false): Promise<UpdateStatus> =>
    request<UpdateStatus>(`/update/status${force ? '?force=true' : ''}`),
  updateDownload: (): Promise<UpdateStatus> =>
    request<UpdateStatus>('/update/download', { method: 'POST' }),
  updateApply: (): Promise<UpdateStatus & { ok: boolean; error?: string }> =>
    request<UpdateStatus & { ok: boolean; error?: string }>('/update/apply', {
      method: 'POST',
    }),
  updateHistory: (): Promise<{ items: UpdateHistoryItem[] }> =>
    request<{ items: UpdateHistoryItem[] }>('/update/history'),

  /** v2：换头像（服务端会裁方 + 压到 256）。 */
  uploadAvatar: (id: string, file: File): Promise<FriendDetail> =>
    upload<FriendDetail>(`/friends/${id}/avatar`, file),

  // ── v2.2 立绘 / 背景（T2.2-3 / D102 / D107）────────────────
  listPortraits: async (friendId: string): Promise<Portrait[]> =>
    (await request<{ portraits: Portrait[] }>(`/friends/${friendId}/portraits`)).portraits,

  /** v7.0（D305）：手机/平板多选立绘，情绪名逐项可改。 */
  uploadPortraits: async (
    friendId: string,
    items: { file: File; emotion: string }[],
  ): Promise<{ imported: { emotion: string; size: number }[] }> => {
    const form = new FormData()
    for (const item of items) form.append('files', item.file)
    form.append('emotions', JSON.stringify(items.map((item) => item.emotion)))
    const res = await fetch(`/api/friends/${friendId}/portraits/batch`, {
      method: 'POST',
      body: form,
    })
    return parse(res)
  },

  /** 扫一个立绘目录（也可以填它的上一级，后端会自动往下找），**只列不导**。 */
  scanPortraits: async (
    friendId: string,
    dir: string,
  ): Promise<{ candidates: PortraitCandidate[]; usedDir: string }> => {
    const body = await request<{ candidates: PortraitCandidate[]; used_dir?: string }>(
      `/friends/${friendId}/portraits/scan`,
      { method: 'POST', body: JSON.stringify({ dir }) },
    )
    return { candidates: body.candidates, usedDir: body.used_dir ?? '' }
  },

  importPortraits: (
    friendId: string,
    body: { dir: string; emotions?: string[]; use_avatar?: boolean },
  ): Promise<{ imported: number; skipped: number; emotions: string[]; avatar_set?: boolean }> =>
    request(`/friends/${friendId}/portraits/import`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  deletePortrait: async (friendId: string, emotion: string): Promise<void> => {
    await fetch(`/api/friends/${friendId}/portraits/${encodeURIComponent(emotion)}`, {
      method: 'DELETE',
    })
  },

  // ── v7.43（D441）：全局立绘包 ───────────────────────────────────
  listPortraitPacks: async (): Promise<PortraitPack[]> =>
    (await request<{ packs: PortraitPack[] }>('/portrait-packs')).packs,

  scanPortraitPack: async (
    dir: string,
  ): Promise<{ candidates: PortraitCandidate[]; usedDir: string }> => {
    const body = await request<{ candidates: PortraitCandidate[]; used_dir?: string }>(
      '/portrait-packs/scan',
      { method: 'POST', body: JSON.stringify({ dir }) },
    )
    return { candidates: body.candidates, usedDir: body.used_dir ?? '' }
  },

  importPortraitPack: (
    name: string,
    dir: string,
  ): Promise<{ pack: PortraitPack; imported: string[] }> =>
    request('/portrait-packs/import', {
      method: 'POST',
      body: JSON.stringify({ name, dir }),
    }),

  deletePortraitPack: async (packId: string): Promise<void> => {
    await request(`/portrait-packs/${encodeURIComponent(packId)}`, { method: 'DELETE' })
  },

  /** 沉浸模式的背景图（服务端自动 16:9 居中裁 + 压到 1920）。 */
  uploadBackground: (id: string, file: File): Promise<Friend> =>
    upload<Friend>(`/friends/${id}/background`, file),

  deleteBackground: (id: string): Promise<Friend> =>
    request<Friend>(`/friends/${id}/background`, { method: 'DELETE' }),

  /**
   * v2（T2.0-6）：传一张图（≤10MB，最长边超 2048 会缩小）。发送消息时带 image_id。
   *
   * 2026-09-27（D215）：**先在本机压一下再传** —— 手机相册里那种 6~20 MB 的图
   * 会被后端 10 MB 的上限挡下，走组网隧道也慢、也容易断。桌面和手机是同一个入口。
   */
  uploadImage: async (file: File): Promise<{
    id: string
    url: string
    width: number
    height: number
  }> => upload('/uploads/image', await shrinkImage(file)),

  // ── v2.1 知识库（T2.1-3 / D98）────────────────────────────
  listDocuments: async (friendId: string): Promise<KnowledgeDoc[]> =>
    (await request<{ documents: KnowledgeDoc[] }>(`/friends/${friendId}/documents`)).documents,

  getDocument: (friendId: string, docId: string): Promise<KnowledgeDocDetail> =>
    request<KnowledgeDocDetail>(`/friends/${friendId}/documents/${docId}`),

  /** 传一份资料（txt/md/pdf/docx/图片）；抽不出文字也会 200，看 status/error。 */
  uploadDocument: (friendId: string, file: File): Promise<KnowledgeDoc> =>
    upload<KnowledgeDoc>(`/friends/${friendId}/documents`, file),

  renameDocument: (friendId: string, docId: string, name: string): Promise<KnowledgeDoc> =>
    request<KnowledgeDoc>(`/friends/${friendId}/documents/${docId}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    }),

  /** 手改抽取出来的文字（改完后端重新切块；原件不动）。 */
  updateDocumentText: (friendId: string, docId: string, text: string): Promise<KnowledgeDoc> =>
    request<KnowledgeDoc>(`/friends/${friendId}/documents/${docId}`, {
      method: 'PATCH',
      body: JSON.stringify({ text }),
    }),

  deleteDocument: (friendId: string, docId: string): Promise<void> =>
    request<void>(`/friends/${friendId}/documents/${docId}`, { method: 'DELETE' }),

  // ── v2.1 工具审批（T2.1-4 / D99）──────────────────────────
  resolveApproval: (requestId: string, allow: boolean): Promise<{ ok: boolean }> =>
    request<{ ok: boolean }>(`/approvals/${requestId}`, {
      method: 'POST',
      body: JSON.stringify({ allow }),
    }),

  // ── v2.1 看屏 / focus（T2.1-5 / D100）─────────────────────
  /** 告诉后端"我现在开着谁"（切朋友时 + 每 60 秒心跳；没人开着传 null）。 */
  setFocus: (friendId: string | null): Promise<{ friend_id: string | null; fresh: boolean }> =>
    request('/focus', { method: 'POST', body: JSON.stringify({ friend_id: friendId }) }),

  focusState: (): Promise<{
    friend_id: string | null
    fresh: boolean
    enabled: boolean
    interval: [number, number]
  }> => request('/focus'),

  /** v2：删头像 → 界面回退成首字母色块。 */
  deleteAvatar: (id: string): Promise<FriendDetail> =>
    request<FriendDetail>(`/friends/${id}/avatar`, { method: 'DELETE' }),

  /** v2：重新生成「她的样子」（没配视觉模型会回 ok:false + 一句人话，不是错误）。 */
  describeAvatar: (id: string): Promise<FriendDetail & { ok?: boolean; hint?: string }> =>
    request<FriendDetail & { ok?: boolean; hint?: string }>(`/friends/${id}/avatar/describe`, {
      method: 'POST',
    }),

  // ── v2 音色库 + 语音服务（T2.0-4）────────────────────────────
  listVoices: (): Promise<{ voices: Voice[] }> => request('/voices'),

  /** 扫 GPT-SoVITS 目录（**不落库**）。认不出会回 ok:false + 一句人话。 */
  scanVoices: (
    sovitsDir?: string,
  ): Promise<{
    ok: boolean
    hint?: string
    sovits_dir?: string
    /** 扫描成功时，后端顺手把目录记下来了（省得用户忘了按保存） */
    saved_dir?: boolean
    candidates?: VoiceCandidate[]
  }> =>
    request('/voices/scan', {
      method: 'POST',
      body: JSON.stringify(sovitsDir ? { sovits_dir: sovitsDir } : {}),
    }),

  createVoice: (body: {
    name: string
    gpt_weights_path: string
    sovits_weights_path: string
    ref_audio_path: string
    prompt_text: string
    speed?: number
  }): Promise<Voice> => request('/voices', { method: 'POST', body: JSON.stringify(body) }),

  patchVoice: (
    id: string,
    body: { name?: string; speed?: number; prompt_text?: string },
  ): Promise<Voice> =>
    request(`/voices/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),

  deleteVoice: (id: string): Promise<void> =>
    request(`/voices/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** 试听：服务没开会回 ok:false + 人话提示（不是错误）。 */
  previewVoice: (
    id: string,
  ): Promise<{ ok: boolean; hint?: string; audio_url?: string; bytes?: number }> =>
    request(`/voices/${encodeURIComponent(id)}/preview`, { method: 'POST' }),

  ttsStatus: (): Promise<TtsStatus> => request('/tts/status'),

  // ── v2.1 语音输入 + 服务启停（T2.1-2）────────────────────────
  speechServices: (): Promise<Record<SpeechServiceName, SpeechService>> =>
    request('/speech/services'),

  startSpeechService: (
    name: SpeechServiceName,
  ): Promise<{ ok: boolean; state?: string; hint?: string }> =>
    request(`/speech/services/${name}/start`, { method: 'POST' }),

  stopSpeechService: (
    name: SpeechServiceName,
  ): Promise<{ ok: boolean; state?: string; hint?: string }> =>
    request(`/speech/services/${name}/stop`, { method: 'POST' }),

  /** 录一段音频 → 文字（服务没开会自动懒启动）。失败也是 200 + ok:false + 人话。 */
  transcribeAudio: (
    file: File,
  ): Promise<{ ok: boolean; text?: string; hint?: string }> => upload('/speech/transcribe', file),

  /**
   * v3.1：**手动画一张**（手机上那个「画图」面板）。
   * 跟工具 draw_image 同一条路；`refImageId` 是刚上传的参考图（图生图用）。
   */
  drawImage: (
    friendId: string,
    prompt: string,
    refImageId?: string,
  ): Promise<{ message: Message; model?: string }> =>
    request(`/friends/${friendId}/draw_image`, {
      method: 'POST',
      body: JSON.stringify(refImageId ? { prompt, ref_image_id: refImageId } : { prompt }),
    }),

  /** v2（T2.0-5）：「念给我听」。幂等；服务没开会回 ok:false + 人话。 */
  speakMessage: (
    friendId: string,
    messageId: string,
    force = false,
  ): Promise<{
    ok: boolean
    hint?: string
    /** 这条只有动作 / 心理描写，本来就没话可念（前端不再给重试入口） */
    nothing_to_say?: boolean
    audio_status?: string
    audio_url?: string
    audio_duration_ms?: number | null
  }> =>
    request(
      `/friends/${friendId}/messages/${messageId}/speak${force ? '?force=true' : ''}`,
      { method: 'POST' },
    ),

  /** 记忆浏览（api.md §3.1）。 */
  listMemories: (
    friendId: string,
    options: {
      layer?: string
      tag?: string
      search?: string
      sort?: 'recent' | 'importance' | 'created'
      limit?: number
      offset?: number
    } = {},
  ): Promise<{ memories: Memory[]; total: number; layer_counts: Record<string, number> }> => {
    const params = new URLSearchParams()
    if (options.layer) params.set('layer', options.layer)
    if (options.tag) params.set('tag', options.tag)
    if (options.search) params.set('search', options.search)
    if (options.sort) params.set('sort', options.sort)
    params.set('limit', String(options.limit ?? 200))
    params.set('offset', String(options.offset ?? 0))
    return request<{ memories: Memory[]; total: number; layer_counts: Record<string, number> }>(
      `/friends/${friendId}/memories?${params.toString()}`,
    )
  },

  /** 用户手动新增（也会走去重；merged=true 表示合并到已有那条）。 */
  createMemory: (body: {
    friend_id: string
    content: string
    layer?: string
    tag?: string
    importance?: number
  }): Promise<{ id: string; merged: boolean }> =>
    request('/memories', { method: 'POST', body: JSON.stringify(body) }),

  updateMemory: (
    id: string,
    body: { content?: string; importance?: number; layer?: string },
  ): Promise<Memory> => request<Memory>(`/memories/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  deleteMemory: (id: string): Promise<void> =>
    request<void>(`/memories/${id}`, { method: 'DELETE' }),

  /** 记忆改动留痕（api.md §3.4 / D230）：朋友最近动过什么。 */
  listMemoryEvents: (
    friendId: string,
    limit = 50,
  ): Promise<{ events: MemoryEvent[] }> =>
    request<{ events: MemoryEvent[] }>(`/friends/${friendId}/memory-events?limit=${limit}`),

  /** 小纸条（D192）：她攒着想说的那件事（待说 + 待回）。 */
  listThoughts: (
    friendId: string,
  ): Promise<{ thoughts: Thought[]; counts: Record<string, number>; max_pending: number | null }> =>
    request<{ thoughts: Thought[]; counts: Record<string, number>; max_pending: number | null }>(
      `/friends/${friendId}/thoughts`,
    ),

  /** 划掉一张纸条（作废，她不占惦记了）。 */
  dropThought: (id: string): Promise<void> =>
    request<void>(`/thoughts/${id}`, { method: 'DELETE' }),

  /** 日志窗口的增量读（api.md §6）。 */
  readLogs: (
    since: number,
    limit = 500,
  ): Promise<{ items: LogEntry[]; next_seq: number; size: number }> =>
    request(`/logs?since=${since}&limit=${limit}`),

  clearLogs: (): Promise<void> => request<void>('/logs', { method: 'DELETE' }),

  diagnostics: (): Promise<{ text: string }> => request('/logs/diagnostics'),
}
