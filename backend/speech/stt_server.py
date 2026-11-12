"""STT 助手服务（v2.1 / T2.1-2 / D96）——**必须用 GPT-SoVITS 的 runtime python 跑**：

    D:\\GPT-SoVITS\\runtime\\python.exe <仓库>\\backend\\speech\\stt_server.py --port 9881

为什么单独一个进程：funasr + 中文 Paraformer 模型都在 GPT-SoVITS 的 runtime 里
（`tools/asr/models/speech_paraformer-*`）；我们自己的 venv 里不该塞 torch（几个 GB，
而且 GPT-SoVITS 已经占了 ~6GB/8GB 显存）。这个进程**纯 CPU** 跑：
模型加载一次 ~5 秒，之后每句 0.3~0.6 秒（rtf≈0.06）。

⚠️ 这个文件**只 import 标准库 + funasr**（它跑在**别人的 python** 里，没有我们的依赖），
   所以用 http.server 而不是 FastAPI。

接口（给 `backend/services/speech_service.py` 调）：

    GET  /health   → {"ok": true, "model_loaded": bool}
    POST /asr      body = 音频原始字节（后端已经用 ffmpeg 转成 16k 单声道 wav）
                   → {"ok": true, "text": "..."} ／ {"ok": false, "error": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_MODEL = None
_MODEL_LOCK = threading.Lock()
MAX_BYTES = 20 * 1024 * 1024


def _load_model():
    """第一次用的时候加载（外面会先调一次预热）。"""
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from tools.asr.funasr_asr import create_model  # noqa: PLC0415 —— 必须延迟到 chdir 之后

            _MODEL = create_model("zh")
        return _MODEL


def _transcribe(raw: bytes) -> str:
    model = _load_model()
    fd, path = tempfile.mkstemp(suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        result = model.generate(input=path)
        return (result[0]["text"] if result else "").strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: ANN002 —— 别往 stderr 刷噪音
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 —— http.server 的接口名
        if self.path.startswith("/health"):
            self._json(200, {"ok": True, "model_loaded": _MODEL is not None})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/asr"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BYTES:
            self._json(400, {"ok": False, "error": "音频是空的（或者太大了）"})
            return
        raw = self.rfile.read(length)
        try:
            text = _transcribe(raw)
        except Exception as exc:  # noqa: BLE001 —— 转写失败也要回 JSON
            traceback.print_exc()
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, {"ok": True, "text": text})


def main() -> int:
    parser = argparse.ArgumentParser(description="yumeFriend 的本地语音识别助手")
    parser.add_argument("--port", type=int, default=9881)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--sovits-dir", default=r"D:\GPT-SoVITS")
    args = parser.parse_args()

    # funasr 的模型路径是相对的（tools/asr/models/...）→ 必须站在 GPT-SoVITS 目录里
    os.chdir(args.sovits_dir)
    sys.path.insert(0, args.sovits_dir)

    print(f"[stt] 正在加载模型（{args.sovits_dir}）…", flush=True)
    _load_model()  # 加载完再对外服务 → /health 一通就代表"能立刻转写"
    print(f"[stt] 就绪，监听 {args.host}:{args.port}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
