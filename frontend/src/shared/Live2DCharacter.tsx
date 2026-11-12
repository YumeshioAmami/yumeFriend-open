/**
 * 沉浸模式 Live2D（v7.32 / D430）。
 *
 * 不再让后端每帧编码 WebP（最多约 10fps），而是把 Cubism 模型交给
 * 浏览器里的 PixiJS 渲染，由 GPU 直接跑 60fps。模型 / 贴图 / exp3
 * 由 `/api/friends/<id>/live2d/model/runtime.json` 及同目录文件提供。
 */

import { useEffect, useRef, useState } from 'react'
import { Application, Ticker } from 'pixi.js'
import { Live2DModel } from 'pixi-live2d-display/cubism4'
import {
  loadLive2DPresets,
  resolveMoodPreset,
  resolveModelLabel,
  targetParamsForLabel,
  type Live2DExpressionDef,
  type Live2DModelSettings,
  type Live2DPresetData,
} from './live2dPresets'

// 不主动压到 60；让 requestAnimationFrame 跟显示器刷新（60/120/144Hz）。
Ticker.shared.maxFPS = 0
Live2DModel.registerTicker(Ticker)

// 与后端 `MOOD_KIND_RULES` 保持同一张表：情绪标签 → 种类 + exp3 候选。
const MOOD_KIND_RULES: Array<[RegExp, string, string[]]> = [
  [/害羞|羞耻|脸红|不好意思/, 'shy', ['shy', 'blush', '脸红', 'mouth_worry']],
  [/心动|喜欢|爱慕|宠溺|色气|发情/, 'love', ['love', 'blush', 'drool', 'eyes_guillemet']],
  [/慌张|害怕|惊恐/, 'afraid', ['nervous', 'eyes_oo', 'mouth_o', '流冷汗']],
  [/紧张|不安|心虚|焦虑/, 'nervous', ['nervous', '流冷汗', 'mouth_worry', 'face_dark']],
  [/担心|忧心|挂念/, 'worried', ['sad', 'mouth_worry', 'face_dark']],
  [/伤心|难过|委屈|失落|哭/, 'sad', ['sad', '流泪', 'mouth_worry', 'face_dark']],
  [/疑惑|困惑|懵/, 'confused', ['confused', '疑惑', 'eyes_oo', 'mouth_o']],
  [/思索|思考|沉思|琢磨|想事情/, 'thinking', ['thinking', 'confused', 'eyes_oo', 'mouth_o']],
  [/晕|圈圈|混乱/, 'dizzy', ['dizzy', '圈圈眼', 'eyes_xx']],
  [/得意|自信|骄傲|调皮|坏笑|狡黠/, 'smug', ['smug', '得意', 'eyes_guillemet', 'mouth_switch', 'teeth']],
  [/惊讶|吃惊|震惊/, 'surprised', ['surprised', '惊讶', 'eyes_oo', 'mouth_o']],
  [/生气|愤怒|厌恶|不爽/, 'angry', ['angry', '生气', 'face_dark', 'eyes_guillemet']],
  [/兴奋|激动|惊喜/, 'excited', ['happy', 'smile', 'mouth_3', 'teeth', 'eyes_guillemet']],
  [/困|睡|疲惫|累了/, 'sleepy', ['sleepy', 'sad', 'mouth_worry']],
  [/嘟嘴|不满|别扭/, 'pout', ['pout', 'smug', 'mouth_worry']],
  [/认真|严肃|专注/, 'serious', ['serious', 'face_dark', 'mouth_worry']],
  [/无奈|无语|没办法/, 'helpless', ['sad', 'mouth_worry', 'face_dark']],
  [/好奇|感兴趣/, 'curious', ['confused', 'eyes_oo', 'mouth_o']],
  [/开心|愉快|高兴|笑|轻笑/, 'happy', ['happy', 'smile', 'mouth_3', 'teeth', 'eyes_guillemet']],
]

function moodRule(mood: string): [RegExp, string, string[]] | null {
  return MOOD_KIND_RULES.find(([pattern]) => pattern.test(mood)) ?? null
}

function moodKind(mood: string): string {
  return moodRule(mood)?.[1] ?? 'calm'
}

function setParam(core: any, id: string, value: number): void {
  try {
    core.setParameterValueById(id, value)
  } catch {
    // 不同模型的参数表不一样，没有这个参数就跳过。
  }
}

