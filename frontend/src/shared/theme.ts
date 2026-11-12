/**
 * 夜间模式（用户要的：整体换成夜间风格，但**设计理念不变**）。
 *
 * 做法：组件里不写死颜色，全用 `index.css` 里的语义色板
 * （page / surface / ink / line / accent…）；这里只负责往 `<html>` 上挂
 * `data-theme="dark"`，色板自动整套换掉。
 *
 * 所以"换主题"= 换色板，不是换设计：暖色调、单一强调色、玻璃质感、
 * 动效时长、克制度，全都一模一样。
 */

export type ThemeChoice = 'light' | 'dark' | 'system'

const STORAGE_KEY = 'yumefriend.theme'

export function readThemeChoice(): ThemeChoice {
  const saved = localStorage.getItem(STORAGE_KEY)
  return saved === 'light' || saved === 'dark' || saved === 'system' ? saved : 'system'
}

export function saveThemeChoice(choice: ThemeChoice): void {
  localStorage.setItem(STORAGE_KEY, choice)
}

export function resolveTheme(choice: ThemeChoice): 'light' | 'dark' {
  if (choice === 'system') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  }
  return choice
}

export function applyTheme(choice: ThemeChoice): 'light' | 'dark' {
  const theme = resolveTheme(choice)
  document.documentElement.dataset.theme = theme
  return theme
}
