/**
 * 创建朋友（v2 改成弹窗；以前是塞在左栏里的一个小表单）。
 *
 * 两件事：
 * · 左栏那个表单太挤，人格是要写好几行的地方，得给它地方
 * · **头像可选**：可以传（先裁再传），也可以不传——没头像就用名字首字（D83 精神：不配也能用）
 */

import { useState } from 'react'

import { api } from '../shared/api'
import Avatar from '../shared/Avatar'
import ImageCropper from '../shared/ImageCropper'
import TemperatureField from '../shared/TemperatureField'

export default function CreateFriendModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (id: string, warning?: string) => void
}) {
  const [name, setName] = useState('')
  const [personality, setPersonality] = useState('')
  // v4.3（D249）：她的温度（null = 跟随默认）
  const [temperature, setTemperature] = useState<number | null>(null)
  const [cropSource, setCropSource] = useState<File | null>(null)
  const [avatar, setAvatar] = useState<File | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function takeCroppedFile(cropped: File) {
    setAvatar(cropped)
    setCropSource(null)
    if (preview) URL.revokeObjectURL(preview)
    setPreview(URL.createObjectURL(cropped))
  }

  async function save() {
    if (busy || !name.trim()) return
    setBusy(true)
    setError(null)
    try {
      const created = await api.createFriend({
        name: name.trim(),
        personality,
        temperature,
      })
      // 朋友已经建好了；头像传不上去也**不算失败**——只提醒一句，别把这条记录回滚
      if (avatar) {
        try {
          await api.uploadAvatar(created.id, avatar)
        } catch (e) {
          onCreated(created.id, `朋友建好了，但头像没传上去：${e instanceof Error ? e.message : e}`)
          return
        }
      }
      onCreated(created.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim/40 p-3 backdrop-blur-sm">
        <div className="flex h-[86%] w-[560px] max-w-[96%] flex-col overflow-hidden rounded-2xl border border-line bg-page shadow-2xl">
          <div className="flex shrink-0 items-center px-4 py-2.5">
            <h2 className="text-[14px] font-medium text-ink">创建朋友</h2>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="ml-auto rounded-lg px-2 py-0.5 text-faint hover:bg-chip hover:text-muted"
            >
              ✕
            </button>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto px-4 pb-3">
            {/* 头像：可选，先裁再传；不选就用名字首字 */}
            <div className="flex items-center gap-3 rounded-2xl border border-line/70 bg-surface/40 p-2.5">
              <Avatar name={name || '?'} url={preview} size={48} />
              <div className="min-w-0 flex-1">
                <p className="text-[11.5px] text-ink">头像</p>
                <p className="mt-0.5 text-[10.5px] text-faint">
                  可以不传。不传就用名字首字，之后随时能换。
                </p>
              </div>
              <label className="shrink-0 cursor-pointer rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent">
                {preview ? '换一张' : '上传'}
                <input
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    e.target.value = ''
                    if (file) setCropSource(file)
                  }}
                />
              </label>
              {preview && (
                <button
                  type="button"
                  onClick={() => {
                    URL.revokeObjectURL(preview)
                    setPreview(null)
                    setAvatar(null)
                  }}
                  className="shrink-0 rounded-full px-2 py-0.5 text-[11px] text-faint transition-colors hover:text-red-500"
                >
                  不要了
                </button>
              )}
            </div>

            <label className="block">
              <span className="mb-1 block text-[11px] text-muted">名字</span>
              <input
                autoFocus
                className="w-full rounded-xl border border-line bg-surface/80 px-3 py-1.5 text-[13px] outline-none transition-colors focus:border-accent/60"
                placeholder="她叫什么"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>

            <label className="block">
              <span className="mb-1 block text-[11px] text-muted">
                人格（自由文本：性格、说话方式、癖好…… 之后也能改）
              </span>
              <textarea
                className="h-[220px] w-full resize-none rounded-xl border border-line bg-surface/80 px-3 py-2 text-[13px] leading-relaxed outline-none transition-colors focus:border-accent/60"
                placeholder={'# 身份\n你是……\n\n# 说话习惯\n……'}
                value={personality}
                onChange={(e) => setPersonality(e.target.value)}
              />
            </label>

            {/* v4.3（D249）：她的温度（不想要就不用管，默认就行） */}
            <TemperatureField value={temperature} onChange={setTemperature} disabled={busy} />
          </div>

          <div className="flex shrink-0 items-center gap-2 border-t border-line/60 px-4 py-2.5">
            {error && <span className="text-[11.5px] text-red-600">出错了：{error}</span>}
            <div className="ml-auto flex gap-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-xl border border-line-strong px-3 py-1.5 text-[12.5px] text-muted transition-colors hover:border-stone-400"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void save()}
                disabled={busy || !name.trim()}
                className="rounded-xl bg-accent px-4 py-1.5 text-[12.5px] font-medium text-white transition-opacity disabled:opacity-40"
              >
                {busy ? '创建中…' : '创建'}
              </button>
            </div>
          </div>
        </div>
      </div>

      {cropSource && (
        <ImageCropper
          file={cropSource}
          onCancel={() => setCropSource(null)}
          onConfirm={takeCroppedFile}
        />
      )}
    </>
  )
}
