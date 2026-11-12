"""音色库（v2 / T2.0-4）：增删改查 + **从 GPT-SoVITS 目录扫描导入**。

扫描只认"标准结构"（我照用户那台机器实测过）：

```
models/v4/派蒙/v4/派蒙_ZH/派蒙_ZH-e10.ckpt          ← GPT 权重
                          /派蒙_ZH_e10_s1770_l32.pth ← SoVITS 权重（同目录）
                          /reference_audios/中文/emotions/【默认】<台词>.wav
models/v4/洛茜/洛茜-e10.ckpt + 洛茜_e16_s544_l32.pth + 数据集_洛茜/<台词>.wav
```

**参考音频的文件名就是台词**（`【默认】` 前缀去掉）——这是用户数据集的组织方式，
也正是 D77 实测"prompt_text 不能为空"那个约束的答案。

认不出来**不做猜测**：返回空，让用户手动填（"自动 + 兜底"，不是"必须自动"）。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Friend, Voice
from backend.services.actor import Actor
from backend.services.errors import NotFoundError, ServiceError

# 这些明显不是"台词"的文件名，跳过（用户机器上就有 ref_luoxi.wav 这种）
_NOT_TRANSCRIPT = re.compile(r"^(ref|test|output|out|sample)[_-]", re.IGNORECASE)
_EPOCH = re.compile(r"[_-]e(\d+)[_-]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch_of(path: Path) -> int:
    """从文件名里抠 epoch（用来在多个 .pth 里挑训练最久的那个）。抠不到就按修改时间。"""
    match = _EPOCH.search(path.stem)
    if match:
        return int(match.group(1))
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def _find_reference(start: Path, search_root: Path) -> Path | None:
    """找参考音频：先看 reference_audios，再看 数据集*，最后往上找两层。

    **顺手挑掉"文件名不是台词"的那些**（`ref_xxx.wav` 排序时可能排在最前面，
    要是直接取第一个，就会因为它没有台词而整个音色被跳过 —— 踩过一次）。
    """
    patterns = ("reference_audios/**/*.wav", "数据集*/**/*.wav", "数据集*/*.wav")
    for directory in (start, start.parent, start.parent.parent):
        for pattern in patterns:
            for hit in sorted(directory.glob(pattern)):
                if _prompt_from(hit):
                    return hit
    return None


def _prompt_from(path: Path) -> str:
    """参考音频的文件名 → 台词（去掉 `【默认】` 这类前缀）。"""
    stem = path.stem.replace("【默认】", "").strip()
    if len(stem) < 2 or _NOT_TRANSCRIPT.match(stem):
        return ""
    return stem


def _voice_name(search_root: Path, ckpt: Path) -> str:
    """从路径推显示名：`models/v4/派蒙/v4/派蒙_ZH/x.ckpt` → `派蒙`。"""
    try:
        parts = ckpt.relative_to(search_root).parts
    except ValueError:
        return ckpt.parent.name
    for part in parts:
        if not re.fullmatch(r"v\d+(\.\d+)?", part, re.IGNORECASE):
            return part
    return ckpt.parent.name


def scan(sovits_dir: str, *, known_gpt: set[str] | None = None) -> list[dict]:
    """扫目录，返回候选音色（**不落库**）。认不出就返回空列表。"""
    root = Path(sovits_dir).expanduser()
    if not sovits_dir or not root.is_dir():
        raise ServiceError("TTS_DIR_NOT_FOUND", f"找不到这个目录：{sovits_dir or '（没填）'}")
    models = root / "models"
    search_root = models if models.is_dir() else root
    known = known_gpt or set()

    found: list[dict] = []
    for ckpt in sorted(search_root.rglob("*.ckpt")):
        siblings = sorted(ckpt.parent.glob("*.pth"))
        if not siblings:
            continue
        sovits = max(siblings, key=_epoch_of)
        reference = _find_reference(ckpt.parent, search_root)
        if reference is None:
            continue
        prompt = _prompt_from(reference)
        if not prompt:
            continue  # 没有台词就用不了（D77：prompt_text 不能空）
        found.append(
            {
                "name": _voice_name(search_root, ckpt),
                "gpt_weights_path": str(ckpt),
                "sovits_weights_path": str(sovits),
                "ref_audio_path": str(reference),
                "prompt_text": prompt,
                "speed": 1.0,
                "already_imported": str(ckpt) in known,
            }
        )
    return found


def list_voices(session: Session) -> list[Voice]:
    return list(session.execute(select(Voice).order_by(Voice.created_at)).scalars())


def _require(session: Session, voice_id: str) -> Voice:
    voice = session.get(Voice, voice_id)
    if voice is None:
        raise NotFoundError("VOICE_NOT_FOUND", "没有这个音色")
    return voice


def get(session: Session, voice_id: str) -> Voice:
    """取一个音色（找不到抛 404）。"""
    return _require(session, voice_id)


def _check_paths(gpt: str, sovits: str, ref: str) -> None:
    """三个文件都得在——不然存进去也是个用不了的音色。"""
    missing = [p for p in (gpt, sovits, ref) if not p or not Path(p).is_file()]
    if missing:
        raise ServiceError("VOICE_FILE_MISSING", f"这些文件找不到：{'、'.join(missing[:3])}")


def create(
    session: Session,
    actor: Actor,
    *,
    name: str,
    gpt_weights_path: str,
    sovits_weights_path: str,
    ref_audio_path: str,
    prompt_text: str,
    speed: float = 1.0,
) -> Voice:
    if not (name or "").strip():
        raise ServiceError("VOICE_NAME_EMPTY", "音色得有个名字")
    if not (prompt_text or "").strip():
        raise ServiceError("VOICE_PROMPT_EMPTY", "参考音频的台词不能空（合成就靠它）")
    _check_paths(gpt_weights_path, sovits_weights_path, ref_audio_path)
    voice = Voice(
        id=str(uuid.uuid4()),
        name=name.strip(),
        gpt_weights_path=gpt_weights_path,
        sovits_weights_path=sovits_weights_path,
        ref_audio_path=ref_audio_path,
        prompt_text=prompt_text.strip(),
        speed=speed,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(voice)
    session.flush()
    return voice


def update(
    session: Session,
    actor: Actor,
    voice_id: str,
    *,
    name: str | None = None,
    speed: float | None = None,
    prompt_text: str | None = None,
) -> Voice:
    voice = _require(session, voice_id)
    if name is not None and name.strip():
        voice.name = name.strip()
    if speed is not None:
        voice.speed = float(speed)
    if prompt_text is not None and prompt_text.strip():
        voice.prompt_text = prompt_text.strip()
    voice.updated_at = _now()
    session.flush()
    return voice


def delete(session: Session, actor: Actor, voice_id: str) -> None:
    """删音色：**引用了它的朋友不会被连坐**，只是 voice_id 置空（FK ON DELETE SET NULL）。"""
    voice = _require(session, voice_id)
    session.delete(voice)
    session.flush()


def to_dict(voice: Voice) -> dict:
    """给前端。`files_ok` = 三个文件现在还都在吗（不在就标"路径失效"，别让用户蒙）。"""
    files_ok = all(
        Path(p).is_file()
        for p in (voice.gpt_weights_path, voice.sovits_weights_path, voice.ref_audio_path)
    )
    return {
        "id": voice.id,
        "name": voice.name,
        "gpt_weights_path": voice.gpt_weights_path,
        "sovits_weights_path": voice.sovits_weights_path,
        "ref_audio_path": voice.ref_audio_path,
        "prompt_text": voice.prompt_text,
        "speed": voice.speed,
        "files_ok": files_ok,
        "used_by": 0,  # 由 API 层填（要查 friends）
        "created_at": voice.created_at,
        "updated_at": voice.updated_at,
    }


def usage_counts(session: Session) -> dict[str, int]:
    """每个音色被几个朋友用着（前端删除前提示用）。"""
    rows = session.execute(
        select(Friend.voice_id).where(Friend.voice_id.is_not(None))
    ).scalars()
    counts: dict[str, int] = {}
    for voice_id in rows:
        counts[voice_id] = counts.get(voice_id, 0) + 1
    return counts
