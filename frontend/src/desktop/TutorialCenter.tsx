/** 教程中心（D458）：右上角问号打开，按分类看短教程。 */

import { useEffect, useMemo, useState } from 'react'

import {
  TUTORIAL_BY_ID,
  TUTORIAL_CATEGORIES,
  TUTORIALS,
  type TutorialAction,
} from './tutorials'

export default function TutorialCenter({
  open,
  initialArticleId,
  onClose,
  onAction,
}: {
  open: boolean
  initialArticleId?: string | null
  onClose: () => void
  onAction: (action: TutorialAction) => void
}) {
  const [category, setCategory] = useState(() => TUTORIAL_CATEGORIES[0].id)
  const [articleId, setArticleId] = useState(TUTORIALS[0].id)

  useEffect(() => {
    if (!open) return
    const initial = initialArticleId ? TUTORIAL_BY_ID[initialArticleId] : null
    if (initial) {
      setCategory(initial.category)
      setArticleId(initial.id)
    }
  }, [open, initialArticleId])

  const articles = useMemo(
    () => TUTORIALS.filter((article) => article.category === category),
    [category],
  )
  const article = TUTORIAL_BY_ID[articleId] ?? articles[0] ?? TUTORIALS[0]

  if (!open) return null

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-scrim/45 p-3 backdrop-blur-sm">
      <div className="flex h-[78%] w-[900px] max-w-[97%] flex-col overflow-hidden rounded-2xl border border-line bg-page shadow-2xl">
        <div className="flex shrink-0 items-center gap-3 border-b border-line/60 px-4 py-2.5">
          <div className="flex h-7 w-7 items-center justify-center rounded-full border border-accent/25 bg-accent/10 text-[13px] font-medium text-accent">
            ?
          </div>
          <div>
            <h2 className="text-[14px] font-medium text-ink">教程中心</h2>
            <p className="text-[10.5px] text-faint">短篇教程，随时可以关掉继续用</p>
          </div>
          <button
            type="button"
            onClick={() => onAction('restart-welcome')}
            className="ml-auto rounded-full border border-line-strong px-2.5 py-1 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent"
          >
            重播新手引导
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭教程"
            className="rounded-lg px-2 py-0.5 text-faint transition-colors hover:bg-chip hover:text-muted"
          >
            ✕
          </button>
        </div>

        <div className="flex min-h-0 flex-1">
          <nav className="flex w-[132px] shrink-0 flex-col gap-0.5 border-r border-line/60 p-2">
            {TUTORIAL_CATEGORIES.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  setCategory(item.id)
                  const first = TUTORIALS.find((entry) => entry.category === item.id)
                  if (first) setArticleId(first.id)
                }}
                className={`rounded-xl px-2.5 py-1.5 text-left text-[11.5px] transition-colors ${
                  category === item.id
                    ? 'bg-surface/90 font-medium text-ink'
                    : 'text-muted hover:bg-surface/70 hover:text-ink'
                }`}
              >
                {item.label}
              </button>
            ))}
          </nav>

          <div className="flex w-[190px] shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-line/60 bg-surface/20 p-2">
            {articles.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setArticleId(item.id)}
                className={`rounded-xl px-2.5 py-2 text-left transition-colors ${
                  article.id === item.id
                    ? 'bg-accent/10 text-accent'
                    : 'text-muted hover:bg-surface/70 hover:text-ink'
                }`}
              >
                <span className="block text-[11.5px] font-medium">{item.title}</span>
                <span className="mt-0.5 block text-[10px] leading-relaxed text-faint">
                  {item.summary}
                </span>
              </button>
            ))}
          </div>

          <article className="min-w-0 flex-1 overflow-y-auto p-5">
            <h3 className="text-[18px] font-medium text-ink">{article.title}</h3>
            <p className="mt-1 text-[11px] text-faint">{article.summary}</p>
            <pre className="mt-4 whitespace-pre-wrap font-sans text-[12.5px] leading-[1.9] text-muted">
              {article.body}
            </pre>
            {article.actions && article.actions.length > 0 && (
              <div className="mt-5 flex flex-wrap gap-2">
                {article.actions.map((item) => (
                  <button
                    key={item.label}
                    type="button"
                    onClick={() => onAction(item.action)}
                    className="rounded-xl bg-accent px-3.5 py-1.5 text-[11.5px] font-medium text-white transition-opacity hover:opacity-90"
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            )}
          </article>
        </div>
      </div>
    </div>
  )
}
