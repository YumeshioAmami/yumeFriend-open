/** v4.3（D249）：朋友的"温度" —— 越高越跳、越低越稳。
 *
 * 只写这一个组件，桌面「编辑人格」「创建朋友」和手机「人格」三处共用。
 * `null` = 跟随默认（主对话 0.7 / 主动消息 0.8，跟以前一模一样）。
 * 这里只管"放不放飞"，性格本身还是写在人格里（宪法§1：性格决定一切）。
 */

const DEFAULT_TEMPERATURE = 0.7
const MIN = 0
const MAX = 1.5

export default function TemperatureField({
  value,
  onChange,
  disabled = false,
}: {
  value: number | null
  onChange: (value: number | null) => void
  disabled?: boolean
}) {
  const follow = value === null
  const shown = value ?? DEFAULT_TEMPERATURE
  return (
    <div className="rounded-2xl border border-line/70 bg-surface/40 p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11.5px] text-ink">她的温度</span>
        <span className="text-[10.5px] text-faint">
          越高越跳、越低越稳（放不放飞，不管性格本身）
        </span>
        <label className="ml-auto flex shrink-0 items-center gap-1 text-[11px] text-muted">
          <input
            type="checkbox"
            className="accent-[var(--color-accent)]"
            checked={follow}
            disabled={disabled}
            onChange={(e) => onChange(e.target.checked ? null : shown)}
          />
          跟随默认
        </label>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <input
          type="range"
          min={MIN}
          max={MAX}
          step={0.05}
          value={shown}
          disabled={disabled || follow}
          onChange={(e) => onChange(Number(e.target.value))}
          className="h-1.5 flex-1 accent-[var(--color-accent)] disabled:opacity-40"
        />
        <span className="w-[52px] shrink-0 text-right text-[12px] text-muted tabular-nums">
          {follow ? '默认' : shown.toFixed(2)}
        </span>
      </div>
      <p className="mt-1 text-[10.5px] leading-relaxed text-faint">
        默认 0.7。像 Rick 那种"疯"的角色可以调到 1.0~1.1；想让她更稳就往下调。
      </p>
    </div>
  )
}
