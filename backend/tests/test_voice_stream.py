"""v3.11（D178）：本地语音那四件 —— 洗字符 / 流式边写边发 / 提前通知 / 占位头。

背景（实测）：合成本身 ≈ 1:1 实时（6 字 2.0s / 25 字 6.0s / 60 字 11.1s），
而 emoji 会让上游直接 400（`'gbk' codec can't encode character '\U0001f60f'`）。
"""

from __future__ import annotations

import asyncio
import threading
import time
import wave
from pathlib import Path

import pytest

from backend.services import tts_service, voice_reply_service


def _pcm(seconds: float = 0.2, rate: int = 32000) -> bytes:
    return b"\x00\x00" * int(rate * seconds)


def test_speakable_text_drops_emoji_and_other_gbk_hostile_chars():
    """emoji（GBK 编不出来）必须被洗掉 —— 不然上游直接 400，整段语音废掉。"""
    cleaned = voice_reply_service.speakable_text("在呢😏，你说呀😊～（尾巴晃）")
    assert "😏" not in cleaned and "😊" not in cleaned
    assert "在呢" in cleaned and "你说呀" in cleaned
    assert "～" in cleaned  # 波浪号 GBK 能编 → 留着
    assert voice_reply_service.speakable_text("もう、しょうがないな") == "もう、しょうがないな"
    assert voice_reply_service.speakable_text("好  😏  的") == "好 的"


def test_wav_header_placeholder_then_fixed(tmp_path):
    """占位头（长度未知）→ 收尾修成真实长度；修完 wave 模块能正常读。"""
    target = tmp_path / "seg.wav"
    pcm = _pcm(0.5)
    target.write_bytes(tts_service._wav_header(rate=32000, data_bytes=0xFFFFFFFF) + pcm)
    # 占位头期间别拿它当正常文件用：wave 模块会当成"超长音频"（所以收尾必须修头）
    with wave.open(str(target)) as broken:
        assert broken.getnframes() > len(pcm)
    assert tts_service.fix_wav_header(target) == len(pcm)
    with wave.open(str(target)) as fh:
        assert fh.getnframes() == len(pcm) // 2
        assert fh.getframerate() == 32000


def test_follow_segment_streams_a_growing_file(tmp_path):
    """尾随流：文件一边长一边往外发，写完就收工（前端据此"边生成边播"）。"""
    message_id, index = "m1", 0
    target = tmp_path / "0.wav"
    target.write_bytes(tts_service._wav_header(rate=32000, data_bytes=0xFFFFFFFF))
    key = voice_reply_service._stream_key(message_id, index)
    voice_reply_service._streaming.add(key)

    def writer() -> None:
        for _ in range(5):
            time.sleep(0.05)
            with target.open("ab") as fh:
                fh.write(_pcm(0.05))
        time.sleep(0.05)
        tts_service.fix_wav_header(target)
        voice_reply_service._streaming.discard(key)

    threading.Thread(target=writer, daemon=True).start()
    chunks = list(
        voice_reply_service.follow_segment(target, message_id, index, poll=0.02, idle_seconds=5)
    )
    streamed = b"".join(chunks)
    final = target.read_bytes()
    # 发出去的是**占位头 + 全部 PCM**（这正是"边生成边播"要的：浏览器不必等长度确定）
    assert streamed[:4] == b"RIFF"
    assert streamed[44:] == final[44:], "PCM 数据一个字节都不能少"
    assert len(chunks) >= 3, "应该是边写边发，不是等写完一次给"


class _FakeVoice:
    """`_snapshot()` 要的那几个字段（够用就行）。"""

    id = "v1"
    name = "假音色"
    gpt_weights_path = "a.ckpt"
    sovits_weights_path = "b.pth"
    ref_audio_path = "ref.wav"
    prompt_text = "参考台词"
    speed = 1.0


