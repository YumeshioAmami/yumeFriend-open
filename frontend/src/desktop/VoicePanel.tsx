/**
 * 设置里的「语音」页（v2 / T2.0-4，D85 改过一次口径）：**音色库** + 服务状态。
 *
 * 「她什么时候用语音」**不在这里配**——那是每个朋友自己的开关
 * （编辑人格里的「她的回复都用语音」），这一页只解释 + 指路。
 *
 * 三条产品原则（跟别处保持一致）：
 * · **服务没开不是错误**（D83）：显示"未连接 + 怎么办"，不弹报错
 * · 该等的地方要说话：试听要合成几秒（首次更久），必须显示"合成中…"
 * · 删除前说清楚后果（被几个朋友用着），别让人手一抖删了
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  api,
  type SpeechService,
  type SpeechServiceName,
  type TtsStatus,
  type Voice,
  type VoiceCandidate,
} from '../shared/api'
import { IconBell, IconWave } from '../shared/icons'

export default function VoicePanel({
  sovitsDir,
  onSovitsDirChange,
}: {
  sovitsDir: string
  onSovitsDirChange: (dir: string) => void
}) {
  const [status, setStatus] = useState<TtsStatus | null>(null)
  const [voices, setVoices] = useState<Voice[]>([])
  const [candidates, setCandidates] = useState<VoiceCandidate[] | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [playingId, setPlayingId] = useState<string | null>(null)
  // 合成要十几秒，回来时"用户手势"早过期了 → 浏览器会拦自动播放。
  // 所以合成完**亮出原生播放器**（点它一定有手势，保证能响）。
  const [previewAudio, setPreviewAudio] = useState<{ name: string; url: string } | null>(null)
  // v2.1（T2.1-2）：两个本地语音服务的启停
  // undefined = 还没读到（加载中）；null = 读失败。别把"加载中"说成"读不到"
  const [services, setServices] = useState<Record<SpeechServiceName, SpeechService> | null | undefined>(
    undefined,
  )
  const [servicesError, setServicesError] = useState<string | null>(null)
  const [busyService, setBusyService] = useState<SpeechServiceName | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const load = useCallback(async () => {
    try {
      const [s, v] = await Promise.all([api.ttsStatus(), api.listVoices()])
      setStatus(s)
      setVoices(v.voices)
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const loadServices = useCallback(async () => {
    try {
      setServices(await api.speechServices())
      setServicesError(null)
    } catch (e) {
      // 别静默失败（D94 的教训）：说清楚为什么读不到
      setServices(null)
      setServicesError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void loadServices()
  }, [loadServices])

  async function toggleService(name: SpeechServiceName) {
    if (busyService) return
    setBusyService(name)
    setNotice(null)
    try {
      const current = services?.[name]?.state
      const result =
        current === 'running' ? await api.stopSpeechService(name) : await api.startSpeechService(name)
      if (!result.ok && result.hint) setNotice(result.hint)
      await loadServices()
      // 启动要等模型加载，多刷几次状态
      for (let i = 0; i < 8; i++) {
        await new Promise((resolve) => setTimeout(resolve, 1500))
        await loadServices()
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyService(null)
    }
  }

  async function scan() {
    if (scanning) return
    setScanning(true)
    setNotice(null)
    try {
      // 用**当前输入框里的**目录（不必先按保存）
      const result = await api.scanVoices(sovitsDir)
      if (!result.ok) {
        setNotice(result.hint ?? '没扫到')
        setCandidates(null)
        return
      }
      const list = result.candidates ?? []
      setCandidates(list)
      const savedNote = result.saved_dir ? '（目录已经帮你记下了，不用再按保存）' : ''
      setNotice(
        list.length
          ? `扫到 ${list.length} 个${savedNote}`
          : `这个目录里没找到能识别的音色——确认是 GPT-SoVITS 的根目录（里面有 models）${savedNote}`,
      )
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setScanning(false)
    }
  }

  async function importOne(candidate: VoiceCandidate) {
    setBusyId(candidate.gpt_weights_path)
    setNotice(null)
    try {
      await api.createVoice({
        name: candidate.name,
        gpt_weights_path: candidate.gpt_weights_path,
        sovits_weights_path: candidate.sovits_weights_path,
        ref_audio_path: candidate.ref_audio_path,
        prompt_text: candidate.prompt_text,
        speed: candidate.speed,
      })
      setCandidates((prev) => (prev ? prev.filter((c) => c.gpt_weights_path !== candidate.gpt_weights_path) : prev))
      await load()
      setNotice(`已导入「${candidate.name}」`)
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyId(null)
    }
  }

  async function preview(voice: Voice) {
    if (busyId) return
    setBusyId(voice.id)
    setNotice('正在合成…（第一次要十几秒，之后会快很多）')
    try {
      const result = await api.previewVoice(voice.id)
      if (!result.ok || !result.audio_url) {
        setNotice(result.hint ?? '试听没成')
        return
      }
      setNotice(null)
      setPreviewAudio({ name: voice.name, url: result.audio_url })
      setPlayingId(voice.id)
      const audio = audioRef.current
      if (audio) {
        audio.src = result.audio_url
        // 能自动播就自动播；被拦也没关系——下面那个原生播放器点一下就能响
        await audio.play().catch(() => undefined)
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyId(null)
    }
  }

  async function remove(voice: Voice) {
    const extra = voice.used_by > 0 ? `（有 ${voice.used_by} 个朋友在用，删了他们会变回没声音）` : ''
    if (!window.confirm(`删掉音色「${voice.name}」？${extra}\n只删这条记录，不动你磁盘上的模型文件。`)) return
    setBusyId(voice.id)
    try {
      await api.deleteVoice(voice.id)
      await load()
      setNotice(`已删掉「${voice.name}」`)
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    } finally {
      setBusyId(null)
    }
  }

  async function changeSpeed(voice: Voice, speed: number) {
    try {
      await api.patchVoice(voice.id, { speed })
      setVoices((prev) => prev.map((v) => (v.id === voice.id ? { ...v, speed } : v)))
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e))
    }
  }

  const connected = status?.available ?? false

  return (
    <section className="space-y-3">
      <audio
        ref={audioRef}
        controls
        src={previewAudio?.url}
        onEnded={() => setPlayingId(null)}
        className={`w-full ${previewAudio ? '' : 'hidden'}`}
      />
      {previewAudio && (
        <p className="text-[10.5px] text-faint">
          最近试听：{previewAudio.name}（没自动响就点上面那个播放键）
        </p>
      )}

      {/* 服务状态：连不上也要说人话（D83） */}
      <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <IconWave size={15} className={connected ? 'text-accent' : 'text-faint'} />
          <span className="text-[12px] text-ink">语音服务</span>
          <span className="flex items-center gap-1.5 text-[11px] text-faint">
            <span className={`h-1.5 w-1.5 rounded-full ${connected ? 'bg-accent' : 'bg-faint'}`} aria-hidden />
            {connected ? '已连接' : '未连接'}
          </span>
          <span className="text-[10.5px] text-faint">{status?.base_url}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="ml-auto rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent"
          >
            重新检测
          </button>
        </div>
        {!connected && (
          <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
            没连上不影响聊天，只是发不了语音。要开语音：去把 GPT-SoVITS 跑起来
            （你那个「启动洛茜语音.bat」就行），它启动要一分钟左右，然后点「重新检测」。
            {status?.error ? `（服务回的是：${status.error}）` : ''}
          </p>
        )}
      </div>

      {/* v2.1（T2.1-2）：两个本地服务，一键启停 */}
      <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
        <div className="flex items-center gap-2">
          <IconBell size={15} className="text-muted" />
          <span className="text-[12px] text-ink">本地语音服务</span>
          <button
            type="button"
            onClick={() => void loadServices()}
            className="ml-auto rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent"
          >
            刷新
          </button>
        </div>
        {services === undefined ? (
          <p className="mt-1.5 text-[11px] text-faint">读取中…</p>
        ) : services ? (
          <ul className="mt-2 space-y-1.5">
            {(Object.keys(services) as SpeechServiceName[]).map((name) => {
              const item = services[name]
              const label =
                item.state === 'running' ? '已就绪' : item.state === 'starting' ? '启动中…' : '没开'
              return (
                <li key={name} className="flex flex-wrap items-center gap-2 text-[11.5px]">
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      item.state === 'running'
                        ? 'bg-accent'
                        : item.state === 'starting'
                          ? 'bg-amber-400'
                          : 'bg-faint'
                    }`}
                    aria-hidden
                  />
                  <span className="text-ink">{item.label}</span>
                  <span className="text-faint">
                    {label}（:{item.port}）
                  </span>
                  <button
                    type="button"
                    onClick={() => void toggleService(name)}
                    disabled={busyService !== null}
                    className="ml-auto rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
                  >
                    {busyService === name
                      ? '处理中…'
                      : item.state === 'running'
                        ? '停止'
                        : '启动'}
                  </button>
                </li>
              )
            })}
          </ul>
        ) : (
          <p className="mt-1.5 text-[11px] text-faint">
            读不到服务状态{servicesError ? `（${servicesError}）` : ''}
          </p>
        )}
        <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
          这两个都是本机跑的：合成（GPT-SoVITS）和识别（Paraformer，用来「按住说话」）。
          识别第一次启动要加载模型（几秒），之后每句零点几秒。
        </p>
      </div>

      {/* 音色库 */}
      <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[12px] text-ink">音色库</span>
          <span className="text-[10.5px] text-faint">（模型文件留在你磁盘上，这里只存"去哪儿找"）</span>
        </div>

        {/* GPT-SoVITS 装在哪 —— 这个输入框**必须放在这儿**：
            放到「模型」页那个卡片里，用户根本想不到要去那儿填（踩过一次） */}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="shrink-0 text-[11px] text-faint">程序目录</span>
          <input
            className="min-w-0 flex-1 rounded-lg border border-line bg-surface/80 px-2 py-1 text-[11.5px] outline-none transition-colors focus:border-accent/60"
            placeholder="D:\GPT-SoVITS（GPT-SoVITS 整个文件夹的位置）"
            value={sovitsDir}
            onChange={(e) => onSovitsDirChange(e.target.value)}
          />
          <button
            type="button"
            onClick={() => void scan()}
            disabled={scanning}
            className="shrink-0 rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
          >
            {scanning ? '扫描中…' : '扫描导入'}
          </button>
        </div>
        <p className="mt-1 text-[10.5px] text-faint">
          就是 GPT-SoVITS 那个文件夹（里面有 models 目录）。**填完直接点「扫描导入」就行**，
          扫到了会顺手帮你记下来。
        </p>

        {notice && <p className="mt-1.5 text-[11px] leading-relaxed text-muted">{notice}</p>}

        {candidates && candidates.length > 0 && (
          <ul className="mt-2 space-y-1">
            {candidates.map((c) => (
              <li
                key={c.gpt_weights_path}
                className="flex items-center gap-2 rounded-xl border border-dashed border-line-strong/80 px-2.5 py-1.5"
              >
                <span className="shrink-0 text-[11.5px] text-ink">{c.name}</span>
                <span className="min-w-0 flex-1 truncate text-[10.5px] text-faint">{c.prompt_text}</span>
                <button
                  type="button"
                  onClick={() => void importOne(c)}
                  disabled={busyId === c.gpt_weights_path}
                  className="shrink-0 rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
                >
                  {busyId === c.gpt_weights_path ? '导入中…' : '导入'}
                </button>
              </li>
            ))}
          </ul>
        )}

        {voices.length === 0 ? (
          <p className="mt-2 text-[11px] text-faint">
            还没有音色。点右上「扫描导入」——它会去设置里的 GPT-SoVITS 目录找（认标准结构）。
          </p>
        ) : (
          <ul className="mt-2 space-y-1">
            {voices.map((voice) => (
              <li key={voice.id} className="flex items-center gap-2 rounded-xl bg-surface/60 px-2.5 py-1.5">
                <span className="shrink-0 text-[11.5px] text-ink">{voice.name}</span>
                {!voice.files_ok && (
                  <span className="shrink-0 rounded-full bg-chip px-1.5 text-[10px] text-faint">路径失效</span>
                )}
                <span className="min-w-0 flex-1 truncate text-[10.5px] text-faint">{voice.prompt_text}</span>
                <label className="flex shrink-0 items-center gap-1 text-[10.5px] text-faint">
                  语速
                  <input
                    type="number"
                    min={0.5}
                    max={2}
                    step={0.05}
                    value={voice.speed}
                    onChange={(e) => void changeSpeed(voice, Number(e.target.value))}
                    className="w-[52px] rounded-md border border-line bg-surface/80 px-1 py-0.5 text-[10.5px] outline-none focus:border-accent/60"
                  />
                </label>
                <button
                  type="button"
                  onClick={() => void preview(voice)}
                  disabled={busyId === voice.id}
                  className="shrink-0 rounded-full border border-line-strong px-2.5 py-0.5 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent disabled:opacity-40"
                >
                  {busyId === voice.id ? '合成中…' : playingId === voice.id ? '正在播' : '试听'}
                </button>
                <button
                  type="button"
                  onClick={() => void remove(voice)}
                  disabled={busyId === voice.id}
                  title={voice.used_by > 0 ? `${voice.used_by} 个朋友在用` : '没有朋友在用'}
                  className="shrink-0 rounded-full px-1.5 text-[11px] text-faint transition-colors hover:text-red-500 disabled:opacity-40"
                >
                  删掉
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 她什么时候用语音（D85：这是**每个朋友自己的开关**，不在这页配） */}
      <div className="rounded-2xl border border-line/70 bg-surface/40 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <IconBell size={15} className="text-muted" />
          <span className="text-[12px] text-ink">她什么时候用语音</span>
        </div>
        <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
          这是<b className="text-muted">每个朋友自己的开关</b>：在聊天页点「✎ 人格」→「她的声音」里
          打开「她的回复都用语音」，她就会<b className="text-muted">每一句都配</b>；关掉就是纯文字。
          任意一条回复上还能点「念给我听」单独补一条。
        </p>
      </div>
    </section>
  )
}
