/**
 * Live2D Cubism Core 的动态加载（D475）。
 *
 * `pixi-live2d-display/cubism4` 在模块初始化时就要求全局存在
 * `Live2DCubismCore`，所以不能静态 import。这里先确认本机运行时已安装，
 * 从后端加载 Core，再动态 import 渲染模块。
 */

import { Ticker } from 'pixi.js'

import { api } from './api'

declare global {
  interface Window {
    Live2DCubismCore?: unknown
  }
}

export class Live2DRuntimeMissingError extends Error {
  constructor() {
    super('还没有安装 Live2D 运行时，请先在 Live2D 设置页下载')
    this.name = 'Live2DRuntimeMissingError'
  }
}

let runtimePromise: Promise<typeof import('pixi-live2d-display/cubism4')> | null = null

function loadCoreScript(): Promise<void> {
  if (window.Live2DCubismCore) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const script = document.createElement('script')
    script.src = `/api/live2d/runtime/core.js?v=${Date.now()}`
    script.async = true
    script.onload = () => {
      if (window.Live2DCubismCore) resolve()
      else reject(new Error('Live2D 运行时脚本已加载，但没有注册 Cubism Core'))
    }
    script.onerror = () => reject(new Error('加载 Live2D 运行时脚本失败'))
    document.head.appendChild(script)
  })
}

export function loadLive2DRuntime(): Promise<typeof import('pixi-live2d-display/cubism4')> {
  if (runtimePromise) return runtimePromise
  runtimePromise = (async () => {
    if (!window.Live2DCubismCore) {
      const status = await api.getLive2DRuntimeStatus()
      if (!status.available) throw new Live2DRuntimeMissingError()
      await loadCoreScript()
    }
    const module = await import('pixi-live2d-display/cubism4')
    Ticker.shared.maxFPS = 0
    module.Live2DModel.registerTicker(Ticker)
    return module
  })().catch((error) => {
    runtimePromise = null
    throw error
  })
  return runtimePromise
}
