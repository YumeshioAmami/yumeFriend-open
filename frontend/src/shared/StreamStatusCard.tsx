/**
 * 出错了 / 她在调工具 / 她在想什么（D134）：**沉浸模式和语音通话共用的一张卡片**。
 *
 * 用户原话："如果 llm 没连上他会直接跳过变成我在听，结果我都不知道他报错在干等……
 * 调用工具也需要显示出来，即使有一些破坏沉浸感，但这是开发者所必要的。"
 * v3.2（D144）：**手机聊天屏也挂它**；而且"被打断（partial）"也要说出来 ——
 * 用户报"手机端输出失败就是一片空白"，什么都不说比一条报错更糟。
 *
 * 所以：左上角常驻一小块（有内容才出现），把**错误 / 工具调用 / 思考**都摊开来说，
 * 不藏、不静默。样式沿用深色半透明胶囊，尽量不挡立绘。
 */

import type { StreamState } from './useChat'
import { stripMoodMarks } from './ui'

export default function StreamStatusCard({
  stream,
  className = 'left-4 top-14 max-w-[380px]',
}: {
  stream?: StreamState
  /** 放哪儿由调用方说了算（手机、沉浸、通话各自躲开自己的顶栏） */
  className?: string
}) {
  const error = stream?.status === 'error' ? (stream.error ?? '出错了') : null
  // v3.2（D144）：被打断 / 连接断了 —— 也要有表现，并留下已经说出口的那半句
  const partial =
    stream?.status === 'partial' ? stripMoodMarks(stream.content ?? '').trim() : null
  const tools = stream?.tools ?? []
  const thinking =
    stream?.status === 'thinking' && stream.reasoning ? stream.reasoning : null
  if (!error && !partial && tools.length === 0 && !thinking) return null

  return (
    <div className={`absolute z-50 flex flex-col gap-1.5 ${className}`}>
      {error && (
        <div className="rounded-xl border border-red-400/60 bg-red-950/85 px-3 py-2 text-[11.5px] text-red-100 shadow-lg backdrop-blur">
          <div className="font-medium">出错了（这次没答上）</div>
          <div className="mt-0.5 whitespace-pre-wrap break-words text-red-200/90">{error}</div>
        </div>
      )}
      {partial && (
        <div className="rounded-xl border border-amber-300/50 bg-amber-950/85 px-3 py-2 text-[11.5px] text-amber-50 shadow-lg backdrop-blur">
          <div className="font-medium">这段被打断了（没说完）</div>
          <div className="mt-0.5 line-clamp-3 whitespace-pre-wrap break-words text-amber-100/85">
            {partial}
          </div>
        </div>
      )}
      {tools.length > 0 && (
        <div className="rounded-xl border border-white/15 bg-black/60 px-3 py-1.5 font-mono text-[10.5px] text-white/80 backdrop-blur">
          {tools.map((tool) => (
            <div key={tool.id} className="truncate" title={JSON.stringify(tool.args)}>
              ⚙ {tool.name}({JSON.stringify(tool.args)})
            </div>
          ))}
        </div>
      )}
      {thinking && (
        <details className="rounded-xl border border-white/15 bg-black/50 px-3 py-1.5 text-[10.5px] text-white/60 backdrop-blur">
          <summary className="cursor-pointer select-none">她在想…（点开看）</summary>
          <p className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap">{thinking}</p>
        </details>
      )}
    </div>
  )
}
