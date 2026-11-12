/**
 * 连接状态（2026-09-27 / D196）。
 *
 * 背景（用户实测）：手机在外面走流量，组网/信号一抖，页面就"请求发出去没人回"。
 * 以前 `request()` **既没有超时、也没有重试** —— 一次断掉就永远卡着，看起来就是
 * "数据拉不下来"（哪怕其实早就通了）。
 *
 * 这里只干一件事：让界面知道"现在连不上"，并给一个立刻恢复的抓手。
 * 只由 `api.ts` 的请求层驱动：**成功 = 在线，网络类失败 = 断开**（4xx/5xx 不算）。
 */

type Listener = (down: boolean) => void

let down = false
const listeners = new Set<Listener>()

function emit(): void {
  for (const listener of listeners) listener(down)
}

export function isOffline(): boolean {
  return down
}

/** 请求成功了 → 连接是好的。 */
export function markOnline(): void {
  if (!down) return
  down = false
  emit()
}

/** 请求因为"网络/超时"失败（**不是** 4xx/5xx —— 那是后端明确回答了）。 */
export function markOffline(): void {
  if (down) return
  down = true
  emit()
}

export function subscribeNetStatus(listener: Listener): () => void {
  listeners.add(listener)
  listener(down)
  return () => {
    listeners.delete(listener)
  }
}
