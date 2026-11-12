/**
 * 平板壳（v3.16 / D200）—— 同一个 `/m/` 入口，宽屏时换成"左列表 + 右聊天"两栏。
 *
 * 为什么**复用手机端组件**、不搬电脑端那套：电脑端的"按住说话"走浏览器
 * `getUserMedia`，安卓 WebView 里起不来（手机端因此才做了原生录音桥 `yfShell`）。
 * 复用手机端组件 → 平板上麦克风 / 通话 / 存图照常能用。
 *
 * 手机端不受影响：判定不命中时渲染的还是原来的 `MobileApp`（见 `main.tsx`）。
 * 子页面（人格 / 知识库 / 日程 / 设置 / 日志）整屏盖上去；沉浸 / 通话由 ChatScreen 自己全屏。
 */

import { useCallback, useState } from 'react'

import ChatScreen from './ChatScreen'
import FriendListScreen from './FriendListScreen'
import KnowledgeScreen from './KnowledgeScreen'
import LogScreen from './LogScreen'
import PersonaScreen from './PersonaScreen'
import ScheduleScreen from './ScheduleScreen'
import SettingsScreen from './SettingsScreen'
import SyncScreen from './SyncScreen'
import { useFriends } from './useFriends'

type Overlay =
  | null
  | { name: 'knowledge'; friendId: string }
  | { name: 'persona'; friendId: string }
  | { name: 'schedules'; friendId: string }
  | { name: 'logs' }
  | { name: 'settings' }
  | { name: 'sync' }

export default function TabletApp() {
  const { friends, loading, error, refresh } = useFriends()
  // 支持 `/m/#chat=<id>` 直达（跟手机端一个口径；沉浸 / 通话的 hash 由 ChatScreen 自己认）
  const [selectedId, setSelectedId] = useState<string | null>(() => {
    const match = /(?:^#|&)chat=([0-9a-fA-F-]{8,})/.exec(window.location.hash)
    return match ? match[1] : null
  })
  const [overlay, setOverlay] = useState<Overlay>(null)

  const selected = friends.find((item) => item.id === selectedId) ?? null
  const closeOverlay = useCallback(() => {
    setOverlay(null)
    void refresh()
  }, [refresh])
  const nameOf = (friendId: string) =>
    friends.find((item) => item.id === friendId)?.name ?? '她'

  return (
    <div className="flex h-dvh w-full overflow-hidden bg-page">
      {/* 左栏：朋友列表（复用手机端那一屏，塞进固定宽的侧栏里） */}
      <aside className="flex h-full w-[320px] shrink-0 flex-col border-r border-line/60">
        <div className="h-full overflow-y-auto">
          <FriendListScreen
            friends={friends}
            loading={loading}
            error={error}
            selectedId={selectedId}
            onOpen={(friendId) => setSelectedId(friendId)}
            onOpenSettings={() => setOverlay({ name: 'settings' })}
            onOpenSync={() => setOverlay({ name: 'sync' })}
            onRefresh={() => void refresh()}
          />
        </div>
      </aside>

      {/* 右栏：那个朋友的聊天（embedded = 不用返回箭头，因为左边就是列表） */}
      <main className="relative flex min-w-0 flex-1 flex-col">
        {selected ? (
          <ChatScreen
            key={selected.id}
            friendId={selected.id}
            embedded
            onBack={() => setSelectedId(null)}
            onOpenKnowledge={() => setOverlay({ name: 'knowledge', friendId: selected.id })}
            onOpenLogs={() => setOverlay({ name: 'logs' })}
            onOpenPersona={() => setOverlay({ name: 'persona', friendId: selected.id })}
            onOpenSchedules={() => setOverlay({ name: 'schedules', friendId: selected.id })}
          />
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center gap-1.5">
            <p className="text-[14px] text-muted">从左边选一个朋友</p>
            <p className="text-[12px] text-faint">手机 / 平板 / 电脑各自本地保存，按需同步</p>
          </div>
        )}
      </main>

      {/* 子页面：整屏盖上去（跟手机上一样是整屏，平板上也不挤） */}
      {overlay && (
        <div className="fixed inset-0 z-50 bg-page">
          {overlay.name === 'knowledge' && (
            <KnowledgeScreen
              friendId={overlay.friendId}
              friendName={nameOf(overlay.friendId)}
              onBack={closeOverlay}
            />
          )}
          {overlay.name === 'persona' && (
            <PersonaScreen
              friendId={overlay.friendId}
              onBack={closeOverlay}
              onChanged={() => void refresh()}
            />
          )}
          {overlay.name === 'schedules' && (
            <ScheduleScreen
              friendId={overlay.friendId}
              friendName={nameOf(overlay.friendId)}
              onBack={closeOverlay}
            />
          )}
          {overlay.name === 'logs' && <LogScreen onBack={closeOverlay} />}
          {overlay.name === 'settings' && <SettingsScreen onBack={closeOverlay} />}
          {overlay.name === 'sync' && <SyncScreen onBack={closeOverlay} />}
        </div>
      )}
    </div>
  )
}