function applyPresetMoodParams(
  core: any,
  mood: string,
  speaking: boolean,
  state: Record<string, number>,
  presetData: Live2DPresetData,
  modelSettings: Live2DModelSettings | null = null,
  modelExpressions: Live2DExpressionDef[] = [],
): void {
  const now = performance.now() / 1000
  const timestamp = performance.now() / 1000
  const last = state.__last ?? timestamp
  const dt = Math.max(0.001, Math.min(0.1, timestamp - last))
  state.__last = timestamp

  if (modelSettings) {
    const label = resolveModelLabel(modelSettings, mood)
    const values = targetParamsForLabel(modelSettings, label, modelExpressions)
    const idle = modelSettings.idle
    if (idle?.enabled) {
      if (!state.__idleTarget || timestamp >= (state.__idleNextAt ?? 0)) {
        const random = (amount: number) => (Math.random() * 2 - 1) * amount
        state.__idleTargetX = random(idle.head_x)
        state.__idleTargetY = random(idle.head_y)
        state.__idleTargetZ = random(idle.head_z)
        state.__idleTargetBodyX = random(idle.body_x)
        state.__idleTargetBodyZ = random(idle.body_z)
        state.__idleTargetGazeX = random(idle.gaze_x)
        state.__idleTargetGazeY = random(idle.gaze_y)
        state.__idleNextAt = timestamp + 1.5 + Math.random() * 2.5
        state.__idleTarget = 1
      }
      const idleBlend = 1 - Math.exp(-dt * 1.35)
      state.__idleX = (state.__idleX ?? 0) + ((state.__idleTargetX ?? 0) - (state.__idleX ?? 0)) * idleBlend
      state.__idleY = (state.__idleY ?? 0) + ((state.__idleTargetY ?? 0) - (state.__idleY ?? 0)) * idleBlend
      state.__idleZ = (state.__idleZ ?? 0) + ((state.__idleTargetZ ?? 0) - (state.__idleZ ?? 0)) * idleBlend
      state.__idleBodyX =
        (state.__idleBodyX ?? 0) +
        ((state.__idleTargetBodyX ?? 0) - (state.__idleBodyX ?? 0)) * idleBlend
      state.__idleBodyZ =
        (state.__idleBodyZ ?? 0) +
        ((state.__idleTargetBodyZ ?? 0) - (state.__idleBodyZ ?? 0)) * idleBlend
      state.__idleGazeX =
        (state.__idleGazeX ?? 0) +
        ((state.__idleTargetGazeX ?? 0) - (state.__idleGazeX ?? 0)) * idleBlend
      state.__idleGazeY =
        (state.__idleGazeY ?? 0) +
        ((state.__idleTargetGazeY ?? 0) - (state.__idleGazeY ?? 0)) * idleBlend
    } else {
      state.__idleX = 0
      state.__idleY = 0
      state.__idleZ = 0
      state.__idleBodyX = 0
      state.__idleBodyZ = 0
      state.__idleGazeX = 0
      state.__idleGazeY = 0
    }

    const breath = Math.sin(now * 1.25 + (state.__seed ?? 0) * 0.7)
    values.ParamAngleX = (values.ParamAngleX ?? 0) + (state.__idleX ?? 0)
    values.ParamAngleY = (values.ParamAngleY ?? 0) + (state.__idleY ?? 0)
    values.ParamAngleZ = (values.ParamAngleZ ?? 0) + (state.__idleZ ?? 0)
    values.ParamBodyAngleX = (values.ParamBodyAngleX ?? 0) + (state.__idleBodyX ?? 0)
    values.ParamBodyAngleZ = (values.ParamBodyAngleZ ?? 0) + (state.__idleBodyZ ?? 0)
    values.ParamEyeBallX = (values.ParamEyeBallX ?? 0) + (state.__idleGazeX ?? 0)
    values.ParamEyeBallY = (values.ParamEyeBallY ?? 0) + (state.__idleGazeY ?? 0)
    values.ParamBreath = (values.ParamBreath ?? 0.5) + breath * (idle?.breath ?? 0.24)
    values.ParamHairFront = (values.ParamHairFront ?? 0) + Math.sin(now * 0.83) * 0.5
    values.ParamHairSide = (values.ParamHairSide ?? 0) + Math.sin(now * 0.71) * 0.45
    values.ParamHairBack = (values.ParamHairBack ?? 0) + Math.sin(now * 0.61) * 0.4

    const blinkPhase = (now + (state.__blinkSeed ?? 0)) % 4.3
    const blink =
      blinkPhase < 0.16
        ? 1 - 0.92 * Math.sin((Math.PI * blinkPhase) / 0.16)
        : 1
    values.ParamEyeLOpen = (values.ParamEyeLOpen ?? 1) * blink
    values.ParamEyeROpen = (values.ParamEyeROpen ?? 1) * blink

    if (speaking && modelSettings.speech?.enabled) {
      const syllable =
        Math.max(0, Math.sin(now * 11.3)) *
        (0.55 + 0.45 * Math.sin(now * 3.1 + (state.__seed ?? 0)))
      values.ParamMouthOpenY = 0.1 + syllable * modelSettings.speech.mouth_open
      values.ParamAngleX = (values.ParamAngleX ?? 0) + Math.sin(now * 7.5) * 1.8
      values.ParamAngleY = (values.ParamAngleY ?? 0) + Math.sin(now * 5.1) * 1.0
      values.ParamBodyAngleX = (values.ParamBodyAngleX ?? 0) + Math.sin(now * 5.7) * 1.2
    }

    const durationSeconds = Math.max(
      0.05,
      Number(label?.transition_ms ?? modelSettings.transition_ms ?? 320) / 1000,
    )
    const blend = 1 - Math.exp((-dt * 3.0) / durationSeconds)
    for (const [key, targetValue] of Object.entries(values)) {
      const current = state[key] ?? targetValue
      const next = current + (targetValue - current) * blend
      state[key] = next
      setParam(core, key, next)
    }
    return
  }

  const seed = (state.__seed ??= Math.random() * Math.PI * 2)
  const blinkSeed = (state.__blinkSeed ??= Math.random() * 4.3)
  const preset = resolveMoodPreset(presetData, mood)
  const idleSway = Math.sin(now * 0.37 + seed)
  const idleBreath = Math.sin(now * 1.25 + seed * 0.7)
  const values: Record<string, number> = {
    ...presetData.base,
    ...(preset?.params ?? {}),
  }

  // 预设决定“这张脸是什么情绪”，待机微动只叠在头上，不把情绪盖掉。
  values.ParamAngleX =
    (values.ParamAngleX ?? 0) + Math.sin((now + seed) * 0.9) * 1.4
  values.ParamAngleY =
    (values.ParamAngleY ?? 0) + Math.sin((now + seed) * 0.66) * 0.9
  values.ParamAngleZ =
    (values.ParamAngleZ ?? 0) + idleSway * 1.2
  values.ParamBodyAngleX =
    (values.ParamBodyAngleX ?? 0) + Math.sin((now + seed) * 0.56) * 1.2
  values.ParamBodyAngleZ =
    (values.ParamBodyAngleZ ?? 0) + idleSway * 0.9
  values.ParamBreath = (values.ParamBreath ?? 0.5) + idleBreath * 0.24
  values.ParamEyeBallX =
    (values.ParamEyeBallX ?? 0) + Math.sin(now * 0.53 + seed) * 0.14
  values.ParamEyeBallY =
    (values.ParamEyeBallY ?? 0) + Math.sin(now * 0.41 + seed * 1.3) * 0.1
  values.ParamHairFront =
    (values.ParamHairFront ?? 0) + Math.sin(now * 0.83 + seed) * 0.5
  values.ParamHairSide =
    (values.ParamHairSide ?? 0) + Math.sin(now * 0.71 + seed) * 0.45
  values.ParamHairBack =
    (values.ParamHairBack ?? 0) + Math.sin(now * 0.61 + seed) * 0.4

  // 眨眼：有些模型 EyeBlink 组是空的，这里自己做一个。
  const blinkPhase = (now + blinkSeed) % 4.3
  const blink =
    blinkPhase < 0.16
      ? 1 - 0.92 * Math.sin((Math.PI * blinkPhase) / 0.16)
      : 1
  values.ParamEyeLOpen = (values.ParamEyeLOpen ?? 1) * blink
  values.ParamEyeROpen = (values.ParamEyeROpen ?? 1) * blink

  if (speaking) {
    const syllable =
      Math.max(0, Math.sin(now * 11.3)) *
      (0.55 + 0.45 * Math.sin(now * 3.1 + seed))
    values.ParamMouthOpenY = 0.12 + syllable * 0.55
    values.ParamMouthForm =
      (values.ParamMouthForm ?? 0) + Math.sin(now * 8.5) * 0.12
    values.ParamAngleX =
      (values.ParamAngleX ?? 0) + Math.sin(now * 7.5) * 2.2
    values.ParamAngleY =
      (values.ParamAngleY ?? 0) + Math.sin(now * 5.1) * 1.2
    values.ParamAngleZ =
      (values.ParamAngleZ ?? 0) + Math.sin(now * 6.3) * 1.4
    values.ParamBodyAngleX =
      (values.ParamBodyAngleX ?? 0) + Math.sin(now * 5.7) * 1.6
    values.ParamBodyAngleZ =
      (values.ParamBodyAngleZ ?? 0) + Math.sin(now * 4.9) * 1
    values.ParamBreath = (values.ParamBreath ?? 0.5) + 0.08
  }

  const blend = 1 - Math.exp(-dt * 10.5)
  for (const [key, targetValue] of Object.entries(values)) {
    const current = state[key] ?? targetValue
    const next = current + (targetValue - current) * blend
    state[key] = next
    setParam(core, key, next)
  }
}

