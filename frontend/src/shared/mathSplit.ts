/** LaTeX 切分（D247）：把一段纯文本切成「文本 / 行内公式 / 整段公式」。
 *
 * 单独抽成纯函数（跟 React 无关）：切分逻辑好测、也能复用（将来语音、桌宠要处理公式时）。
 *
 * 支持的写法：`$...$`（行内）、`$$...$$`（整段）、`\(...\)`、`\[...\]`。
 * 两条刻意的取舍：
 *   · **没闭合的公式一律当普通文本** —— 流式输出时半截公式不会闪成红色报错；
 *   · 行内 `$...$` 要求内容**不以空白开头/结尾、不含换行** ——
 *     这样"花了 $5 和 $10"这种（内容以空格收尾）不会被误当成公式。
 */

export type MathNode =
  | { type: 'text'; value: string }
  | { type: 'inline' | 'block'; value: string }

function nonSpace(ch: string | undefined): boolean {
  return !!ch && !/\s/.test(ch)
}

export function splitMath(text: string): MathNode[] {
  const nodes: MathNode[] = []
  let buffer = ''
  const flush = () => {
    if (buffer) {
      nodes.push({ type: 'text', value: buffer })
      buffer = ''
    }
  }
  let i = 0
  while (i < text.length) {
    if (text.startsWith('$$', i)) {
      const end = text.indexOf('$$', i + 2)
      if (end > i + 2) {
        flush()
        nodes.push({ type: 'block', value: text.slice(i + 2, end) })
        i = end + 2
        continue
      }
    }
    if (text.startsWith('\\[', i)) {
      const end = text.indexOf('\\]', i + 2)
      if (end > i + 2) {
        flush()
        nodes.push({ type: 'block', value: text.slice(i + 2, end) })
        i = end + 2
        continue
      }
    }
    if (text.startsWith('\\(', i)) {
      const end = text.indexOf('\\)', i + 2)
      if (end > i + 2) {
        flush()
        nodes.push({ type: 'inline', value: text.slice(i + 2, end) })
        i = end + 2
        continue
      }
    }
    if (text[i] === '$') {
      const end = text.indexOf('$', i + 1)
      if (end > i + 1) {
        const body = text.slice(i + 1, end)
        if (nonSpace(body[0]) && nonSpace(body[body.length - 1]) && !body.includes('\n')) {
          flush()
          nodes.push({ type: 'inline', value: body })
          i = end + 1
          continue
        }
      }
    }
    buffer += text[i]
    i += 1
  }
  flush()
  return nodes
}
