/**
 * "我"叫什么（v3.0 / B9 / D142）。
 *
 * 以前存在浏览器 localStorage → 电脑和手机各叫各的。现在存电脑的 DB：
 * 手机改完，电脑上（沉浸模式里点那个 ✎）也是新名字；反过来一样。
 */

import { useCallback, useEffect, useState } from 'react'

import { api } from '../shared/api'

export function useMyName() {
  const [myName, setMyName] = useState('我')

  useEffect(() => {
    api
      .getSettings()
      .then((row) => setMyName(row.my_name || '我'))
      .catch(() => undefined) // 拿不到就用默认"我"，不打扰用户
  }, [])

  const save = useCallback((name: string) => {
    const next = name.trim() || '我'
    setMyName(next)
    void api.patchSettings({ my_name: next }).catch(() => undefined)
  }, [])

  return { myName, save }
}
