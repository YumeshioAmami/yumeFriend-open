/**
 * 手机壳（v3.0 / D140）—— 屏幕栈 + 安卓返回键。
 *
 * 单栈（用户拍板：**不做底部 tab**）：
 *
 *     朋友列表 ──点人──► 某个朋友的聊天 ──右上角──► 沉浸 / 通话（全屏）
 *
 * 返回键语义：**安卓的返回 = 上一层**；在列表再按返回 = 退出（交给浏览器/壳）。
 * 实现方式：进下一屏时 `history.pushState`，`popstate` 时退一层 ——
 * 这样 WebView 里的系统返回键、浏览器自带返回、手势返回全都自然生效。
 */

import { useCallback, useEffect, useState } from 'react'

import ChatScreen from './ChatScreen'
import FriendListScreen from './FriendListScreen'
import KnowledgeScreen from './KnowledgeScreen'
import LogScreen from './LogScreen'
import PersonaScreen from './PersonaScreen'
import ScheduleScreen from './ScheduleScreen'
import SettingsScreen from './SettingsScreen'
import SyncScreen from './SyncScreen'
import { useFriends } from './useFriends'
import { preloadMoodSounds } from '../shared/moodSound'

type Screen =
  | { name: 'list' }
  | { name: 'chat'; friendId: string }
  | { name: 'knowledge'; friendId: string }
  | { name: 'persona'; friendId: string }
  | { name: 'schedules'; friendId: string }
  | { name: 'logs' }
  | { name: 'settings' }
  | { name: 'sync' }