async def test_stream_synth_emits_early_event_and_stores_segment(tmp_path, monkeypatch):
    """`_synth` 要：① 先推"开始合成了"（前端能马上开播）② 写下来的那份要能拼/能读时长。"""
    events: list[dict] = []

    class _Manager:
        async def broadcast(self, event: dict) -> None:
            events.append(event)

    monkeypatch.setattr(voice_reply_service, "manager", _Manager(), raising=False)
    monkeypatch.setattr(
        voice_reply_service, "segment_path_of", lambda mid, idx: tmp_path / f"{idx}.wav"
    )
    monkeypatch.setattr(
        voice_reply_service, "segment_rel_path", lambda mid, idx: f"audio/{mid}/{idx}.wav"
    )

    def fake_stream_to(*, text, voice, base_url, sample_steps, path, opener=None):
        pcm = _pcm(0.3)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(tts_service._wav_header(rate=32000, data_bytes=0xFFFFFFFF) + pcm)
        return len(pcm)

    monkeypatch.setattr(tts_service, "synthesize_stream_to", fake_stream_to)

    stream = voice_reply_service.StreamingVoice(
        message_id="m9",
        friend_id="f9",
        voice=voice_reply_service._snapshot(_FakeVoice()),
        base_url="http://127.0.0.1:9880",
        sample_steps=8,
    )
    stream._dispatch("在呢，你说。")
    await asyncio.gather(*stream._tasks)

    assert events and events[0].get("streaming") is True, "第一条应该是开始合成"
    assert events[-1]["audio_duration_ms"], "最后一条应该带真实时长"
    assert stream._results[0][:4] == b"RIFF"


def test_cold_start_prewarm_runs_when_service_is_up(monkeypatch, tmp_path):
    """冷启动预热（D178）：启动时若服务在、且有朋友开着语音 → 后台热一遍它的音色。"""
    from backend import app as app_module

    calls: list[tuple] = []
    monkeypatch.setattr(tts_service, "status", lambda base, force=False: {"available": True})
    monkeypatch.setattr(
        tts_service, "spawn_prewarm", lambda vid, base, steps: calls.append((vid, base, steps))
    )

    class _Session:
        def scalars(self, *_a, **_k):  # noqa: ANN002, ANN003
            class _R:
                def first(self):  # noqa: ANN201
                    class _F:
                        voice_id = "v-1"

                    return _F()

            return _R()

        def get(self, *_a, **_k):  # noqa: ANN002, ANN003
            return object()

    class _Scope:
        def __enter__(self):  # noqa: ANN204
            return _Session()

        def __exit__(self, *_a):  # noqa: ANN002
            return False

    monkeypatch.setattr(app_module, "session_scope", lambda: _Scope())
    monkeypatch.setattr(
        app_module.settings_service, "effective_purposes_from", lambda _row: {}
    )
    monkeypatch.setattr(app_module.tts_service, "tts_settings", lambda _p: {
        "base_url": "http://127.0.0.1:9880", "sovits_dir": "", "sample_steps": 8
    })

    asyncio.run(app_module._warm_tts_voice())
    assert calls and calls[0][0] == "v-1", "应该把那个朋友的音色排上预热"


class _MissingVoice:
    id = "vm"
    name = "文件没了的音色"
    gpt_weights_path = "D:/nonexistent/x-e10.ckpt"
    sovits_weights_path = "D:/nonexistent/y.pth"
    ref_audio_path = "ref.wav"
    prompt_text = "台词"
    speed = 1.0


def test_ensure_weights_refuses_missing_files(tmp_path):
    """D179：权重文件不在 → **绝不拿去切**。

    事故：上游的 `set_gpt_weights` 会把路径**持久化进它自己的配置文件** ——
    拿一个幽灵路径去切，会把这个服务永久弄坏（用户实测：下次启动直接崩）。
    """
    from backend.services.errors import ServiceError

    tts_service.reset_cache()
    with pytest.raises(ServiceError) as caught:
        tts_service._ensure_weights("http://127.0.0.1:9", _MissingVoice())
    assert "权重文件不在了" in str(caught.value)


