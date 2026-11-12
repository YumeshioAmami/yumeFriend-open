/**
 * SSE 读取（LLM协议§3.2 的事件表）。
 *
 * 为什么不用 EventSource：它只能发 GET，而发消息必须 POST。
 * 所以用 fetch + ReadableStream 自己解帧。
 */

export interface SseFrame {
  event: string
  data: Record<string, unknown>
}

function parseFrame(raw: string): SseFrame | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of raw.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }
  if (dataLines.length === 0) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) as Record<string, unknown> }
  } catch {
    return null
  }
}

export async function* readSse(response: Response): AsyncGenerator<SseFrame> {
  const body = response.body
  if (!body) return
  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let index = buffer.indexOf('\n\n')
      while (index >= 0) {
        const frame = parseFrame(buffer.slice(0, index))
        buffer = buffer.slice(index + 2)
        if (frame) yield frame
        index = buffer.indexOf('\n\n')
      }
    }
    const tail = parseFrame(buffer)
    if (tail) yield tail
  } finally {
    reader.releaseLock()
  }
}