function applyMoodParams(
  core: any,
  mood: string,
  speaking: boolean,
  state: Record<string, number>,
  presetData: Live2DPresetData | null = null,
  modelSettings: Live2DModelSettings | null = null,
  modelExpressions: Live2DExpressionDef[] = [],
): void {
  if (modelSettings) {
    applyPresetMoodParams(
      core,
      mood,
      speaking,
      state,
      presetData ?? { base: {}, aliases: {}, presets: {} },
      modelSettings,
      modelExpressions,
    )
    return
  }
  if (presetData) {
    applyPresetMoodParams(core, mood, speaking, state, presetData)
    return
  }
  const kind = moodKind(mood)
  const now = performance.now() / 1000
  const seed = (state.__seed ??= Math.random() * Math.PI * 2)
  const blinkSeed = (state.__blinkSeed ??= Math.random() * 4.3)
  const burst = Math.exp(-0.22 * (now % 8)) * Math.sin(now * 7)
  const base: Record<string, number> = {
    ParamEyeLOpen: 1,
    ParamEyeROpen: 1,
    ParamEyeLSmile: 0,
    ParamEyeRSmile: 0,
    ParamEyeSmile: 0,
    ParamEyeAnger: 0,
    ParamEyePuff: 0,
    ParamMouthForm: 0,
    ParamMouthOpenY: 0,
    ParamCheek: 0,
    ParamBrowLY: 0,
    ParamBrowRY: 0,
    ParamBrowLAngle: 0,
    ParamBrowRAngle: 0,
    ParamBrowLForm: 0,
    ParamBrowRForm: 0,
    ParamEyeBallX: 0,
    ParamEyeBallY: 0,
    ParamMouthX: 0,
    ParamAngleX: 0,
    ParamAngleY: 0,
    ParamAngleZ: 0,
    ParamBodyAngleX: 0,
    ParamBodyAngleY: 0,
    ParamBodyAngleZ: 0,
    ParamBreath: 0.5,
    ParamHairFront: 0,
    ParamHairSide: 0,
    ParamHairBack: 0,
    PARAM_EXP1: 0,
    PARAM_EXP2: 0,
    PARAM_EXP3: 0,
    PARAM_EXP4: 0,
    PARAM_EXP5: 0,
    PARAM_EXP6: 0,
    PARAM_EXP7: 0,
    PARAM_EXP8: 0,
    // 中肥鱼自带的水印开关（作者提供的 no_watermark.exp3 参数）。
    Paramcol_3: 1,
  }
  const idleSway = Math.sin(now * 0.37 + seed)
  const idleBreath = Math.sin(now * 1.25 + seed * 0.7)
  const values: Record<string, number> = {
    ParamAngleX: Math.sin((now + seed) * 0.9) * 1.4,
    ParamAngleY: Math.sin((now + seed) * 0.66) * 0.9,
    ParamAngleZ: idleSway * 1.2,
    ParamBodyAngleX: Math.sin((now + seed) * 0.56) * 1.2,
    ParamBodyAngleZ: idleSway * 0.9,
    ParamBreath: 0.5 + idleBreath * 0.24,
    ParamEyeBallX: Math.sin(now * 0.53 + seed) * 0.14,
    ParamEyeBallY: Math.sin(now * 0.41 + seed * 1.3) * 0.1,
    ParamHairFront: Math.sin(now * 0.83 + seed) * 0.5,
    ParamHairSide: Math.sin(now * 0.71 + seed) * 0.45,
    ParamHairBack: Math.sin(now * 0.61 + seed) * 0.4,
  }
  if (kind === 'happy') {
    values.ParamEyeLOpen = 0.82
    values.ParamEyeROpen = 0.82
    values.ParamEyeLSmile = 1
    values.ParamEyeRSmile = 1
    values.ParamEyeSmile = 1
    values.ParamMouthForm = 0.85
    values.ParamMouthOpenY = 0.3
    values.ParamCheek = 0.7
    values.ParamBrowLY = 0.35
    values.ParamBrowRY = 0.35
    values.ParamAngleX = 3
    values.ParamAngleY = -4
    values.ParamAngleZ = -8 + burst * 3
    values.ParamBodyAngleX = 2.6 + burst * 1.5
    values.PARAM_EXP2 = 1
  } else if (kind === 'excited') {
    values.ParamEyeLOpen = 0.95
    values.ParamEyeROpen = 0.95
    values.ParamEyeLSmile = 1
    values.ParamEyeRSmile = 1
    values.ParamEyeSmile = 1
    values.ParamMouthForm = 0.9
    values.ParamMouthOpenY = 0.55
    values.ParamCheek = 0.9
    values.ParamBrowLY = 0.5
    values.ParamBrowRY = 0.5
    values.ParamAngleX = 5
    values.ParamAngleY = -6 + burst * 4
    values.ParamAngleZ = -10 + burst * 4
    values.ParamBodyAngleX = 4 + burst * 2
    values.PARAM_EXP2 = 1
  } else if (kind === 'shy') {
    values.ParamEyeLOpen = 0.3
    values.ParamEyeROpen = 0.3
    values.ParamEyeLSmile = 0.6
    values.ParamEyeRSmile = 0.6
    values.ParamEyeSmile = 0.6
    values.ParamMouthForm = 0.5
    values.ParamMouthOpenY = 0.08
    values.ParamCheek = 1
    values.ParamBrowLY = -0.25
    values.ParamBrowRY = -0.25
    values.ParamAngleX = -4
    values.ParamAngleY = 6
    values.ParamAngleZ = 12 + burst * 3
    values.ParamEyeBallX = -0.25
    values.ParamEyeBallY = -0.22
    values.PARAM_EXP4 = 1
  } else if (kind === 'love') {
    values.ParamEyeLOpen = 0.45
    values.ParamEyeROpen = 0.45
    values.ParamEyeLSmile = 1
    values.ParamEyeRSmile = 1
    values.ParamEyeSmile = 1
    values.ParamMouthForm = 0.8
    values.ParamMouthOpenY = 0.14
    values.ParamCheek = 1
    values.ParamAngleX = 4
    values.ParamAngleY = 3
    values.ParamAngleZ = 8 + burst * 3
    values.ParamBodyAngleX = 1.8
    values.ParamEyeBallX = 0.2
    values.ParamEyeBallY = -0.16
    values.PARAM_EXP4 = 1
    values.PARAM_EXP7 = 1
  } else if (kind === 'sad') {
    values.ParamEyeLOpen = 0.22
    values.ParamEyeROpen = 0.22
    values.ParamMouthForm = -0.8
    values.ParamMouthOpenY = 0.06
    values.ParamBrowLY = -0.55
    values.ParamBrowRY = -0.55
    values.ParamBrowLAngle = -0.55
    values.ParamBrowRAngle = 0.55
    values.ParamAngleX = -3
    values.ParamAngleY = 12
    values.ParamAngleZ = 6
    values.ParamBodyAngleX = -3
    values.ParamEyeBallY = -0.28
    values.PARAM_EXP3 = 1
    values.PARAM_EXP7 = -1
  } else if (kind === 'worried') {
    values.ParamEyeLOpen = 0.62
    values.ParamEyeROpen = 0.62
    values.ParamMouthForm = -0.55
    values.ParamBrowLY = 0.25
    values.ParamBrowRY = 0.25
    values.ParamBrowLAngle = 0.45
    values.ParamBrowRAngle = -0.45
    values.ParamAngleX = -4
    values.ParamAngleY = 8
    values.ParamAngleZ = 5
    values.ParamBodyAngleX = -1.5
    values.ParamEyeBallY = -0.12
  } else if (kind === 'afraid') {
    values.ParamEyeLOpen = 1
    values.ParamEyeROpen = 1
    values.ParamMouthForm = -0.4
    values.ParamMouthOpenY = 0.5
    values.ParamBrowLY = 0.55
    values.ParamBrowRY = 0.55
    values.ParamBrowLAngle = 0.35
    values.ParamBrowRAngle = -0.35
    values.ParamAngleX = 3
    values.ParamAngleY = 6
    values.ParamAngleZ = -6
    values.ParamBodyAngleX = -3.5
    values.ParamEyeBallY = 0.15
    values.ParamEyePuff = 0.25
  } else if (kind === 'angry') {
    values.ParamEyeLOpen = 0.5
    values.ParamEyeROpen = 0.5
    values.ParamMouthForm = -0.85
    values.ParamMouthOpenY = 0.12
    values.ParamBrowLAngle = -1
    values.ParamBrowRAngle = 1
    values.ParamBrowLY = 0.3
    values.ParamBrowRY = 0.3
    values.ParamAngleX = burst * 5
    values.ParamAngleY = 8
    values.ParamAngleZ = -7 + burst * 2
    values.ParamBodyAngleX = 3.5
    values.ParamEyeBallX = 0.25
    values.ParamEyeAnger = 1
    values.PARAM_EXP3 = 1
    values.PARAM_EXP5 = -1
  } else if (kind === 'surprised') {
    values.ParamEyeLOpen = 1
    values.ParamEyeROpen = 1
    values.ParamMouthOpenY = 0.78
    values.ParamMouthForm = 0.1
    values.ParamBrowLY = 0.7
    values.ParamBrowRY = 0.7
    values.ParamAngleX = 2
    values.ParamAngleY = -10 + burst * 4
    values.ParamAngleZ = 5
    values.ParamBodyAngleY = burst * 2
    values.ParamEyeBallY = 0.25
    values.PARAM_EXP5 = 1
    values.PARAM_EXP8 = 1
  } else if (kind === 'nervous') {
    values.ParamEyeLOpen = 0.7
    values.ParamEyeROpen = 0.7
    values.ParamMouthForm = -0.35
    values.ParamMouthOpenY = 0.08
    values.ParamBrowLY = 0.2
    values.ParamBrowRY = 0.2
    values.ParamAngleX = Math.sin(now * 6.2) * 4
    values.ParamAngleY = 4
    values.ParamAngleZ = 4 + Math.sin(now * 5.1) * 2
    values.ParamBodyAngleZ = Math.sin(now * 4.4) * 1.8
    values.ParamEyeBallX = Math.sin(now * 7) * 0.35
    values.ParamEyePuff = 0.45
    values.PARAM_EXP8 = -1
  } else if (kind === 'confused') {
    values.ParamEyeLOpen = 0.85
    values.ParamEyeROpen = 0.75
    values.ParamBrowLY = 0.65
    values.ParamBrowRY = -0.2
    values.ParamBrowLAngle = 0.15
    values.ParamBrowRAngle = 0.45
    values.ParamMouthForm = -0.2
    values.ParamMouthOpenY = 0.08
    values.ParamAngleX = 7
    values.ParamAngleY = 2
    values.ParamAngleZ = -12 + burst * 2
    values.ParamEyeBallX = 0.25
    values.ParamEyeBallY = 0.12
    values.PARAM_EXP5 = 1
    values.PARAM_EXP8 = 1
  } else if (kind === 'curious') {
    values.ParamEyeLOpen = 1
    values.ParamEyeROpen = 0.85
    values.ParamBrowLY = 0.45
    values.ParamBrowRY = 0.1
    values.ParamMouthForm = 0.1
    values.ParamMouthOpenY = 0.1
    values.ParamAngleX = 6
    values.ParamAngleY = -2
    values.ParamAngleZ = -8
    values.ParamEyeBallX = 0.3
    values.ParamEyeBallY = 0.1
  } else if (kind === 'smug') {
    values.ParamEyeLOpen = 0.5
    values.ParamEyeROpen = 0.5
    values.ParamEyeLSmile = 0.8
    values.ParamEyeRSmile = 0.8
    values.ParamEyeSmile = 0.8
    values.ParamMouthForm = 0.65
    values.ParamMouthOpenY = 0.1
    values.ParamBrowLY = 0.1
    values.ParamBrowRY = 0.4
    values.ParamAngleX = 4
    values.ParamAngleY = 2
    values.ParamAngleZ = 9
    values.ParamEyeBallX = 0.2
    values.PARAM_EXP1 = 1
  } else if (kind === 'serious') {
    values.ParamEyeLOpen = 0.9
    values.ParamEyeROpen = 0.9
    values.ParamMouthForm = -0.2
    values.ParamBrowLY = -0.15
    values.ParamBrowRY = -0.15
    values.ParamBrowLAngle = -0.2
    values.ParamBrowRAngle = 0.2
    values.ParamAngleY = 4
    values.ParamAngleZ = 2
    values.ParamBodyAngleX = 0.8
    values.ParamEyeBallY = -0.1
  } else if (kind === 'helpless') {
    values.ParamEyeLOpen = 0.45
    values.ParamEyeROpen = 0.45
    values.ParamMouthForm = -0.4
    values.ParamMouthOpenY = 0.1
    values.ParamBrowLY = 0.3
    values.ParamBrowRY = 0.3
    values.ParamBrowLAngle = 0.35
    values.ParamBrowRAngle = -0.35
    values.ParamAngleX = -3
    values.ParamAngleY = 8
    values.ParamAngleZ = -6
    values.ParamBodyAngleX = -1.2
  } else if (kind === 'dizzy') {
    values.ParamEyeLOpen = 0.15
    values.ParamEyeROpen = 0.15
    values.ParamMouthForm = -0.25
    values.ParamMouthOpenY = 0.12
    values.ParamAngleZ = 18 * Math.sin(now * 3.2)
    values.ParamBodyAngleZ = 4 * Math.cos(now * 2.7)
    values.PARAM_EXP6 = 1
  }
  if (kind === 'thinking') {
    values.ParamEyeLOpen = 0.85
    values.ParamEyeROpen = 0.7
    values.ParamBrowLY = 0.6
    values.ParamBrowRY = 0.1
    values.ParamBrowLAngle = 0.2
    values.ParamBrowRAngle = 0.4
    values.ParamMouthForm = -0.15
    values.ParamAngleX = 8
    values.ParamAngleY = 4
    values.ParamAngleZ = -12 + burst * 1.5
    values.ParamBodyAngleX = -0.8
    values.ParamEyeBallX = 0.32
    values.ParamEyeBallY = 0.22
    values.PARAM_EXP5 = 1
  } else if (kind === 'sleepy') {
    values.ParamEyeLOpen = 0.18
    values.ParamEyeROpen = 0.18
    values.ParamMouthForm = -0.2
    values.ParamMouthOpenY = 0.16
    values.ParamAngleX = -2
    values.ParamAngleY = 10
    values.ParamAngleZ = 6
    values.ParamBodyAngleX = -1.5
  } else if (kind === 'pout') {
    values.ParamEyeLOpen = 0.65
    values.ParamEyeROpen = 0.65
    values.ParamMouthForm = -0.8
    values.ParamMouthOpenY = 0.08
    values.ParamCheek = 0.8
    values.ParamBrowLY = 0.25
    values.ParamBrowRY = 0.25
    values.ParamBrowLAngle = -0.3
    values.ParamBrowRAngle = 0.3
    values.ParamAngleX = -2
    values.ParamAngleY = 4
    values.ParamAngleZ = 8
    values.ParamMouthX = 0.35
    values.PARAM_EXP8 = -1
  }

  // 自己补眨眼：有些模型 EyeBlink 组是空的。
  const blinkPhase = (now + blinkSeed) % 4.3
  const blink =
    blinkPhase < 0.16
      ? 1 - 0.92 * Math.sin((Math.PI * blinkPhase) / 0.16)
      : 1
  values.ParamEyeLOpen = (values.ParamEyeLOpen ?? 1) * blink
  values.ParamEyeROpen = (values.ParamEyeROpen ?? 1) * blink

  if (speaking) {
    const syllable =
      Math.max(0, Math.sin(now * 11.3)) *
      (0.55 + 0.45 * Math.sin(now * 3.1 + seed))
    values.ParamMouthOpenY = 0.12 + syllable * 0.55
    values.ParamMouthForm = (values.ParamMouthForm ?? 0) + Math.sin(now * 8.5) * 0.12
    values.ParamAngleX = (values.ParamAngleX ?? 0) + Math.sin(now * 7.5) * 2.2
    values.ParamAngleY = (values.ParamAngleY ?? 0) + Math.sin(now * 5.1) * 1.2
    values.ParamAngleZ = (values.ParamAngleZ ?? 0) + Math.sin(now * 6.3) * 1.4
    values.ParamBodyAngleX =
      (values.ParamBodyAngleX ?? 0) + Math.sin(now * 5.7) * 1.6
    values.ParamBodyAngleZ =
      (values.ParamBodyAngleZ ?? 0) + Math.sin(now * 4.9) * 1
    values.ParamBreath = (values.ParamBreath ?? 0.5) + 0.08
  }
  const timestamp = performance.now() / 1000
  const last = state.__last ?? timestamp
  const dt = Math.max(0.001, Math.min(0.1, timestamp - last))
  state.__last = timestamp
  const blend = 1 - Math.exp(-dt * 10.5)
  for (const [key, targetValue] of Object.entries({ ...base, ...values })) {
    const current = state[key] ?? targetValue
    const next = current + (targetValue - current) * blend
    state[key] = next
    setParam(core, key, next)
  }
}

