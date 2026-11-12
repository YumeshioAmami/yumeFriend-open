import { useCallback, useRef, useState, type KeyboardEvent } from 'react'

/**
 * 输入框 ↑ / ↓ 历史（D449，仅桌面端接入）。
 *
 * 口径：
 * · ↑ 只在"输入框为空"或"光标在开头"时接管，不抢正常的光标移动；
 * · 第一次 ↑ 记住当前草稿，翻到最新后再按 ↓ 就恢复它；
 * · 用户一改字就调用 reset()，避免历史游标和手改后的草稿打架。
 */
export function useInputHistory(history: string[]) {
  const [cursor, setCursor] = useState<number | null>(null)
  const savedDraft = useRef('')

  const reset = useCallback(() => {
    setCursor(null)
    savedDraft.current = ''
  }, [])

  const onArrow = useCallback(
    (
      draft: string,
      event: KeyboardEvent<HTMLTextAreaElement>,
      apply: (text: string) => void,
    ): boolean => {
      if (event.key === 'ArrowUp') {
        const atStart =
          event.currentTarget.selectionStart === 0 && event.currentTarget.selectionEnd === 0
        // 已经翻出来的历史句允许继续往上翻；用户手改过就不再抢 ↑。
        const recalled = cursor !== null && history[cursor] === draft
        if (!atStart && draft.length > 0 && !recalled) return false
        if (history.length === 0) return false
        event.preventDefault()
        if (cursor === null) {
          savedDraft.current = draft
          const next = history.length - 1
          setCursor(next)
          apply(history[next])
        } else {
          const next = Math.max(0, cursor - 1)
          setCursor(next)
          apply(history[next])
        }
        return true
      }

      if (event.key === 'ArrowDown' && cursor !== null) {
        event.preventDefault()
        const next = cursor + 1
        if (next >= history.length) {
          setCursor(null)
          apply(savedDraft.current)
        } else {
          setCursor(next)
          apply(history[next])
        }
        return true
      }

      return false
    },
    [cursor, history],
  )

  return { cursor, onArrow, reset }
}
