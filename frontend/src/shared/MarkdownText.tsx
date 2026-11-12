/** 受控 Markdown 渲染（D269）。
 *
 * 只实现聊天里常用的子集：标题 / 列表 / 引用 / 代码块 / 行内代码 /
 * 粗体 / 斜体 / 删除线 / 链接 / 分隔线 / 换行。**不注入 HTML**，
 * 所有文本都走 React 节点；LaTeX 仍交给现有的 `MathText`（D247）。
 *
 * 刻意不做完整 CommonMark：表格、嵌套列表等先不铺开，避免把聊天正文
 * 变成一套难控的排版系统。流式输出时未闭合的标记会自然退化成普通文本。
 */

import type { ReactNode } from 'react'

import { MathText } from './MathText'
import { splitMath } from './mathSplit'

type Tone = 'normal' | 'inverse'

type Block =
  | { type: 'paragraph'; text: string }
  | { type: 'heading'; level: number; text: string }
  | { type: 'code'; text: string; lang: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'quote'; text: string }
  | { type: 'hr' }

const FENCE = /^\s*```(.*)$/
const HEADING = /^(#{1,6})\s+(.*)$/
const QUOTE = /^\s*>\s?(.*)$/
const UL = /^\s*[-*+]\s+(.*)$/
const OL = /^\s*\d+[.)]\s+(.*)$/
const HR = /^\s*(?:---+|\*\*\*+|___+)\s*$/

function isBlockStart(line: string): boolean {
  const trimmed = line.trim()
  return (
    trimmed === '' ||
    FENCE.test(line) ||
    HEADING.test(line) ||
    QUOTE.test(line) ||
    HR.test(line) ||
    UL.test(line) ||
    OL.test(line)
  )
}

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const blocks: Block[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (line.trim() === '') {
      index += 1
      continue
    }

    const fence = FENCE.exec(line)
    if (fence) {
      const lang = fence[1].trim()
      index += 1
      const body: string[] = []
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        body.push(lines[index])
        index += 1
      }
      if (index < lines.length) index += 1
      blocks.push({ type: 'code', text: body.join('\n'), lang })
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      blocks.push({
        type: 'heading',
        level: heading[1].length,
        text: heading[2].trim(),
      })
      index += 1
      continue
    }

    if (HR.test(line)) {
      blocks.push({ type: 'hr' })
      index += 1
      continue
    }

    if (QUOTE.test(line)) {
      const quoteLines: string[] = []
      while (index < lines.length) {
        const match = QUOTE.exec(lines[index])
        if (!match) break
        quoteLines.push(match[1])
        index += 1
      }
      blocks.push({ type: 'quote', text: quoteLines.join('\n') })
      continue
    }

    const ul = UL.exec(line)
    const ol = OL.exec(line)
    if (ul || ol) {
      const ordered = Boolean(ol)
      const items: string[] = []
      while (index < lines.length) {
        const match = ordered ? OL.exec(lines[index]) : UL.exec(lines[index])
        if (!match) break
        items.push(match[1])
        index += 1
      }
      blocks.push({ type: 'list', ordered, items })
      continue
    }

    const paragraph: string[] = [line]
    index += 1
    while (index < lines.length && !isBlockStart(lines[index])) {
      paragraph.push(lines[index])
      index += 1
    }
    blocks.push({ type: 'paragraph', text: paragraph.join('\n') })
  }

  return blocks
}

function safeHref(url: string): string | null {
  return /^(https?:\/\/|mailto:)/i.test(url.trim()) ? url.trim() : null
}

function inlineNodes(text: string, key: string, tone: Tone): ReactNode[] {
  const out: ReactNode[] = []
  let cursor = 0
  let serial = 0

  const patterns: Array<{
    re: RegExp
    render: (match: RegExpExecArray) => ReactNode
  }> = [
    {
      re: /`([^`\n]+)`/,
      render: (match) => (
        <code
          key={`${key}-code-${serial++}`}
          className={
            tone === 'inverse'
              ? 'rounded bg-white/15 px-1 py-0.5 font-mono text-[0.9em]'
              : 'rounded bg-black/5 px-1 py-0.5 font-mono text-[0.9em]'
          }
        >
          {match[1]}
        </code>
      ),
    },
    {
      re: /\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/,
      render: (match) => {
        const href = safeHref(match[2])
        if (!href) return match[0]
        return (
          <a
            key={`${key}-link-${serial++}`}
            href={href}
            target="_blank"
            rel="noreferrer"
            className={
              tone === 'inverse'
                ? 'text-white underline decoration-white/50 underline-offset-2'
                : 'text-accent underline decoration-accent/40 underline-offset-2'
            }
          >
            {inlineNodes(match[1], `${key}-link-${serial}`, tone)}
          </a>
        )
      },
    },
    {
      re: /\*\*([^*\n]+)\*\*/,
      render: (match) => (
        <strong key={`${key}-strong-${serial++}`}>
          {inlineNodes(match[1], `${key}-strong-${serial}`, tone)}
        </strong>
      ),
    },
    {
      re: /__([^_\n]+)__/,
      render: (match) => (
        <strong key={`${key}-strong-${serial++}`}>
          {inlineNodes(match[1], `${key}-strong-${serial}`, tone)}
        </strong>
      ),
    },
    {
      re: /~~([^~\n]+)~~/,
      render: (match) => (
        <del key={`${key}-del-${serial++}`}>
          {inlineNodes(match[1], `${key}-del-${serial}`, tone)}
        </del>
      ),
    },
    {
      re: /\*([^*\n]+)\*/,
      render: (match) => (
        <em key={`${key}-em-${serial++}`}>
          {inlineNodes(match[1], `${key}-em-${serial}`, tone)}
        </em>
      ),
    },
  ]

  while (cursor < text.length) {
    let best: { index: number; match: RegExpExecArray; pattern: (typeof patterns)[number] } | null =
      null
    const rest = text.slice(cursor)
    for (const pattern of patterns) {
      const match = pattern.re.exec(rest)
      if (match && (best === null || match.index < best.index)) {
        best = { index: match.index, match, pattern }
      }
    }
    if (!best) {
      out.push(text.slice(cursor))
      break
    }
    if (best.index > 0) out.push(rest.slice(0, best.index))
    out.push(best.pattern.render(best.match))
    cursor += best.index + best.match[0].length
  }

  return out
}