def test_stream_header_uses_the_upstream_sample_rate(tmp_path):
    """D180：**头必须按上游说的写**。

    事故：我原来写死 32000，而 GPT-SoVITS 实际流的是 **48000** → 所有流式音频以 0.667 倍速播放
    （听感整体低一个音区，用户报"所有音色都是大叔音"）。
    """
    # 假的"上游流"：首块是 48kHz 的 WAV 头，后面给 PCM
    header = tts_service._wav_header(rate=48000, data_bytes=0)
    pcm = _pcm(0.2)
    out = tmp_path / "s.wav"
    # D179 的护栏要求"权重文件真实存在" → 造两个假的放临时目录
    gpt = tmp_path / "a.ckpt"; gpt.write_bytes(b"fake")
    sov = tmp_path / "b.pth"; sov.write_bytes(b"fake")
    voice = _StreamVoice()
    voice.gpt_weights_path = str(gpt)
    voice.sovits_weights_path = str(sov)
    voice.ref_audio_path = str(gpt)  # 够用：流式这条路不看 ref 文件本身
    tts_service.reset_cache()
    # 权重"已经切好了"（跳过真的去连服务那一步）—— 这条测的是"头的采样率"
    tts_service._loaded["key"] = f"{voice.gpt_weights_path}|{voice.sovits_weights_path}"
    n = tts_service.synthesize_stream_to(
        text="你好", voice=voice, base_url="http://127.0.0.1:9", sample_steps=8,
        path=out, opener=iter([header, pcm]),
    )
    assert n == len(pcm)
    assert tts_service.parse_wav_format(out.read_bytes())["rate"] == 48000, "头要跟上游一致"
    assert tts_service.fix_wav_header(out) == len(pcm)
    assert tts_service.parse_wav_format(out.read_bytes())["rate"] == 48000  # 修头不改变采样率


class _StreamVoice:
    id = "vs"
    name = "假音色"
    gpt_weights_path = "a.ckpt"
    sovits_weights_path = "b.pth"
    ref_audio_path = "ref.wav"
    prompt_text = "台词"
    speed = 1.0


def test_parse_wav_format_reads_rate_channels_width():
    raw = tts_service._wav_header(rate=44100, data_bytes=0) + b"\x00\x00"
    fmt = tts_service.parse_wav_format(raw)
    assert fmt == {"rate": 44100, "channels": 1, "width": 2}
    assert tts_service.parse_wav_format(b"not a wav") is None


def test_tts_prewarm_can_be_switched_off_for_check_runs():
    """v3.14（D182 附带）：验收实例**不许**去切用户正在跑的语音服务。

    实测踩过：`check_packaged.ps1` 起的那个实例（带真数据副本）一启动就跑冷启动预热 →
    调上游的 set_*_weights（写操作）→ 把用户服务里的音色改成了别人的。
    """
    from backend.app import NO_TTS_PREWARM_ENV, tts_prewarm_enabled

    assert tts_prewarm_enabled({}) is True, "默认还是要预热（D178）"
    assert tts_prewarm_enabled({NO_TTS_PREWARM_ENV: "1"}) is False
    assert tts_prewarm_enabled({NO_TTS_PREWARM_ENV: "true"}) is False
    assert tts_prewarm_enabled({NO_TTS_PREWARM_ENV: "0"}) is True
    assert tts_prewarm_enabled({NO_TTS_PREWARM_ENV: ""}) is True


def test_stream_falls_back_when_upstream_gives_no_header(tmp_path):
    """上游连头都没给（纯 PCM）→ 用兜底格式写头，数据一个字节都不能丢。

    守的是一个真踩过的坑：`_WAV_FMT_FALLBACK` 当时**没定义**（`NameError` 被
    `synthesize_stream_to` 的兜底 except 吞掉 → 静默退回整段合成），这条用例把它钉住。
    """
    gpt = tmp_path / "a.ckpt"; gpt.write_bytes(b"fake")
    sov = tmp_path / "b.pth"; sov.write_bytes(b"fake")
    voice = _StreamVoice()
    voice.gpt_weights_path = str(gpt)
    voice.sovits_weights_path = str(sov)
    tts_service.reset_cache()
    tts_service._loaded["key"] = f"{voice.gpt_weights_path}|{voice.sovits_weights_path}"

    pcm = _pcm(0.2, rate=tts_service.STREAM_SAMPLE_RATE_FALLBACK)
    out = tmp_path / "no-head.wav"
    n = tts_service.synthesize_stream_to(
        text="你好", voice=voice, base_url="http://127.0.0.1:9", sample_steps=8,
        path=out, opener=iter([pcm]),
    )
    assert n == len(pcm), "没有头的上游流也要当成数据收下"
    assert tts_service.parse_wav_format(out.read_bytes())["rate"] == (
        tts_service.STREAM_SAMPLE_RATE_FALLBACK
    )


def test_fix_wav_header_survives_an_unparsable_header(tmp_path):
    """头读不出来时也得能修长度（别 NameError）—— 兜底格式上场。"""
    target = tmp_path / "garbled.wav"
    target.write_bytes(b"RIFF" + b"\x00" * 80)
    assert tts_service.parse_wav_format(target.read_bytes()) is None
    assert tts_service.fix_wav_header(target) == target.stat().st_size - 44
