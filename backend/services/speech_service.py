"""两个本地语音服务的生命周期（v2.1 / T2.1-2 / D96）。

```
tts  GPT-SoVITS api_v2.py            127.0.0.1:9880   合成（本来就有，这里只管启停）
stt  backend/speech/stt_server.py    127.0.0.1:9881   识别（用 GPT-SoVITS 的 runtime python 跑）
```

三条纪律：

1. **已经开着的别重复起**：先探健康，通了就只认状态（用户自己双击了 .bat 也算）。
2. **停就停干净**：`taskkill /T /F`（连子进程，D67 的僵尸教训）；**退出应用时只收自己起的**。
3. **没开/失败都是人话**（D83）：端点和前端只负责显示，不当异常抛。

转写链路（同步实现 —— 端点用 `def` 跑在线程池里，不堵事件循环）：

    浏览器 webm/opus → 存临时文件 → GPT-SoVITS 自带 ffmpeg.exe 转 16k 单声道 wav
      → POST 127.0.0.1:9881/asr → {"text": ...}
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from backend import config

from backend import config
from backend.services import settings_service

STT_PORT = 9881
TTS_PORT = 9880
STT_HEALTH_TIMEOUT = 1.0
HEALTH_CACHE_SECONDS = 5.0
START_WAIT_SECONDS = 90.0  # STT 要加载模型（实测 ~5s），留足余量
FFMPEG_TIMEOUT = 30.0
ASR_TIMEOUT = 120.0

_LOCK = threading.Lock()
# name -> {"proc": Popen, "started_at": float}；只记**我们自己起的**
_PROCS: dict[str, dict] = {}
# 健康检查的短缓存：前端会轮询（启动中每 1.5 秒刷一次），别每次都去真连
_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}
# 落盘的 PID 记录：**父进程被硬杀（崩溃 / 任务管理器）时，孩子会变孤儿** ——
# 下次启动照这份记录把它们收掉（D96 第 3 条；只认命令行对得上的 python 进程，别误杀）
_PID_FILE = config.USER_DATA_DIR / "speech_services.json"

# 命令行里出现这些片段才算"我们的"
_MARKERS = {
    "tts": ("api_v2.py",),
    "stt": ("stt_server.py",),
}


def _save_pids() -> None:
    try:
        data = {name: record["proc"].pid for name, record in _PROCS.items()}
        _PID_FILE.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, KeyError) as exc:
        logger.warning("写语音服务 PID 记录失败（不影响使用）：{}", exc)


def _pid_command_line(pid: int) -> str:
    """问一次 Windows：这个 PID 的命令行是什么（用来确认"它到底是不是我们起的"）。"""
    try:
        done = subprocess.run(  # noqa: S603
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return done.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cleanup_leftovers() -> list[str]:
    """收掉**上一次没收干净**的服务进程（父进程被硬杀时留下的孤儿）。

    只杀"命令行里带着 `api_v2.py` / `stt_server.py` 的 python"——
    PID 可能被系统回收，别的进程我们绝对不碰。
    """
    if not _PID_FILE.exists():
        return []
    try:
        # utf-8-sig：PowerShell 的 Set-Content -Encoding UTF8 会加 BOM，
        # 直接 utf-8 读会让 json.loads 失败 → 悄悄"当成没记录"（踩过一次）
        data = json.loads(_PID_FILE.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("语音服务 PID 记录读不出来（跳过清理）：{}", exc)
        data = {}
    killed: list[str] = []
    for name, pid in (data or {}).items():
        markers = _MARKERS.get(name, ())
        if not markers or not isinstance(pid, int):
            continue
        line = _pid_command_line(pid)
        if not line or not any(marker in line for marker in markers):
            continue
        logger.info("收掉上次留下的 {} 服务（PID {}）", name, pid)
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        killed.append(name)
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return killed


def _sovits_dir(session: Session) -> str:
    purposes = settings_service.effective_purposes_from(settings_service.get(session))
    conf = purposes.get("tts") or {}
    return str(conf.get("sovits_dir") or "").strip()


def _runtime_python(sovits_dir: str) -> Path:
    return Path(sovits_dir) / "runtime" / "python.exe"


def _health(name: str) -> bool:
    """127.0.0.1 上的本地健康检查（**trust_env=False**：别被系统代理吞掉）。"""
    now = time.monotonic()
    cached = _HEALTH_CACHE.get(name)
    if cached and now - cached[0] < HEALTH_CACHE_SECONDS:
        return cached[1]
    url = (
        f"http://127.0.0.1:{TTS_PORT}/docs"
        if name == "tts"
        else f"http://127.0.0.1:{STT_PORT}/health"
    )
    ok = False
    try:
        with httpx.Client(timeout=STT_HEALTH_TIMEOUT, trust_env=False) as client:
            ok = client.get(url).status_code < 500
    except httpx.HTTPError:
        ok = False
    _HEALTH_CACHE[name] = (now, ok)
    return ok


def reset_health_cache() -> None:
    """启停之后要立刻反映真实状态（别让 5 秒缓存骗了用户）。"""
    _HEALTH_CACHE.clear()


def _alive(name: str) -> bool:
    record = _PROCS.get(name)
    proc = record.get("proc") if record else None
    return bool(proc) and proc.poll() is None


def state(name: str) -> str:
    """`stopped` / `starting` / `running`。"""
    if _health(name):
        return "running"
    if _alive(name):
        record = _PROCS[name]
        if time.monotonic() - record["started_at"] < START_WAIT_SECONDS:
            return "starting"
    return "stopped"


def status_all(session: Session) -> dict:
    sovits_dir = _sovits_dir(session)
    result: dict[str, dict] = {}
    # 两个健康检查**并行**（串行时 TTS 一忙就要等两轮超时，前端会一直显示"读取中"）
    states: dict[str, str] = {}
    threads = []
    for name in ("tts", "stt"):
        thread = threading.Thread(target=lambda n=name: states.__setitem__(n, state(n)))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout=STT_HEALTH_TIMEOUT + 1.0)
    for name, label, port in (
        ("tts", "语音合成（GPT-SoVITS）", TTS_PORT),
        ("stt", "语音识别（Paraformer）", STT_PORT),
    ):
        current = states.get(name) or state(name)
        hint = ""
        if not sovits_dir:
            hint = "还没在「模型 → 语音合成」里填 GPT-SoVITS 的程序目录"
        elif not _runtime_python(sovits_dir).exists():
            hint = f"找不到 runtime\\python.exe（{sovits_dir}）"
        elif current == "stopped":
            hint = "点「启动」就能用（识别要等模型加载几秒）"
        result[name] = {
            "state": current,
            "label": label,
            "port": port,
            "ours": bool(_PROCS.get(name)),
            "hint": hint,
        }
    return result


def _spawn(name: str, sovits_dir: str) -> dict:
    python = _runtime_python(sovits_dir)
    if name == "tts":
        args = [
            str(python),
            "api_v2.py",
            "-a",
            "127.0.0.1",
            "-p",
            str(TTS_PORT),
            "-c",
            "GPT_SoVITS/configs/tts_infer.yaml",
        ]
    else:
        args = [
            str(python),
            # 打包后这个脚本在解包目录里（spec 的 datas 塞进去了）
            str(config.RESOURCE_DIR / "backend" / "speech" / "stt_server.py"),
            "--port",
            str(STT_PORT),
            "--sovits-dir",
            sovits_dir,
        ]
    creation = 0
    if os.name == "nt":  # 别弹出黑窗口（桌面应用里跳个 cmd 很吓人）
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    logger.info("启动 {} 服务：{}", name, " ".join(args[:3]) + " …")
    proc = subprocess.Popen(  # noqa: S603 —— 参数是我们自己拼的，没有用户输入
        args,
        cwd=sovits_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation,
    )
    with _LOCK:
        _PROCS[name] = {"proc": proc, "started_at": time.monotonic()}
        _save_pids()
    return {"ok": True, "state": "starting"}


def start(session: Session, name: str) -> dict:
    """起服务。**已经开着的（含用户自己开的）只认状态，不重复起。**"""
    if name not in ("tts", "stt"):
        return {"ok": False, "hint": "没有这个服务"}
    reset_health_cache()  # 启停要立刻反映真实状态
    if _health(name):
        return {"ok": True, "state": "running", "hint": "它已经开着了"}
    sovits_dir = _sovits_dir(session)
    if not sovits_dir:
        return {"ok": False, "hint": "先在「模型 → 语音合成」里填 GPT-SoVITS 的程序目录"}
    if not _runtime_python(sovits_dir).exists():
        return {"ok": False, "hint": f"找不到 {sovits_dir}\\runtime\\python.exe"}
    if _alive(name):
        return {"ok": True, "state": "starting", "hint": "正在启动中，稍等"}
    try:
        return _spawn(name, sovits_dir)
    except OSError as exc:
        logger.warning("启动 {} 失败：{}", name, exc)
        return {"ok": False, "hint": f"启动不了（{type(exc).__name__}）—— 看日志"}


def stop(name: str) -> dict:
    """停**我们自己起的**那个进程（连子进程一起）。"""
    reset_health_cache()
    record = _PROCS.get(name)
    if not record:
        if _health(name):
            return {"ok": False, "hint": "这个服务不是 yumeFriend 启动的 —— 用它自己的窗口关"}
        return {"ok": True, "state": "stopped"}
    proc = record["proc"]
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(  # noqa: S603
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:  # pragma: no cover —— 项目只跑 Windows
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            logger.warning("{} 服务没在 5 秒内退出（PID {}）", name, proc.pid)
    _PROCS.pop(name, None)
    _save_pids()
    logger.info("已停止 {} 服务（PID {}）", name, proc.pid)
    return {"ok": True, "state": "stopped"}


def stop_all_ours() -> None:
    """应用退出时收尾：**只收我们自己起的**（别去动用户自己开的窗口）。"""
    for name in list(_PROCS):
        try:
            stop(name)
        except Exception as exc:  # noqa: BLE001 —— 收尾失败不该拦住退出
            logger.warning("停止 {} 服务失败：{}", name, exc)


def _to_wav(sovits_dir: str, raw: bytes, filename: str) -> Path | None:
    """把浏览器录的音频转成 16k 单声道 wav（用 GPT-SoVITS 自带的 ffmpeg.exe）。"""
    ffmpeg = Path(sovits_dir) / "ffmpeg.exe"
    if not ffmpeg.exists():
        logger.warning("找不到 ffmpeg：{}", ffmpeg)
        return None
    suffix = Path(filename or "a.webm").suffix or ".webm"
    fd, source = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    target = Path(source).with_suffix(".16k.wav")
    try:
        done = subprocess.run(  # noqa: S603
            [str(ffmpeg), "-y", "-i", source, "-ar", "16000", "-ac", "1", target],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if done.returncode != 0 or not target.exists():
            # ⚠️ 2026-09-27 加：**坏的录音留一份再删** —— 不然只能靠猜。
            # 背景：手机原生录的 m4a 出现过 `Audio: none, 0 channels`（ffmpeg 解不出流），
            # 光看 stderr 判断不了是"录空了"还是"传输坏了"。留档到 user_data/logs/bad-audio/。
            try:
                dump_dir = Path(config.USER_DATA_DIR) / "logs" / "bad-audio"
                dump_dir.mkdir(parents=True, exist_ok=True)
                dump = dump_dir / f"{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
                shutil.copy2(source, dump)
                logger.warning("坏录音已留档：{}（{} 字节）", dump, len(raw))
            except Exception as exc:  # noqa: BLE001 —— 留档失败别影响主流程
                logger.warning("坏录音留档失败：{}", exc)
            logger.warning("ffmpeg 转换失败：{}", done.stderr[-300:])
            return None
        return target
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffmpeg 转换异常：{}", exc)
        return None
    finally:
        try:
            os.unlink(source)
        except OSError:
            pass


def transcribe(session: Session, raw: bytes, *, filename: str = "rec.webm") -> dict:
    """录到的音频 → 文字。**所有失败都回人话**（`{"ok": false, "hint": ...}`）。"""
    if not raw:
        return {"ok": False, "hint": "没录到声音"}
    sovits_dir = _sovits_dir(session)
    if not sovits_dir:
        return {"ok": False, "hint": "先在「模型 → 语音合成」里填 GPT-SoVITS 的程序目录"}

    # 懒启动：第一次用的时候才起（并等它就绪）
    if not _health("stt"):
        started = start(session, "stt")
        if not started.get("ok"):
            return {"ok": False, "hint": started.get("hint") or "语音识别服务起不来"}
        deadline = time.monotonic() + START_WAIT_SECONDS
        while time.monotonic() < deadline:
            if _health("stt"):
                break
            if not _alive("stt") and not _health("stt"):
                return {"ok": False, "hint": "语音识别服务起不来（看日志里的报错）"}
            time.sleep(0.5)
        else:
            return {"ok": False, "hint": "语音识别服务启动超时（第一次要加载模型，再试一次）"}

    wav = _to_wav(sovits_dir, raw, filename)
    if wav is None:
        return {
            "ok": False,
            "hint": "这段录音电脑解不出声音（多半是手机那边没录上）—— 再按一次「按住说话」；"
            "还是不行就看看手机的麦克风权限有没有给 yumeFriend（坏的录音已留档，日志里能看到）。",
        }
    try:
        payload = wav.read_bytes()
        with httpx.Client(timeout=ASR_TIMEOUT, trust_env=False) as client:
            response = client.post(
                f"http://127.0.0.1:{STT_PORT}/asr",
                content=payload,
                headers={"Content-Type": "application/octet-stream"},
            )
        body = response.json()
        if not body.get("ok"):
            return {"ok": False, "hint": f"识别失败：{body.get('error') or response.status_code}"}
        return {"ok": True, "text": (body.get("text") or "").strip()}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("调 STT 服务失败：{}", exc)
        return {"ok": False, "hint": "语音识别服务连接不上（是不是刚被关掉了）"}
    finally:
        try:
            wav.unlink()
        except OSError:
            pass
