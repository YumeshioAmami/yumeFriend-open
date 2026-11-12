/** 桌面端「手机 / 平板」说明（v7.0 / D300）：独立单机端 + 手动同步。 */

export default function MobilePanel() {
  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-[12px] font-medium text-ink">手机 / 平板是独立 App</h3>
        <p className="mt-1 text-[11px] leading-relaxed text-muted">
          它们现在有自己的数据库、Agent 和语音，不再加载电脑网页。电脑关机时也能聊天。
        </p>
      </div>

      <div className="space-y-2 rounded-xl border border-line bg-surface-hover/40 p-4 text-[12px] leading-relaxed text-muted">
        <p>① 把 APK 装到手机 / 平板。</p>
        <p>② 首次打开后，在设置里填聊天模型 Key。</p>
        <p>③ 两台设备都打开「设置 → 同步」，选设备、输入对方屏幕上的 6 位码。</p>
        <p>④ 默认手机是主动消息/提醒的主设备；平板和电脑待机。</p>
      </div>

      <div className="rounded-xl border border-line p-4">
        <p className="text-[12px] font-medium text-ink">下载手机安装包</p>
        <p className="mt-1 text-[11px] text-muted">
          电脑版和手机端现在分开打包，电脑 zip 不再内置 APK。手机安装包请从资源页下载：
        </p>
        <a
          href="https://www.yumeshio.cn/uploads/yumefriend/"
          target="_blank"
          rel="noreferrer"
          className="mt-2 inline-flex rounded-lg bg-accent px-3 py-2 text-[12px] text-white"
        >
          打开资源页
        </a>
      </div>

      <p className="text-[11px] leading-relaxed text-faint">
        同步走同一 Wi-Fi 的临时连接，不经过服务器；API Key、日志、语音缓存、游戏数据不会同步。
      </p>
    </div>
  )
}
