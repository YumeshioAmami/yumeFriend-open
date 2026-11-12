/** 带 LaTeX 渲染的文本（D247）。
 *
 * 她的回复里可能出现数学公式（行内 `$...$` / 整段 `$$...$$`）—— 这里用 **KaTeX 本地渲染**
 * （字体 / CSS 都打进包里，不联网）。普通文字**原样**走 React 节点（不注入 HTML），
 * 只有公式那一小段用 KaTeX 生成的 HTML。
 *
 * 渲染不出来（非法公式 / 半截公式）→ KaTeX 自己画成红色报错，**绝不吞掉原文**。
 */

import katex from 'katex'
import 'katex/dist/katex.min.css'

import { splitMath } from './mathSplit'

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function renderMath(tex: string, display: boolean): string {
  try {
    return katex.renderToString(tex, {
      displayMode: display,
      throwOnError: false,
      errorColor: '#cc0000',
      strict: false,
      trust: false,
      output: 'html',
    })
  } catch {
    return escapeHtml(tex)
  }
}

export function MathText({ text }: { text: string }) {
  const nodes = splitMath(text)
  return (
    <>
      {nodes.map((node, index) =>
        node.type === 'text' ? (
          <span key={index}>{node.value}</span>
        ) : (
          <span
            key={index}
            className={
              node.type === 'block' ? 'my-1 block overflow-x-auto text-center' : undefined
            }
            dangerouslySetInnerHTML={{ __html: renderMath(node.value, node.type === 'block') }}
          />
        ),
      )}
    </>
  )
}
