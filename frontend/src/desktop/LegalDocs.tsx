/** 设置 → 关于里的三份声明（D459）。 */

import { LEGAL_DOCS } from '../shared/legal'

export default function LegalDocs() {
  return (
    <div className="space-y-2">
      <div>
        <h3 className="text-[12px] font-medium text-ink">用户声明与协议</h3>
        <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">
          使用本软件即表示你理解以下内容。重要事项请自己判断并自行备份数据。
        </p>
      </div>
      {LEGAL_DOCS.map((doc) => (
        <details
          key={doc.id}
          className="rounded-xl border border-line/60 bg-surface/40 px-3 py-2"
        >
          <summary className="cursor-pointer text-[11.5px] font-medium text-ink">
            {doc.title}
            <span className="ml-2 font-normal text-[10.5px] text-faint">{doc.summary}</span>
          </summary>
          <pre className="mt-2 max-h-[30vh] overflow-y-auto whitespace-pre-wrap border-t border-line/50 pt-2 font-sans text-[10.5px] leading-[1.8] text-muted">
            {doc.body}
          </pre>
        </details>
      ))}
    </div>
  )
}
