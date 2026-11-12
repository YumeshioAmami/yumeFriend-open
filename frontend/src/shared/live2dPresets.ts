export type Live2DParamMap = Record<string, number>

export interface Live2DMoodPreset {
  kind: string
  params: Live2DParamMap
}

export interface Live2DPresetData {
  base: Live2DParamMap
  aliases: Record<string, string>
  presets: Record<string, Live2DMoodPreset>
}

export interface Live2DLabel {
  id: string
  name: string
  aliases: string[]
  params: Live2DParamMap
  expressions: string[]
  transition_ms?: number | null
}

export interface Live2DIdleSettings {
  enabled: boolean
  head_x: number
  head_y: number
  head_z: number
  body_x: number
  body_z: number
  gaze_x: number
  gaze_y: number
  breath: number
}

export interface Live2DSpeechSettings {
  enabled: boolean
  text_ms_per_char: number
  min_ms: number
  max_ms: number
  mouth_open: number
}

export interface Live2DModelSettings {
  version: number
  model_id: string
  display_name: string
  default_label: string
  transition_ms: number
  base: Live2DParamMap
  labels: Live2DLabel[]
  idle: Live2DIdleSettings
  speech: Live2DSpeechSettings
}

export interface Live2DExpressionParam {
  id: string
  value: number
  blend: 'Add' | 'Multiply' | 'Overwrite' | string
}

export interface Live2DExpressionDef {
  id: string
  name: string
  file: string
  params: Live2DExpressionParam[]
}

export interface Live2DParameterMeta {
  id: string
  name?: string
  group?: string
  min: number
  max: number
  default: number
}

export interface Live2DModelMeta {
  model: {
    id: string
    name: string
    model_file: string
    source?: string
  }
  parameters: Live2DParameterMeta[]
  expressions: Live2DExpressionDef[]
  settings: Live2DModelSettings
  model_url: string
}

let cached: Live2DPresetData | null = null
let loading: Promise<Live2DPresetData> | null = null

export function loadLive2DPresets(): Promise<Live2DPresetData> {
  if (cached) return Promise.resolve(cached)
  if (loading) return loading
  loading = fetch('/live2d/mood_presets.json', { cache: 'no-cache' })
    .then((response) => {
      if (!response.ok) throw new Error(`mood presets: HTTP ${response.status}`)
      return response.json() as Promise<Live2DPresetData>
    })
    .then((data) => {
      cached = data
      return data
    })
    .finally(() => {
      loading = null
    })
  return loading
}

export function resolveMoodPreset(
  data: Live2DPresetData | null,
  mood: string,
): Live2DMoodPreset | null {
  if (!data) return null
  const key = data.aliases[mood] ?? mood
  return (
    data.presets[key] ??
    data.presets['正常'] ??
    data.presets['平静'] ??
    null
  )
}

export function resolveModelLabel(
  settings: Live2DModelSettings | null | undefined,
  mood: string,
): Live2DLabel | null {
  if (!settings) return null
  const text = (mood ?? '').trim()
  const labels = settings.labels ?? []
  const exact = labels.find((label) => label.name === text)
  if (exact) return exact
  if (text) {
    const alias = labels.find((label) => (label.aliases ?? []).includes(text))
    if (alias) return alias
  }
  return (
    labels.find((label) => label.name === settings.default_label) ??
    labels[0] ??
    null
  )
}

export function applyExpressionDefs(
  params: Live2DParamMap,
  expressions: Live2DExpressionDef[],
  expressionIds: string[],
): Live2DParamMap {
  const wanted = new Set(expressionIds ?? [])
  const result = { ...params }
  for (const expression of expressions ?? []) {
    if (!wanted.has(expression.id)) continue
    for (const op of expression.params ?? []) {
      const current = result[op.id] ?? 0
      const blend = String(op.blend || 'Add').toLowerCase()
      if (blend === 'multiply') result[op.id] = (result[op.id] ?? 1) * op.value
      else if (blend === 'overwrite') result[op.id] = op.value
      else result[op.id] = current + op.value
    }
  }
  return result
}

export function targetParamsForLabel(
  settings: Live2DModelSettings | null | undefined,
  label: Live2DLabel | null,
  expressions: Live2DExpressionDef[] = [],
): Live2DParamMap {
  if (!settings || !label) return {}
  const params: Live2DParamMap = {
    ...(settings.base ?? {}),
    ...(label.params ?? {}),
  }
  return applyExpressionDefs(params, expressions, label.expressions ?? [])
}
