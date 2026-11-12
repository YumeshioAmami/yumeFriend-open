/**
 * 每个朋友的知识库管理（v2.1 / T2.1-3 / D98）。
 *
 * 增删改查四件事都在这里：
 *   查 = 列表 + 点开看抽出来的文字 ｜ 增 = 选文件 / 拖进来 ｜ 改 = 改名 ｜ 删 = 删（连文件一起）
 *
 * 两条口径跟别处一致：
 * · 抽不出文字不是错误（D83）：那条显示成「没读出文字」+ 一句人话，仍能删掉
 * · 删除要说清后果，删完连原件一起清（宪法第九条）
 */

import { useEffect, useRef, useState } from 'react'

import { api, type Friend, type KnowledgeDoc } from '../shared/api'
import { ACCENT } from '../shared/ui'

const KIND_LABEL: Record<string, string> = {
  txt: '文本',
  md: 'Markdown',
  pdf: 'PDF',
  docx: 'Word',
  image: '图片',
}

function sizeOf(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}

function timeOf(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default function KnowledgeModal({
  friend,
  onClose,
  onChanged,
}: {
  friend: Friend
  onClose: () => void
  onChanged?: () => void
}) {
  const [docs, setDocs] = useState<KnowledgeDoc[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ doc: KnowledgeDoc; text: string } | null>(null)
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null)
  // 删之前**应用内**确认（别用 window.confirm：桌面外壳里它未必弹得出来，实测用户那边删不掉）
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  // 手改抽取出来的文字
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null)
  const [savingText, setSavingText] = useState(false)
  const [dragging, setDragging] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  async function load() {
    try {
      setDocs(await api.listDocuments(friend.id))
    } catch (e) {
      setDocs(null)
      setNotice(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [friend.id])

  async function add(file: File) {
    if (busy) return
    setBusy(true)
    setNotice(null)
    try {
      const doc = await api.uploadDocument(friend.id, file)
      if (doc.status === 'failed') setNotice(`《${doc.name}》${doc.error ?? ''}`)
      await load()
      onChanged?.()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function saveRename() {
    if (!renaming || busy) return
    setBusy(true)
    try {
      await api.renameDocument(friend.id, renaming.id, renaming.name)
      setRenaming(null)
      await load()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(doc: KnowledgeDoc) {
    setBusy(true)
    setConfirmingId(null)
    try {
      await api.deleteDocument(friend.id, doc.id)
      if (preview?.doc.id === doc.id) setPreview(null)
      await load()
      onChanged?.()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function editText(doc: KnowledgeDoc) {
    try {
      const detail = await api.getDocument(friend.id, doc.id)
      setPreview({ doc, text: detail.text || "（这份没读出文字）" })
      setEditing({ id: doc.id, text: detail.text || "" })
      setRenaming(null)
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    }
  }

  async function saveText() {
    if (!editing || savingText) return
    setSavingText(true)
    setNotice(null)
    try {
      await api.updateDocumentText(friend.id, editing.id, editing.text)
      setEditing(null)
      await load()
      const detail = await api.getDocument(friend.id, editing.id)
      setPreview({ doc: detail, text: detail.text || "（这份没读出文字）" })
      onChanged?.()
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setSavingText(false)
    }
  }

  async function open(doc: KnowledgeDoc) {
    try {
      const detail = await api.getDocument(friend.id, doc.id)
      setPreview({ doc, text: detail.text || '（这份没读出文字）' })
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim/40 backdrop-blur-sm">
      <div
        className="relative flex h-[80%] w-[720px] max-w-[94%] flex-col rounded-2xl border border-line bg-page p-5 shadow-2xl"
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          const file = e.dataTransfer.files?.[0]
          if (file) void add(file)
        }}
      >
        {dragging && (
          <div className="pointer-events-none absolute inset-2 z-10 flex items-center justify-center rounded-2xl border-2 border-dashed border-accent/60 bg-page/70 text-[12.5px] text-accent">
            松手就把这份资料加进去
          </div>
        )}

        <div className="mb-3 flex items-center gap-2">
          <h2 className="text-[15px] font-medium text-ink">{friend.name} 的知识库</h2>
          <span className="text-[11px] text-faint">只有她能看到这些资料</span>
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={busy}
            className="ml-auto rounded-xl px-3 py-1 text-[12px] font-medium text-white disabled:opacity-40"
            style={{ background: ACCENT }}
          >
            {busy ? '处理中…' : '+ 添加资料'}
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-2 py-0.5 text-faint hover:bg-chip hover:text-muted"
          >
            ✕
          </button>
        </div>

        <input
          ref={fileRef}
          type="file"
          accept=".txt,.md,.pdf,.docx,image/*"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0]
            e.target.value = ''
            if (file) void add(file)
          }}
        />

        {notice && <p className="mb-2 text-[11.5px] text-amber-600">{notice}</p>}

        <div className="flex min-h-0 flex-1 gap-3">
          <div className="flex w-[46%] min-w-0 flex-col rounded-xl border border-line/70 bg-surface/40">
            <div className="flex-1 overflow-y-auto">
              {docs === null && <p className="p-3 text-[11.5px] text-faint">读取中…</p>}
              {docs?.length === 0 && (
                <div className="p-4 text-center">
                  <p className="text-[12.5px] text-muted">还没有资料</p>
                  <p className="mt-1 text-[11px] leading-relaxed text-faint">
                    把手上的文件丢给她（拖进来也行）——聊天时她会自己翻，找不到会直说没写。
                  </p>
                </div>
              )}
              {docs?.map((doc) => (
                <div
                  key={doc.id}
                  className="border-b border-line/50 px-3 py-2 last:border-0 hover:bg-surface-hover"
                >
                  {renaming?.id === doc.id ? (
                    <div className="flex items-center gap-1.5">
                      <input
                        autoFocus
                        data-role="kb-rename"
                        value={renaming.name}
                        onChange={(e) => setRenaming({ id: doc.id, name: e.target.value })}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') void saveRename()
                          if (e.key === 'Escape') setRenaming(null)
                        }}
                        className="min-w-0 flex-1 rounded-lg border border-line bg-surface/80 px-2 py-0.5 text-[12px] outline-none focus:border-accent/60"
                      />
                      <button
                        type="button"
                        onClick={() => void saveRename()}
                        className="shrink-0 text-[11px] text-accent"
                      >
                        保存
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void open(doc)}
                      className="block w-full text-left"
                    >
                      <span className="flex items-center gap-1.5">
                        <span
                          className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                            doc.status === 'ready' ? 'bg-accent' : 'bg-faint'
                          }`}
                          aria-hidden
                        />
                        <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink">
                          {doc.name}
                        </span>
                      </span>
                      <span className="mt-0.5 block text-[10.5px] text-faint">
                        {KIND_LABEL[doc.kind] ?? doc.kind} · {sizeOf(doc.bytes)} ·{' '}
                        {doc.status === 'ready' ? `${doc.chars} 字` : '没读出文字'} ·{' '}
                        {timeOf(doc.created_at)}
                      </span>
                    </button>
                  )}
                  {confirmingId === doc.id ? (
                    <div className="mt-1 flex items-center gap-2">
                      <span className="text-[10.5px] text-red-500">连原件一起删，确定？</span>
                      <button
                        type="button"
                        onClick={() => void remove(doc)}
                        className="text-[10.5px] font-medium text-red-500 hover:underline"
                      >
                        确定删掉
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmingId(null)}
                        className="text-[10.5px] text-faint hover:text-muted"
                      >
                        算了
                      </button>
                    </div>
                  ) : (
                    <div className="mt-1 flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => setRenaming({ id: doc.id, name: doc.name })}
                        className="text-[10.5px] text-faint hover:text-accent"
                      >
                        改名
                      </button>
                      <button
                        type="button"
                        onClick={() => void editText(doc)}
                        className="text-[10.5px] text-faint hover:text-accent"
                      >
                        改内容
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmingId(doc.id)}
                        className="text-[10.5px] text-faint hover:text-red-500"
                      >
                        删掉
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="flex min-w-0 flex-1 flex-col rounded-xl border border-line/70 bg-surface/40 p-3">
            {preview && editing?.id === preview.doc.id ? (
              <>
                <p className="text-[12px] text-ink">{preview.doc.name} · 改内容</p>
                <p className="mt-0.5 text-[10.5px] text-faint">
                  改的是抽出来的文字（她看到的就是它），原件文件不动；保存后会重新切块
                </p>
                <textarea
                  data-role="kb-text"
                  value={editing.text}
                  onChange={(e) => setEditing({ id: editing.id, text: e.target.value })}
                  className="mt-2 min-h-0 flex-1 resize-none rounded-lg border border-line bg-page/60 p-2 text-[11.5px] leading-relaxed text-muted outline-none focus:border-accent/60"
                />
                <div className="mt-2 flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => setEditing(null)}
                    className="rounded-xl border border-line-strong px-3 py-1 text-[11.5px] text-muted hover:border-stone-400"
                  >
                    取消
                  </button>
                  <button
                    type="button"
                    onClick={() => void saveText()}
                    disabled={savingText}
                    className="rounded-xl px-3 py-1 text-[11.5px] font-medium text-white disabled:opacity-40"
                    style={{ background: ACCENT }}
                  >
                    {savingText ? '保存中…' : '保存'}
                  </button>
                </div>
              </>
            ) : preview ? (
              <>
                <p className="text-[12px] text-ink">{preview.doc.name}</p>
                <p className="mt-0.5 text-[10.5px] text-faint">
                  {preview.doc.chars} 字 —— 抽出来的原文，她聊天时看到的就是它
                </p>
                <pre className="mt-2 min-h-0 flex-1 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-page/60 p-2 text-[11.5px] leading-relaxed text-muted">
                  {preview.text.slice(0, 20000)}
                </pre>
              </>
            ) : (
              <p className="m-auto text-center text-[11.5px] leading-relaxed text-faint">
                点左边任一份资料
                <br />
                看它抽出来是什么样
              </p>
            )}
          </div>
        </div>

        <p className="mt-3 shrink-0 text-[10.5px] leading-relaxed text-faint">
          支持 txt / md / pdf / Word / 图片（图片要配「视觉」模型才能读）。资料是她的参考材料，
          不会变成长记忆；她记不记得住你的事，还是走「记忆」那套。
        </p>
      </div>
    </div>
  )
}