export default function Live2DCharacter({
  friendId,
  modelId,
  settingsOverride,
  expressionsOverride,
  speakText,
  mood,
  speaking,
  className = '',
  onUnavailable,
}: {
  friendId?: string
  modelId?: string
  settingsOverride?: Live2DModelSettings | null
  expressionsOverride?: Live2DExpressionDef[]
  /** 没有音频时按这段文字估口型时长；有音频时不要传。 */
  speakText?: string
  mood: string
  speaking: boolean
  className?: string
  onUnavailable?: () => void
}) {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const moodRef = useRef(mood)
  const speakingRef = useRef(speaking)
  const paramStateRef = useRef<Record<string, number>>({})
  const presetDataRef = useRef<Live2DPresetData | null>(null)
  const modelSettingsRef = useRef<Live2DModelSettings | null>(settingsOverride ?? null)
  const modelExpressionsRef = useRef<Live2DExpressionDef[]>(expressionsOverride ?? [])
  const [ready, setReady] = useState(false)
  const [textSpeaking, setTextSpeaking] = useState(false)
  const [settingsEpoch, setSettingsEpoch] = useState(0)
  const [settingsRefresh, setSettingsRefresh] = useState(0)

  useEffect(() => {
    let alive = true
    void loadLive2DPresets()
      .then((data) => {
        if (alive) presetDataRef.current = data
      })
      .catch((error) => {
        console.warn('[live2d] mood presets load failed', error)
      })
    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    modelSettingsRef.current = settingsOverride ?? null
    setSettingsEpoch((value) => value + 1)
  }, [settingsOverride])
  useEffect(() => {
    modelExpressionsRef.current = expressionsOverride ?? []
  }, [expressionsOverride])

  useEffect(() => {
    const onSettingsChanged = () => setSettingsRefresh((value) => value + 1)
    window.addEventListener('yf-live2d-settings-changed', onSettingsChanged)
    return () => window.removeEventListener('yf-live2d-settings-changed', onSettingsChanged)
  }, [])

  useEffect(() => {
    if (!friendId && !modelId) return
    let alive = true
    if (settingsOverride) {
      modelSettingsRef.current = settingsOverride
      modelExpressionsRef.current = expressionsOverride ?? []
      return () => {
        alive = false
      }
    }
    const url = friendId
      ? `/api/friends/${friendId}/live2d/meta`
      : `/api/live2d/models/${modelId}/meta`
    void fetch(url, { cache: 'no-store' })
      .then((response) => {
        if (!response.ok) throw new Error(`Live2D meta HTTP ${response.status}`)
        return response.json()
      })
      .then((data) => {
        if (!alive) return
        if (data?.settings) modelSettingsRef.current = data.settings
        if (Array.isArray(data?.expressions)) modelExpressionsRef.current = data.expressions
        setSettingsEpoch((value) => value + 1)
      })
      .catch((error) => {
        console.warn('[live2d] model settings load failed', error)
      })
    return () => {
      alive = false
    }
  }, [friendId, modelId, settingsOverride, expressionsOverride, settingsRefresh])

  useEffect(() => {
    moodRef.current = mood
  }, [mood])
  useEffect(() => {
    speakingRef.current = speaking || textSpeaking
  }, [speaking, textSpeaking])

  useEffect(() => {
    if (!speakText || speaking) {
      setTextSpeaking(false)
      return
    }
    const speech = modelSettingsRef.current?.speech
    const perChar = Number(speech?.text_ms_per_char ?? 160)
    const minMs = Number(speech?.min_ms ?? 600)
    const maxMs = Number(speech?.max_ms ?? 12000)
    const duration = Math.max(minMs, Math.min(maxMs, speakText.trim().length * perChar))
    setTextSpeaking(true)
    const timer = window.setTimeout(() => setTextSpeaking(false), duration)
    return () => window.clearTimeout(timer)
  }, [speakText, speaking, settingsEpoch])

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let disposed = false
    let resizeObserver: ResizeObserver | null = null
    let model: any = null
    let app: Application | null = null

    const boot = async () => {
      try {
        app = new Application({
          backgroundAlpha: 0,
          antialias: true,
          autoDensity: true,
          // Windows 100% 缩放时 devicePixelRatio 只有 1，旧逻辑会让整张
          // Live2D 以 1× 渲染再被 CSS 拉伸，脸和发丝会发糊。至少按 2× 渲染；
          // 高分屏最多 2.5×，避免无意义地烧 GPU。
          resolution: Math.min(2.5, Math.max(2, window.devicePixelRatio || 1)),
          // Live2D 的模型更新挂在 Ticker.shared；如果 Application 自己再开一个
          // ticker，更新和绘制不同步，就会出现“看起来一顿一顿/发抖”。
          sharedTicker: true,
        })
        app.ticker.maxFPS = 0
        app.view.style.width = '100%'
        app.view.style.height = '100%'
        app.view.style.display = 'block'
        host.appendChild(app.view as HTMLCanvasElement)
        const runtimeUrl = modelId
          ? `/api/live2d/models/${modelId}/runtime.json`
          : `/api/friends/${friendId}/live2d/model/runtime.json`
        model = await Live2DModel.from(runtimeUrl, { autoInteract: false })
        if (disposed) {
          model.destroy()
          app.destroy(true)
          return
        }
        app.stage.addChild(model)
        model.anchor.set(0, 0)
        const fit = () => {
          if (!app || !model || disposed) return
          const width = Math.max(1, host.clientWidth)
          const height = Math.max(1, host.clientHeight)
          app.renderer.resize(width, height)
          const originalWidth = Number(model.internalModel?.originalWidth) || 1
          const originalHeight = Number(model.internalModel?.originalHeight) || 1
          // 先把完整画布缩进 viewport，再读一次真实 alpha 外接框；
          // Live2D 的 canvas 有大量透明边，按 canvas 尺寸缩放会让角色变得很小。
          const baseScale = Math.min(width / originalWidth, height / originalHeight)
          model.scale.set(baseScale)
          model.position.set(0, 0)
          let visibleBox: { x: number; y: number; w: number; h: number } | null = null
          try {
            // 渲染到主画布后直接读 alpha，不再依赖 extract 插件（不同 Pixi
            // 小版本里插件位置不一样，读 GL 像素最稳）。
            app.renderer.render(app.stage)
            const gl = (app.renderer as any).gl as WebGLRenderingContext
            const resolution = Math.max(1, app.renderer.resolution || 1)
            const renderWidth = Math.max(1, Math.round(app.renderer.width || width * resolution))
            const renderHeight = Math.max(1, Math.round(app.renderer.height || height * resolution))
            const pixels = new Uint8Array(renderWidth * renderHeight * 4)
            gl.readPixels(0, 0, renderWidth, renderHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels)
            let minX = renderWidth
            let minY = renderHeight
            let maxX = -1
            let maxY = -1
            for (let y = 0; y < renderHeight; y += 1) {
              for (let x = 0; x < renderWidth; x += 1) {
                const alpha = pixels[(y * renderWidth + x) * 4 + 3]
                if (alpha <= 8) continue
                minX = Math.min(minX, x)
                minY = Math.min(minY, y)
                maxX = Math.max(maxX, x)
                maxY = Math.max(maxY, y)
              }
            }
            if (maxX >= minX && maxY >= minY) {
              // glReadPixels 原点在左下，转成 Pixi 的左上坐标。
              visibleBox = {
                x: minX / resolution,
                y: (renderHeight - 1 - maxY) / resolution,
                w: (maxX - minX + 1) / resolution,
                h: (maxY - minY + 1) / resolution,
              }
            }
          } catch {
            // 提取失败就退回整个 canvas。
          }
          if (!visibleBox) {
            const scale = Math.min(width / originalWidth, height / originalHeight) * 0.98
            model.scale.set(scale)
            model.position.set(width / 2, height)
            return
          }
          const extra = Math.min(width / visibleBox.w, height / visibleBox.h) * 0.98
          model.scale.set(baseScale * extra)
          model.position.set(
            width / 2 - (visibleBox.x + visibleBox.w / 2) * extra,
            height - (visibleBox.y + visibleBox.h) * extra,
          )
        }
        fit()
        window.requestAnimationFrame(fit)
        window.setTimeout(fit, 120)
        resizeObserver = new ResizeObserver(fit)
        resizeObserver.observe(host)

        const onBeforeModelUpdate = () => {
          const core = model?.internalModel?.coreModel
          if (core) {
            applyMoodParams(
              core,
              moodRef.current,
              speakingRef.current,
              paramStateRef.current,
              presetDataRef.current,
              modelSettingsRef.current,
              modelExpressionsRef.current,
            )
          }
        }
        model.internalModel.on('beforeModelUpdate', onBeforeModelUpdate)
        setReady(true)
      } catch (error) {
        console.error('[live2d] 加载失败', error)
        if (!disposed) onUnavailable?.()
      }
    }
    void boot()
    return () => {
      disposed = true
      resizeObserver?.disconnect()
      try {
        model?.internalModel?.off?.('beforeModelUpdate')
        model?.destroy()
      } catch {
        // ignore
      }
      try {
        app?.destroy(true, { children: true })
      } catch {
        // ignore
      }
      setReady(false)
    }
  }, [friendId, modelId, onUnavailable])

  return (
    <div ref={hostRef} className={`relative h-full w-full ${className}`}>
      {!ready && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <span className="text-[11px] text-white/35">正在加载 Live2D…</span>
        </div>
      )}
    </div>
  )
}
