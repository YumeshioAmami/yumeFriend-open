"""文件工具的服务层（v2.1 / T2.1-4 / D99）。
口径（用户定的）：**读随便读，写要用户点头**；点头那一步在 `approval_service` 里做。

硬护栏（不替代授权，是"有些事根本不做"）：
· **删目录只删空的，永不递归** —— 要删一堆东西请用户自己动手
· 路径必须是**绝对路径**（或 `~` 开头）：相对路径不猜
· 读/写都有上限，超了截断并说明

⚠️ 读出来的内容会随上下文发给云端模型 —— 这一点在提示词里跟她说清楚了（D99 第 1 条）。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from backend.text_encoding import decode_text, looks_like_text

MAX_READ_CHARS = 4000
MAX_WRITE_CHARS = 200_000
MAX_LIST_ENTRIES = 200

# 危险的写入目标：不拦（用户点头为准），但卡片上要额外警示一句
DANGEROUS_MARKERS = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
)


def resolve_path(raw: str) -> Path:
    """把模型给的字符串变成绝对路径（**不猜相对路径**）。"""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("要一个路径（绝对路径，比如 E:\\Notes\\a.txt）")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("请给绝对路径（E:\\... 或 ~/...）—— 相对路径我不知道是哪儿")
    return path


def danger_note(path: Path) -> str | None:
    """危险路径的额外警示（只是提示，不拦）。"""
    lowered = str(path).lower().replace("/", "\\")
    if any(marker in lowered for marker in DANGEROUS_MARKERS):
        return "⚠️ 这是系统目录，改坏可能影响整个 Windows"
    for part in ("\\projects\\yumefriend", "\\yumefriend\\.git"):
        if part in lowered:
            return "⚠️ 这是 yumeFriend 自己的程序/仓库目录，改坏了她自己可能就打不开了"
    if path.parent == path:  # 盘根
        return "⚠️ 这是盘符根目录"
    return None


def _is_probably_text(raw: bytes) -> bool:
    """像文本吗。⚠️ 别只看 NUL —— **UTF-16 的文字里全是 NUL**（高字节），
    旧写法会把"记事本存的 Unicode txt"当成二进制拒读（2026-09-29 与知识库同一个坑的另一半）。"""
    return looks_like_text(raw)


def _decode(raw: bytes) -> str:
    return decode_text(raw)


def _stat(path: Path) -> dict:
    try:
        info = path.stat()
    except OSError:
        return {}
    return {
        "size": info.st_size,
        "modified": datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds"),
    }


def list_dir(raw_path: str) -> dict:
    path = resolve_path(raw_path)
    if not path.exists():
        raise ValueError(f"没有这个目录：{path}")
    if not path.is_dir():
        raise ValueError(f"这是个文件，不是目录：{path}")
    entries = []
    try:
        children = sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
    except OSError as exc:
        raise ValueError(f"这个目录打不开（{exc.strerror or exc}）") from exc
    for child in children[:MAX_LIST_ENTRIES]:
        entries.append(
            {
                "name": child.name,
                "kind": "dir" if child.is_dir() else "file",
                **_stat(child),
            }
        )
    return {
        "path": str(path),
        "entries": entries,
        "truncated": len(children) > MAX_LIST_ENTRIES,
        "note": "（只列了前 200 项）" if len(children) > MAX_LIST_ENTRIES else "",
    }


def read_file(raw_path: str, max_chars: int = MAX_READ_CHARS) -> dict:
    path = resolve_path(raw_path)
    if not path.exists():
        raise ValueError(f"没有这个文件：{path}")
    if path.is_dir():
        raise ValueError(f"这是个目录：{path} —— 想看看里面有什么就用 list_dir")
    try:
        size = path.stat().st_size
        if size > max_chars * 8:  # 先按大小拦一下，别把几百 MB 读进内存
            with path.open("rb") as handle:
                raw = handle.read(max_chars * 4)
            truncated = True
        else:
            raw = path.read_bytes()
            truncated = len(raw) > max_chars * 4
    except OSError as exc:
        raise ValueError(f"这个文件读不了（{exc.strerror or exc}）") from exc
    if not _is_probably_text(raw):
        raise ValueError("这看起来是二进制文件（读出来是乱码）—— 我先不读了，免得白花 token")
    text = _decode(raw)
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return {
        "path": str(path),
        "chars": len(text),
        "size": size,
        "truncated": truncated,
        "text": text,
        "note": "（只读了前面一部分）" if truncated else "",
    }


def write_file(raw_path: str, content: str) -> dict:
    path = resolve_path(raw_path)
    body = content or ""
    if len(body) > MAX_WRITE_CHARS:
        raise ValueError(f"一次最多写 {MAX_WRITE_CHARS} 字（这次 {len(body)}）—— 分几次写")
    existed = path.exists()
    if path.is_dir():
        raise ValueError(f"这是个目录，不能当文件写：{path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"写不进去（{exc.strerror or exc}）") from exc
    return {
        "path": str(path),
        "chars": len(body),
        "created": not existed,
        "note": "（新建）" if not existed else "（覆盖了原来的内容）",
    }


def move_path(raw_src: str, raw_dst: str) -> dict:
    source, target = resolve_path(raw_src), resolve_path(raw_dst)
    if not source.exists():
        raise ValueError(f"源不存在：{source}")
    if target.exists():
        raise ValueError(f"目标已经存在了：{target} —— 先删掉它或者换个名字")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError as exc:
        raise ValueError(f"移动失败（{exc.strerror or exc}）") from exc
    return {"from": str(source), "to": str(target), "ok": True}


def delete_path(raw_path: str) -> dict:
    path = resolve_path(raw_path)
    if not path.exists():
        raise ValueError(f"这个东西不存在了：{path}")
    if path.is_dir():
        try:
            count = len(list(path.iterdir()))
        except OSError as exc:
            raise ValueError(f"这个目录打不开（{exc.strerror or exc}）") from exc
        if count:
            raise ValueError(
                f"这个目录里还有 {count} 项 —— 我不会递归删。要清就你自己动手，或者把里面的东西一个一个删"
            )
        try:
            path.rmdir()
        except OSError as exc:
            raise ValueError(f"删不掉（{exc.strerror or exc}）") from exc
        return {"path": str(path), "kind": "dir", "deleted": True}
    try:
        size = path.stat().st_size
        path.unlink()
    except OSError as exc:
        raise ValueError(f"删不掉（{exc.strerror or exc}）") from exc
    return {"path": str(path), "kind": "file", "size": size, "deleted": True}


def summarize_write(raw_path: str, content: str) -> str:
    """给审批卡片看的内容预览（前 200 字）。"""
    body = (content or "").replace("\r\n", "\n")
    preview = body[:200] + ("…" if len(body) > 200 else "")
    return f"共 {len(body)} 字：\n{preview}"
