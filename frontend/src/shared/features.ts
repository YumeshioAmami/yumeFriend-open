/** v7.20（D373）：功能开关的前端小工具。
 *
 * 真正的清单在后台（`GET /api/features`）；这里只做：
 * - 统一的"这个功能开没开"判断；
 * - 聊天页入口的显示顺序 / 标签。
 *
 * 加一个新功能：后台 catalog 加一条；这里如果要出现在聊天页，再加一条入口。
 */

import type { FeatureInfo } from './api'

export type FeatureMap = Record<string, FeatureInfo>

export function featureMap(features: FeatureInfo[] | null | undefined): FeatureMap {
  const result: FeatureMap = {}
  for (const item of features ?? []) result[item.id] = item
  return result
}

/** 没加载到清单时一律放行（和后台 fail-open 的口径一致）。 */
export function isFeatureOn(map: FeatureMap, id: string): boolean {
  const item = map[id]
  return item ? item.enabled : true
}

export const FEATURE_CATEGORY_ORDER = ['核心', '聊天能力', '生活与陪伴', '界面', '玩法'] as const

export const FEATURE_CATEGORY_HINTS: Record<string, string> = {
  核心: '聊天、人格、记忆这些地基，不能关。',
  聊天能力: '决定她能调用哪些工具、看到哪些上下文；关掉后不再占工具位和提示词。',
  生活与陪伴: '她自己的生活和呈现方式；关掉后不再后台推进或显示入口。',
  界面: '只是换一种看法；关掉不影响聊天本身。',
  玩法: '独立进程 / 独立循环的重功能。',
}

/** 聊天页右上角入口（顺序就是显示顺序）。 */
export const CHAT_FEATURE_ENTRIES: {
  id: string
  featureId: string
  label: string
  title: string
}[] = [
  { id: 'schedule', featureId: 'schedule', label: '日程', title: '她的日程与到点提醒' },
  { id: 'knowledge', featureId: 'knowledge', label: '知识库', title: '她手头的资料（发文件给她读）' },
  { id: 'screen', featureId: 'screen', label: '看看屏幕', title: '截一张你现在的屏幕，让她看看你在干嘛' },
  { id: 'immersive', featureId: 'immersive', label: '沉浸', title: '沉浸模式：立绘 + 一段一段说（Esc 退出）' },
  { id: 'space', featureId: 'space', label: '小世界', title: '小世界：3D 房间原型（先看效果）' },
  { id: 'call', featureId: 'immersive', label: '通话', title: '语音通话：不显示文字，她说完就自动听你说' },
  { id: 'pet', featureId: 'pet', label: '桌宠', title: '桌宠：独立小窗（置顶、可拖动、能直接聊天）' },
]
