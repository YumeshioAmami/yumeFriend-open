/**
 * 手机壳：某个朋友的**知识库**（v3.0 / B7）。
 *
 * 用户点名"知识库我也要" —— 所以增删改查都做，只是按手机的习惯来：
 *   · 上传：系统文件选择器（手机上就是相册/文件/微信文件那一套）
 *   · 改名：**内联**改（不用 window.prompt —— 将来套进 WebView 里 prompt 可能被拦）
 *   · 删除：**两步**（点一下变成"确认删？"），防手滑
 *   · 看抽取出来的文字：点一条展开（她检索时用的就是这份文字）
 *
 * 跟电脑上的区别：**不提供"扫目录导入"**（那是电脑才有的概念，D140 明确砍掉）。
 */

import { useEffect, useRef, useState } from 'react'

import { api, type KnowledgeDoc, type KnowledgeDocDetail } from '../shared/api'

export default function KnowledgeScreen({
  friendId,
  friendName,
  onBack,
}: {
  friendId: string
  friendName: string
  onBack: () => void
}) {
  const [docs, setDocs] = useState<KnowledgeDoc[]>([])
  const [open, setOpen] = useState<KnowledgeDocDetail | null>(null)
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(null)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [hint, setHint] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  async function load() {
    try {
      setDocs(await api.listDocuments(friendId))
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }

  useEffect(() => {
    void load()
  }, [friendId])

  async function upload(file: File | undefined) {
    if (!file) return
    setBusy(true)
    setHint(`正在读《${file.name}》…（PDF / 图片要跑一会儿）`)
    try {
      await api.uploadDocument(friendId, file)
      setHint('加好了')
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function openDoc(id: string) {
    setHint(null)
    try {
      setOpen(await api.getDocument(friendId, id))
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    }
  }

  async function rename() {
    if (!editing) return
    setBusy(true)
    try {
      await api.renameDocument(friendId, editing.id, editing.name.trim())
      setEditing(null)
      setHint('改好了')
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string) {
    setBusy(true)
    try {
      await api.deleteDocument(friendId, id)
      setConfirmId(null)
      setOpen(null)
      setHint('删掉了')
      await load()
    } catch (exc) {
      setHint(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-dvh bg-page pb-[max(16px,env(safe-area-inset-bottom))]">
      <header className="sticky top-0 z-10 flex items-center gap-2 border-b border-line/70 bg-page/95 px-2 pb-2.5 pt-[max(8px,env(safe-area-inset-top))] backdrop-blur">
        <button
          type="button"
          onClick={onBack}
          aria-label="返回"
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-[22px] text-muted active:bg-surface-hover"
        >
          ‹
        </button>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[16px] font-medium text-ink">知识库</p>
          <p className="text-[11.5px] text-muted">{friendName} 的资料（她自己会去翻）</p>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={() => fileRef.current?.click()}
          className="h-11 shrink-0 rounded-2xl border border-line bg-surface px-3.5 text-[13px] text-ink active:bg-surface-hover disabled:opacity-40"
        >
          加资料
        </button>
      </header>

      {hint && <p className="px-4 pt-2 text-[11.5px] text-muted">{hint}</p>}

      <ul className="px-4 pt-2">
        {docs.map((doc) => (
          <li key={doc.id} className="border-b border-line/60 py-2.5">
            {editing?.id === doc.id ? (
              <div className="flex items-center gap-2">
                <input
                  value={editing.name}
                  onChange={(event) => setEditing({ id: doc.id, name: event.target.value })}
                  className="h-11 min-w-0 flex-1 rounded-xl border border-line bg-surface px-3 text-[14px] text-ink"
                />
                <button
                  type="button"
                  onClick={() => void rename()}
                  className="h-11 shrink-0 rounded-xl border border-line bg-surface px-3 text-[13px] text-ink"
                >
                  保存
                </button>
                <button
                  type="button"
                  onClick={() => setEditing(null)}
                  className="h-11 shrink-0 rounded-xl px-3 text-[13px] text-muted"
                >
                  取消
                </button>
              </div>
            ) : (
              <button type="button" onClick={() => void openDoc(doc.id)} className="w-full text-left">
                <span className="block truncate text-[14.5px] text-ink">{doc.name}</span>
                <span className="mt-0.5 block text-[11.5px] text-faint">
                  {doc.kind} · {doc.chars} 字
                  {doc.status === 'failed' ? ' · 没读出文字（她看不了这份）' : ''}
                  {doc.error ? ` · ${doc.error}` : ''}
                </span>
              </button>
            )}

            {editing?.id !== doc.id && (
              <div className="mt-1.5 flex gap-2">
                <button
                  type="button"
                  onClick={() => setEditing({ id: doc.id, name: doc.name })}
                  className="h-9 rounded-xl border border-line bg-surface px-3 text-[12.5px] text-muted active:bg-surface-hover"
                >
                  改名
                </button>
                {confirmId === doc.id ? (
                  <>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void remove(doc.id)}
                      className="h-9 rounded-xl border border-accent bg-accent/10 px-3 text-[12.5px] text-accent"
                    >
                      确认删？
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmId(null)}
                      className="h-9 rounded-xl px-3 text-[12.5px] text-muted"
                    >
                      算了
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    onClick={() => setConfirmId(doc.id)}
                    className="h-9 rounded-xl border border-line bg-surface px-3 text-[12.5px] text-muted active:bg-surface-hover"
                  >
                    删除
                  </button>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>

      {!docs.length && (
        <p className="px-5 py-10 text-center text-[13px] text-muted">
          还没有资料 —— 点右上角「加资料」，把 txt / md / pdf / 图片 丢给她。
        </p>
      )}

      <input
        ref={fileRef}
        type="file"
        accept=".txt,.md,.pdf,.docx,image/*"
        className="hidden"
        onChange={(event) => void upload(event.target.files?.[0])}
      />

      {open && (
        <div className="fixed inset-0 z-40 flex items-end bg-black/40" onClick={() => setOpen(null)}>
          <div
            className="max-h-[80dvh] w-full rounded-t-3xl border-t border-line bg-surface px-4 pb-[max(16px,env(safe-area-inset-bottom))] pt-4"
            onClick={(event) => event.stopPropagation()}
          >
            <p className="truncate text-[15px] font-medium text-ink">{open.name}</p>
            <p className="mt-0.5 text-[11.5px] text-faint">
              {open.kind} · {open.chars} 字 · 她检索时用的就是这份文字
            </p>
            <div className="mt-3 max-h-[56dvh] overflow-y-auto whitespace-pre-wrap rounded-2xl border border-line/70 bg-page/60 p-3 text-[13px] leading-relaxed text-ink-soft">
              {open.text || '（没读出文字）'}
            </div>
            <button
              type="button"
              onClick={() => setOpen(null)}
              className="mt-3 h-12 w-full rounded-2xl border border-line bg-surface text-[14px] text-ink active:bg-surface-hover"
            >
              关闭
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