/** `#chat=<id>` → 直接打开那个朋友的聊天（刷新不丢；将来推送点进来也用这个）。 */
function screenFromHash(): Screen {
  if (/(?:^#|&)settings=1/.test(window.location.hash)) return { name: 'settings' }
  if (/(?:^#|&)logs=1/.test(window.location.hash)) return { name: 'logs' }
  if (/(?:^#|&)sync=1/.test(window.location.hash)) return { name: 'sync' }
  const knowledge = /(?:^#|&)knowledge=([0-9a-fA-F-]{8,})/.exec(window.location.hash)
  if (knowledge) return { name: 'knowledge', friendId: knowledge[1] }
  const persona = /(?:^#|&)persona=([0-9a-fA-F-]{8,})/.exec(window.location.hash)
  if (persona) return { name: 'persona', friendId: persona[1] }
  const schedules = /(?:^#|&)schedules=([0-9a-fA-F-]{8,})/.exec(window.location.hash)
  if (schedules) return { name: 'schedules', friendId: schedules[1] }
  const match = /(?:^#|&)chat=([0-9a-fA-F-]{8,})/.exec(window.location.hash)
  return match ? { name: 'chat', friendId: match[1] } : { name: 'list' }
}

export default function MobileApp() {
  const [screen, setScreen] = useState<Screen>(() => screenFromHash())
  const { friends, loading, error, refresh } = useFriends()

  const openChat = useCallback((friendId: string) => {
    window.history.pushState({ screen: 'chat', friendId }, '', `#chat=${friendId}`)
    setScreen({ name: 'chat', friendId })
  }, [])

  const backToList = useCallback(() => {
    setScreen({ name: 'list' })
    void refresh()
  }, [refresh])

  const openSettings = useCallback(() => {
    window.history.pushState({ screen: 'settings' }, '', '#settings=1')
    setScreen({ name: 'settings' })
  }, [])

  const openLogs = useCallback(() => {
    window.history.pushState({ screen: 'logs' }, '', '#logs=1')
    setScreen({ name: 'logs' })
  }, [])

  const openSync = useCallback(() => {
    window.history.pushState({ screen: 'sync' }, '', '#sync=1')
    setScreen({ name: 'sync' })
  }, [])

  const openKnowledge = useCallback((friendId: string) => {
    window.history.pushState({ screen: 'knowledge', friendId }, '', `#knowledge=${friendId}`)
    setScreen({ name: 'knowledge', friendId })
  }, [])

  const openPersona = useCallback((friendId: string) => {
    window.history.pushState({ screen: 'persona', friendId }, '', `#persona=${friendId}`)
    setScreen({ name: 'persona', friendId })
  }, [])

  const openSchedules = useCallback((friendId: string) => {
    window.history.pushState({ screen: 'schedules', friendId }, '', `#schedules=${friendId}`)
    setScreen({ name: 'schedules', friendId })
  }, [])

  const goBack = useCallback(() => {
    if (window.history.state?.screen) window.history.back()
    else backToList()
  }, [backToList])

  // D194：手机壳一进来就把情绪音效抓成本地 blob（一个会话只拉一次）——
  // 不然每次换表情都要现拉 200~400KB，立绘动作演完了那一声才到。
  useEffect(() => {
    void preloadMoodSounds()
  }, [])

  // D196：回到前台 / 系统说网络恢复 → **立刻再拉一次**。
  // 以前这里什么都不做：手机在兜里被系统挂起一会儿，切回来页面就一直卡着（用户实测
  // "断一下就一直拉不下来"）。现在至少把当前这屏的数据重新拉一遍。
  useEffect(() => {
    const wake = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    window.addEventListener('online', wake)
    document.addEventListener('visibilitychange', wake)
    return () => {
      window.removeEventListener('online', wake)
      document.removeEventListener('visibilitychange', wake)
    }
  }, [refresh])

  // 安卓返回键 / 手势返回 / 浏览器返回 → 退一层
  useEffect(() => {
    const onPop = () => {
      const next = screenFromHash()
      setScreen(next)
      // v3.8（T3.8-1）：**回到列表就重拉一次** —— 不然预览文本/时间/排序都是进来之前的
      // （系统返回/手势返回走的是这条路，以前只切屏不重拉；只有"没有历史时"那条路才会 refresh）
      if (next.name === 'list') void refresh()
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [refresh])

  if (screen.name === 'chat') {
    return (
      <div className="mx-auto min-h-dvh w-full max-w-[560px] bg-page">
        <ChatScreen
          friendId={screen.friendId}
          onBack={goBack}
          onOpenKnowledge={() => openKnowledge(screen.friendId)}
          onOpenLogs={openLogs}
          onOpenPersona={() => openPersona(screen.friendId)}
          onOpenSchedules={() => openSchedules(screen.friendId)}
        />
      </div>
    )
  }

  if (screen.name === 'knowledge') {
    const name = friends.find((item) => item.id === screen.friendId)?.name ?? '她'
    return (
      <div className="mx-auto min-h-dvh w-full max-w-[560px] bg-page">
        <KnowledgeScreen friendId={screen.friendId} friendName={name} onBack={goBack} />
      </div>
    )
  }

  if (screen.name === 'persona') {
    return (
      <div className="mx-auto min-h-dvh w-full max-w-[560px] bg-page">
        <PersonaScreen friendId={screen.friendId} onBack={goBack} onChanged={() => void refresh()} />
      </div>
    )
  }

  if (screen.name === 'logs') {
    return (
      <div className="mx-auto min-h-dvh w-full max-w-[560px] bg-page">
        <LogScreen onBack={goBack} />
      </div>
    )
  }

  if (screen.name === 'schedules') {
    const name = friends.find((item) => item.id === screen.friendId)?.name ?? '她'
    return (
      <div className="mx-auto min-h-dvh w-full max-w-[560px] bg-page">
        <ScheduleScreen friendId={screen.friendId} friendName={name} onBack={goBack} />
      </div>
    )
  }

  if (screen.name === 'settings') {
    return (
      <div className="mx-auto min-h-dvh w-full max-w-[560px] bg-page">
        <SettingsScreen onBack={goBack} />
      </div>
    )
  }

  if (screen.name === 'sync') {
    return (
      <div className="mx-auto min-h-dvh w-full max-w-[560px] bg-page">
        <SyncScreen onBack={goBack} />
      </div>
    )
  }

  return (
    <div className="mx-auto min-h-dvh w-full max-w-[560px] bg-page">
      <FriendListScreen
        friends={friends}
        loading={loading}
        error={error}
        onOpen={openChat}
        onOpenSettings={openSettings}
        onOpenSync={openSync}
        onRefresh={() => void refresh()}
      />
    </div>
  )
}