function inlineRichWithBreaks(text: string, key: string, tone: Tone): ReactNode[] {
  const out: ReactNode[] = []
  splitMath(text).forEach((node, index) => {
    if (node.type !== 'text') {
      const wrapped = node.type === 'block' ? `$$${node.value}$$` : `$${node.value}$`
      out.push(<MathText key={`${key}-math-${index}`} text={wrapped} />)
      return
    }
    node.value.split('\n').forEach((line, lineIndex) => {
      if (lineIndex > 0) out.push(<br key={`${key}-br-${index}-${lineIndex}`} />)
      out.push(...inlineNodes(line, `${key}-line-${index}-${lineIndex}`, tone))
    })
  })
  return out
}

function codeClass(tone: Tone): string {
  return tone === 'inverse'
    ? 'my-2 overflow-x-auto rounded-xl bg-white/10 p-2.5 font-mono text-[12px] leading-relaxed'
    : 'my-2 overflow-x-auto rounded-xl bg-black/5 p-2.5 font-mono text-[12px] leading-relaxed'
}

export function MarkdownText({
  text,
  tone = 'normal',
}: {
  text: string
  tone?: Tone
}) {
  const blocks = parseBlocks(text)
  return (
    <div className="markdown-text break-words">
      {blocks.map((block, index) => {
        const key = `block-${index}`
        if (block.type === 'paragraph') {
          return (
            <p key={key} className="m-0 whitespace-pre-wrap">
              {inlineRichWithBreaks(block.text, key, tone)}
            </p>
          )
        }
        if (block.type === 'heading') {
          const size =
            block.level === 1
              ? 'text-[1.25em]'
              : block.level === 2
                ? 'text-[1.15em]'
                : 'text-[1.05em]'
          return (
            <p key={key} className={`mb-1 mt-2 font-semibold ${size}`}>
              {inlineRichWithBreaks(block.text, key, tone)}
            </p>
          )
        }
        if (block.type === 'code') {
          return (
            <pre key={key} className={codeClass(tone)}>
              <code>{block.text}</code>
            </pre>
          )
        }
        if (block.type === 'list') {
          const listClass = `my-1 space-y-0.5 pl-5 ${
            block.ordered ? 'list-decimal' : 'list-disc'
          }`
          const items = block.items.map((item, itemIndex) => (
            <li key={`${key}-item-${itemIndex}`}>
              {inlineRichWithBreaks(item, `${key}-item-${itemIndex}`, tone)}
            </li>
          ))
          return (
            block.ordered ? (
              <ol key={key} className={listClass}>
                {items}
              </ol>
            ) : (
              <ul key={key} className={listClass}>
                {items}
              </ul>
            )
          )
        }
        if (block.type === 'quote') {
          return (
            <blockquote
              key={key}
              className={`my-2 border-l-2 pl-3 ${
                tone === 'inverse'
                  ? 'border-white/40 text-white/80'
                  : 'border-line-strong/60 text-muted'
              }`}
            >
              {inlineRichWithBreaks(block.text, key, tone)}
            </blockquote>
          )
        }
        return (
          <hr
            key={key}
            className={`my-2 border-0 border-t ${
              tone === 'inverse' ? 'border-white/30' : 'border-line/60'
            }`}
          />
        )
      })}
    </div>
  )
}
