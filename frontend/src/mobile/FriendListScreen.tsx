/**
 * 手机壳的第一屏：**朋友列表**（v3.0 / B3；用户要的"手机 QQ 那种"）。
 *
 * 一屏就够：她 / 头像 + 名字 + 最后一句预览 + 时间 + 未读红点 + 在场状态。
 * 点一个人进聊天（B4）。右上角是设置（B6）。
 *
 * 触控约定（D140）：可点的东西 ≥44px、字号比桌面大一档。
 */

import Avatar from '../shared/Avatar'
import { IconDatabase, IconGear, IconRefresh } from '../shared/icons'
import { ACCENT, stripMoodMarks } from '../shared/ui'
import type { Friend } from '../shared/api'
import { listTime } from './useFriends'

export default function FriendListScreen({
  friends,
  loading,
  error,
  selectedId,
  onOpen,
  onOpenSettings,
  onOpenSync,
  onRefresh,
}: {
  friends: Friend[]
  loading: boolean
  error: string | null
  /** 平板两栏里高亮"当前正看着的朋友"（D200）；不传 = 老行为（不高亮） */
  selectedId?: string | null
  onOpen: (friendId: string) => void
  onOpenSettings: () => void
  onOpenSync: () => void
  onRefresh: () => void
}) {
  const totalUnread = friends.reduce((sum, friend) => sum + (friend.unread_count || 0), 0)

  return (
    <div className="min-h-dvh bg-page pb-[env(safe-area-inset-bottom)]">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-line/70 bg-page/95 px-4 pb-3 pt-[max(12px,env(safe-area-inset-top))] backdrop-blur">
        <div className="flex items-center gap-2.5">
          <h1 className="text-[19px] font-medium text-ink">朋友</h1>
          {totalUnread > 0 && (
            <span className="rounded-full bg-accent/12 px-2 py-[3px] text-[11.5px] text-accent">
              {totalUnread > 99 ? '99+' : totalUnread} 条未读
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onOpenSync}
            aria-label="同步"
            className="flex h-11 w-11 items-center justify-center rounded-2xl border border-line bg-surface/80 text-muted active:bg-surface-hover"
          >
            <IconDatabase size={17} />
          </button>
          <button
            type="button"
            onClick={onRefresh}
            aria-label="刷新"
            className="flex h-11 w-11 items-center justify-center rounded-2xl border border-line bg-surface/80 text-muted active:bg-surface-hover"
          >
            <IconRefresh size={17} />
          </button>
          <button
            type="button"
            onClick={onOpenSettings}
            aria-label="设置"
            className="flex h-11 w-11 items-center justify-center rounded-2xl border border-line bg-surface/80 text-muted active:bg-surface-hover"
          >
            <IconGear size={17} />
          </button>
        </div>
      </header>

      {loading && friends.length === 0 && (
        <p className="px-5 py-10 text-center text-[13px] text-muted">正在拉朋友列表…</p>
      )}

      {error && (
        <div className="mx-4 mt-4 rounded-2xl border border-accent/40 bg-accent/8 p-4">
          <p className="text-[13px] text-accent">本地服务出了点问题（{error}）</p>
          <p className="mt-1.5 text-[12px] leading-relaxed text-muted">
            先退出重开 App；还不行就从同步页看看日志。
          </p>
        </div>
      )}

      {!loading && !error && friends.length === 0 && (
        <p className="px-5 py-10 text-center text-[13px] text-muted">
          还没有朋友 —— 先从电脑同步，或者新建一个。
        </p>
      )}

      <ul>
        {friends.map((friend) => (
          <li key={friend.id}>
            <button
              type="button"
              onClick={() => onOpen(friend.id)}
              className={`flex w-full items-center gap-3 overflow-hidden border-b border-line/60 px-4 py-3 text-left active:bg-surface-hover ${
                selectedId === friend.id ? 'bg-surface-hover' : ''
              }`}
            >
              <span className="relative">
                <Avatar name={friend.name} url={friend.avatar_url} size={52} />
                {friend.presence && (
                  <span
                    aria-label="在场"
                    className="absolute -bottom-0.5 -right-0.5 h-3.5 w-3.5 rounded-full border-2 border-page bg-emerald-500"
                  />
                )}
              </span>

              <span className="flex min-w-0 flex-1 flex-col">
                <span className="flex items-baseline gap-2">
                  <span className="min-w-0 truncate text-[16px] font-medium text-ink">
                    {friend.name}
                  </span>
                  {friend.has_unfinished && (
                    <span className="shrink-0 text-[11px] text-accent">正在说…</span>
                  )}
                </span>
                <span className="mt-0.5 block truncate text-[13px] text-muted">
                  {friend.last_message_preview
                    ? stripMoodMarks(friend.last_message_preview)
                    : '还没聊过'}
                </span>
              </span>

              <span className="flex shrink-0 flex-col items-end gap-1.5">
                <span className="text-[11.5px] text-faint">
                  {listTime(friend.last_message_at)}
                </span>
                {friend.unread_count > 0 ? (
                  <span
                    className="flex h-[20px] min-w-[20px] items-center justify-center rounded-full px-1.5 text-[11px] font-medium text-white"
                    style={{ background: ACCENT }}
                  >
                    {friend.unread_count > 99 ? '99+' : friend.unread_count}
                  </span>
                ) : (
                  <span className="h-[20px]" />
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>

      <p className="px-5 py-6 text-center text-[11.5px] leading-relaxed text-faint">
        手机 / 平板 / 电脑各有本地数据；点右上角同步，手动对齐。
      </p>
    </div>
  )
}
