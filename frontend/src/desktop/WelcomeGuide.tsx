/** 首次启动三步引导（D458）。 */

export default function WelcomeGuide({
  open,
  chatReady,
  friendCount,
  onClose,
  onOpenModels,
  onCreateFriend,
}: {
  open: boolean
  chatReady: boolean
  friendCount: number
  onClose: () => void
  onOpenModels: () => void
  onCreateFriend: () => void
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim/35 p-4 backdrop-blur-[2px]">
      <div className="w-[560px] max-w-full overflow-hidden rounded-2xl border border-line bg-page shadow-2xl">
        <div className="border-b border-line/60 px-5 py-4">
          <div className="text-[11px] font-medium tracking-[0.14em] text-accent">
            WELCOME TO YUMEFRIEND
          </div>
          <h2 className="mt-1 text-[19px] font-medium text-ink">先做三件事就可以开始了</h2>
          <p className="mt-1 text-[11.5px] leading-relaxed text-faint">
            不用一次配完。先能聊天，语音、Live2D、小世界以后慢慢加。
          </p>
        </div>
        <div className="space-y-3 px-5 py-4">
          <div className="flex items-start gap-3 rounded-xl border border-line/60 bg-surface/50 p-3">
            <span
              className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] ${
                chatReady ? 'bg-emerald-100 text-emerald-700' : 'bg-accent/10 text-accent'
              }`}
            >
              {chatReady ? '✓' : '1'}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[12.5px] font-medium text-ink">连接聊天模型</p>
              <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">
                填服务地址、密钥和模型名。任何 OpenAI 兼容服务都行。
              </p>
            </div>
            <button
              type="button"
              onClick={onOpenModels}
              className="shrink-0 rounded-lg border border-line-strong px-2.5 py-1 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
            >
              {chatReady ? '已连接' : '去连接'}
            </button>
          </div>
          <div className="flex items-start gap-3 rounded-xl border border-line/60 bg-surface/50 p-3">
            <span
              className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] ${
                friendCount > 0 ? 'bg-emerald-100 text-emerald-700' : 'bg-accent/10 text-accent'
              }`}
            >
              {friendCount > 0 ? '✓' : '2'}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[12.5px] font-medium text-ink">创建第一个朋友</p>
              <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">
                给她写名字和人格；人格越具体，她越像你描述的人。
              </p>
            </div>
            <button
              type="button"
              onClick={onCreateFriend}
              className="shrink-0 rounded-lg border border-line-strong px-2.5 py-1 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
            >
              {friendCount > 0 ? '已有朋友' : '创建朋友'}
            </button>
          </div>
          <div className="flex items-start gap-3 rounded-xl border border-line/60 bg-surface/50 p-3">
            <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent/10 text-[11px] text-accent">
              3
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[12.5px] font-medium text-ink">发第一句话</p>
              <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">
                选中朋友，在下方输入框说话。电脑端按 ↑ 可以翻回上一句。
              </p>
            </div>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-line/60 px-5 py-3.5">
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl bg-accent px-4 py-1.5 text-[11.5px] font-medium text-white transition-opacity hover:opacity-90"
          >
            开始使用
          </button>
          <button
            type="button"
            onClick={onClose}
            className="rounded-xl border border-line-strong px-3.5 py-1.5 text-[11.5px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
          >
            跳过，之后看教程
          </button>
          <span className="ml-auto text-[10.5px] text-faint">右上角 ? 随时打开教程中心</span>
        </div>
      </div>
    </div>
  )
}
