"""Live2D 模型级设置（D440）。

一份设置文件对应一个 moc3 指纹，存在
`user_data/live2d/model_settings/<model_id>.json`。沉浸模式、桌宠和设置页都从这里
读同一份标签 / 参数 / 表情 / 过渡 / 待机 / 口型规则。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from backend import config
from backend.services.live2d_model_registry import (
    Live2DModelInfo,
    discover_models,
    get_model,
    model_id_for_file,
    model_info_for_file,
)

SETTINGS_VERSION = 2

DEFAULT_IDLE = {
    "enabled": True,
    "head_x": 2.0,
    "head_y": 1.2,
    "head_z": 1.5,
    "body_x": 1.2,
    "body_z": 0.8,
    "gaze_x": 0.14,
    "gaze_y": 0.10,
    "breath": 0.24,
}

DEFAULT_SPEECH = {
    "enabled": True,
    "text_ms_per_char": 160,
    "min_ms": 600,
    "max_ms": 12000,
    "mouth_open": 0.62,
}

_PRESET_CACHE: dict[str, Any] | None = None

_PARAM_RANGES: dict[str, tuple[float, float]] = {
    "ParamEyeLOpen": (0.0, 1.0),
    "ParamEyeROpen": (0.0, 1.0),
    "ParamEyeLSmile": (-1.0, 1.0),
    "ParamEyeRSmile": (-1.0, 1.0),
    "ParamEyeSmile": (-1.0, 1.0),
    "ParamEyeAnger": (-1.0, 1.0),
    "ParamEyePuff": (-1.0, 1.0),
    "ParamMouthForm": (-1.0, 1.0),
    "ParamMouthOpenY": (0.0, 1.0),
    "ParamMouthX": (-1.0, 1.0),
    "ParamCheek": (0.0, 1.0),
    "ParamBrowLY": (-1.0, 1.0),
    "ParamBrowRY": (-1.0, 1.0),
    "ParamBrowLAngle": (-1.0, 1.0),
    "ParamBrowRAngle": (-1.0, 1.0),
    "ParamBrowLForm": (-1.0, 1.0),
    "ParamBrowRForm": (-1.0, 1.0),
    "ParamEyeBallX": (-1.0, 1.0),
    "ParamEyeBallY": (-1.0, 1.0),
    "ParamAngleX": (-30.0, 30.0),
    "ParamAngleY": (-30.0, 30.0),
    "ParamAngleZ": (-30.0, 30.0),
    "ParamBodyAngleX": (-10.0, 10.0),
    "ParamBodyAngleY": (-10.0, 10.0),
    "ParamBodyAngleZ": (-10.0, 10.0),
    "ParamBreath": (0.0, 1.0),
    "ParamHairFront": (-1.0, 1.0),
    "ParamHairSide": (-1.0, 1.0),
    "ParamHairBack": (-1.0, 1.0),
}


def _preset_paths() -> tuple[Path, ...]:
    return (
        config.RESOURCE_DIR / "frontend" / "dist" / "live2d" / "mood_presets.json",
        config.ROOT_DIR / "frontend" / "public" / "live2d" / "mood_presets.json",
    )


def default_preset_data() -> dict[str, Any]:
    global _PRESET_CACHE
    if _PRESET_CACHE is not None:
        return _PRESET_CACHE
    for path in _preset_paths():
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("presets"), dict):
                    _PRESET_CACHE = data
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Live2D 默认 preset 读取失败（{}）：{}", path, exc)
    _PRESET_CACHE = {"version": 1, "base": {}, "aliases": {}, "presets": {}}
    return _PRESET_CACHE


def _settings_path(model_id: str) -> Path:
    return config.LIVE2D_DIR / "model_settings" / f"{model_id}.json"


def _bundled_settings_path(model_id: str) -> Path:
    return config.BUNDLED_LIVE2D_SETTINGS_DIR / f"{model_id}.json"


def _slug(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_\-\u3400-\u9fff]+", "_", value or "").strip("_")
    return safe[:40] or f"label_{uuid.uuid4().hex[:8]}"


def _default_labels(preset_data: dict[str, Any]) -> list[dict[str, Any]]:
    aliases: dict[str, list[str]] = {}
    for alias, name in (preset_data.get("aliases") or {}).items():
        aliases.setdefault(str(name), []).append(str(alias))
    labels: list[dict[str, Any]] = []
    for name, preset in (preset_data.get("presets") or {}).items():
        params = {
            str(key): float(value)
            for key, value in ((preset or {}).get("params") or {}).items()
            if isinstance(value, (int, float))
        }
        labels.append(
            {
                "id": _slug(str(name)),
                "name": str(name),
                "aliases": aliases.get(str(name), []),
                "params": params,
                "expressions": [],
                "transition_ms": None,
            }
        )
    return labels


def default_settings(model: Live2DModelInfo) -> dict[str, Any]:
    preset_data = default_preset_data()
    labels = _default_labels(preset_data)
    _hydrate_label_expressions(
        {"base": dict(preset_data.get("base") or {}), "labels": labels},
        model,
    )
    names = {str(item.get("name")) for item in labels}
    return {
        "version": SETTINGS_VERSION,
        "model_id": model.id,
        "display_name": model.name,
        "default_label": "正常" if "正常" in names else (labels[0]["name"] if labels else ""),
        "transition_ms": 320,
        "base": dict(preset_data.get("base") or {}),
        "labels": labels,
        "idle": deepcopy(DEFAULT_IDLE),
        "speech": deepcopy(DEFAULT_SPEECH),
    }


def _coerce_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def normalize_settings(data: dict[str, Any] | None, model: Live2DModelInfo) -> dict[str, Any]:
    base = default_settings(model)
    if not isinstance(data, dict):
        return base

    result = deepcopy(base)
    result["version"] = SETTINGS_VERSION
    result["model_id"] = model.id
    result["display_name"] = str(data.get("display_name") or base["display_name"]).strip()
    result["transition_ms"] = int(
        max(0, min(5000, _coerce_float(data.get("transition_ms"), 320)))
    )
    if isinstance(data.get("base"), dict):
        result["base"] = {
            str(key): _coerce_float(value, 0.0)
            for key, value in data["base"].items()
            if isinstance(value, (int, float))
        }

    raw_labels = data.get("labels")
    if isinstance(raw_labels, list):
        legacy = int(_coerce_float(data.get("version"), 1)) < 2
        labels: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for item in raw_labels:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            params = {
                str(key): _coerce_float(value, 0.0)
                for key, value in (item.get("params") or {}).items()
                if isinstance(value, (int, float))
            }
            aliases = [
                str(alias).strip()
                for alias in (item.get("aliases") or [])
                if str(alias).strip() and str(alias).strip() != name
            ]
            expressions = [
                str(expr).strip()
                for expr in (item.get("expressions") or [])
                if str(expr).strip()
            ]
            transition = item.get("transition_ms")
            labels.append(
                {
                    "id": str(item.get("id") or _slug(name)),
                    "name": name,
                    "aliases": list(dict.fromkeys(aliases)),
                    "params": params,
                    "expressions": list(dict.fromkeys(expressions)),
                    "transition_ms": (
                        None if transition is None else int(max(0, min(5000, _coerce_float(transition, 0))))
                    ),
                }
            )
        if labels:
            if legacy:
                seeded = {str(item["id"]): item for item in base["labels"]}
                for label in labels:
                    seed = seeded.get(str(label.get("id"))) or next(
                        (
                            item
                            for item in base["labels"]
                            if str(item.get("name")) == str(label.get("name"))
                        ),
                        None,
                    )
                    if seed is None:
                        continue
                    label["params"] = {
                        str(key): value
                        for key, value in (label.get("params") or {}).items()
                        if not str(key).startswith("PARAM_EXP")
                    }
                    label["expressions"] = list(seed.get("expressions") or [])
            result["labels"] = labels

    if isinstance(data.get("idle"), dict):
        idle = result["idle"]
        for key, fallback in DEFAULT_IDLE.items():
            value = data["idle"].get(key, fallback)
            idle[key] = bool(value) if key == "enabled" else _coerce_float(value, float(fallback))
    if isinstance(data.get("speech"), dict):
        speech = result["speech"]
        for key, fallback in DEFAULT_SPEECH.items():
            value = data["speech"].get(key, fallback)
            speech[key] = bool(value) if key == "enabled" else _coerce_float(value, float(fallback))
        speech["min_ms"] = int(max(50, speech["min_ms"]))
        speech["max_ms"] = int(max(speech["min_ms"], speech["max_ms"]))
        speech["text_ms_per_char"] = int(max(20, speech["text_ms_per_char"]))
        speech["mouth_open"] = max(0.0, min(1.0, speech["mouth_open"]))

    names = {str(item["name"]) for item in result["labels"]}
    if not names:
        result["labels"] = _default_labels(default_preset_data())
        names = {str(item["name"]) for item in result["labels"]}
    default_label = str(data.get("default_label") or result["default_label"]).strip()
    if default_label not in names:
        default_label = "正常" if "正常" in names else next(iter(names))
    result["default_label"] = default_label
    _hydrate_label_expressions(result, model)
    return result


def _expression_matches_params(
    definition: dict[str, Any],
    params: dict[str, float],
    base: dict[str, float],
) -> bool:
    operations = [item for item in (definition.get("params") or []) if item.get("id")]
    if not operations:
        return False
    for op in operations:
        param = str(op.get("id"))
        value = _coerce_float(op.get("value"), 0.0)
        blend = str(op.get("blend") or "Add").lower()
        current = params.get(param)
        if current is None:
            return False
        base_value = _coerce_float(base.get(param), 0.0)
        if blend == "multiply":
            expected = base_value * value
        elif blend == "overwrite":
            expected = value
        else:
            expected = base_value + value
        if abs(current - expected) > 1e-4:
            return False
    return True


def _hydrate_label_expressions(settings: dict[str, Any], model: Live2DModelInfo) -> None:
    """把预设里已经是 exp3 效果的 `PARAM_EXP*` 还原成对应复选框。

    旧 preset 用 `PARAM_EXP4=1` 这种写法直接写参数；设置页那边看起来就是
    “没勾选”。这里按模型实际 exp3 的参数效果做匹配：
    · 匹配上 → 勾选该 exp3，并从 params 里移除对应参数，避免运行时叠加两次；
    · 已勾选的 exp3 → 同样移除 params 里的重复参数。
    """
    definitions = expression_defs_for_model(model)
    if not definitions:
        return
    base = settings.get("base") or {}
    for label in settings.get("labels") or []:
        params = {
            str(key): _coerce_float(value, 0.0)
            for key, value in (label.get("params") or {}).items()
        }
        selected = {str(item) for item in (label.get("expressions") or [])}
        for definition in definitions:
            expr_id = str(definition.get("id") or "")
            if not expr_id:
                continue
            if expr_id in selected or _expression_matches_params(definition, params, base):
                selected.add(expr_id)
                for op in definition.get("params") or []:
                    params.pop(str(op.get("id") or ""), None)
        label["params"] = params
        label["expressions"] = sorted(selected)


def has_custom_settings(model_id: str) -> bool:
    return _settings_path(model_id).is_file()


def settings_mtime(model_id: str) -> int:
    path = _settings_path(model_id)
    if not path.is_file():
        path = _bundled_settings_path(model_id)
    try:
        return path.stat().st_mtime_ns if path.is_file() else 0
    except OSError:
        return 0


def load_settings(model: Live2DModelInfo) -> dict[str, Any]:
    for path in (_settings_path(model.id), _bundled_settings_path(model.id)):
        if path.is_file():
            try:
                return normalize_settings(
                    json.loads(path.read_text(encoding="utf-8")),
                    model,
                )
            except Exception as exc:  # noqa: BLE001 —— 坏设置回默认，不影响聊天
                logger.warning("Live2D 模型设置读取失败（{}）：{}", path, exc)
    return normalize_settings(None, model)


def save_settings(model: Live2DModelInfo, data: dict[str, Any]) -> dict[str, Any]:
    settings = normalize_settings(data, model)
    path = _settings_path(model.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, path)
    return settings


def resolve_model_for_friend(
    friend_id: str | None,
    model_id: str | None = None,
) -> Live2DModelInfo | None:
    from backend.desktop.live2d_renderer import resolve_model_file  # 懒导入避免循环

    model_file = resolve_model_file(friend_id, model_id)
    if model_file is None:
        return None
    return model_info_for_file(model_file)


def settings_for_friend(
    friend_id: str | None,
    model_id: str | None = None,
) -> tuple[Live2DModelInfo | None, dict[str, Any] | None]:
    model = resolve_model_for_friend(friend_id, model_id)
    if model is None:
        return None, None
    return model, load_settings(model)


def _strip_expr_suffix(name: str) -> str:
    return re.sub(r"\.exp3(?:\.json)?$", "", name, flags=re.IGNORECASE)


def expression_defs_for_model(model: Live2DModelInfo) -> list[dict[str, Any]]:
    """列出模型目录内全部 exp3，并解析成前端/渲染器都能用的参数操作。"""
    result: list[dict[str, Any]] = []
    root = model.model_file.parent
    for path in sorted(root.rglob("*.exp3.json")):
        expr_id = _strip_expr_suffix(path.name)
        if expr_id.lower() == "no_watermark":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            continue
        operations = []
        for item in data.get("Parameters") or []:
            param = str(item.get("Id") or "").strip()
            if not param:
                continue
            operations.append(
                {
                    "id": param,
                    "value": _coerce_float(item.get("Value"), 0.0),
                    "blend": str(item.get("Blend") or "Add"),
                }
            )
        result.append(
            {
                "id": expr_id,
                "name": expr_id,
                "file": path.relative_to(root).as_posix(),
                "params": operations,
            }
        )
    return result


def _param_range(param_id: str) -> tuple[float, float]:
    if param_id in _PARAM_RANGES:
        return _PARAM_RANGES[param_id]
    if param_id.startswith("ParamAngle"):
        return (-30.0, 30.0)
    if param_id.startswith("ParamBodyAngle"):
        return (-10.0, 10.0)
    if param_id.startswith("Paramcol_"):
        return (0.0, 1.0)
    return (-1.0, 1.0)


def model_parameter_metadata(model: Live2DModelInfo) -> list[dict[str, Any]]:
    """从 cdi3.json 读参数名/分组，不打开 OpenGL（D440 闪退修复）。

    min/max/default 不在 cdi3 里，用标准 Cubism 范围 + 模型设置 base 兜底；
    自定义参数按 `-1..1` 处理。这样设置页不需要再开第二个 GL 上下文。
    """
    root = model.model_file.parent
    cdi_path: Path | None = None
    try:
        model_json = json.loads(model.model_file.read_text(encoding="utf-8-sig"))
        display_info = str((model_json.get("FileReferences") or {}).get("DisplayInfo") or "")
        if display_info:
            candidate = (root / display_info).resolve()
            if candidate.is_file():
                cdi_path = candidate
    except Exception:  # noqa: BLE001
        cdi_path = None
    if cdi_path is None:
        for candidate in sorted(root.rglob("*.cdi3.json")):
            cdi_path = candidate
            break
    if cdi_path is None:
        return []
    try:
        cdi = json.loads(cdi_path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return []
    groups = {
        str(item.get("Id")): str(item.get("Name") or item.get("Id") or "")
        for item in (cdi.get("ParameterGroups") or [])
        if isinstance(item, dict)
    }
    settings = load_settings(model)
    base = settings.get("base") or {}
    result: list[dict[str, Any]] = []
    for item in cdi.get("Parameters") or []:
        if not isinstance(item, dict):
            continue
        param_id = str(item.get("Id") or "").strip()
        if not param_id:
            continue
        low, high = _param_range(param_id)
        result.append(
            {
                "id": param_id,
                "name": str(item.get("Name") or param_id),
                "group": groups.get(str(item.get("GroupId") or ""), ""),
                "min": low,
                "max": high,
                "default": _coerce_float(base.get(param_id), 0.0),
            }
        )
    return result


def apply_expressions(
    params: dict[str, float],
    expression_ids: Iterable[str],
    expression_defs: Iterable[dict[str, Any]],
) -> dict[str, float]:
    result = {str(key): float(value) for key, value in params.items()}
    wanted = {str(item) for item in expression_ids}
    for definition in expression_defs:
        if str(definition.get("id")) not in wanted:
            continue
        for op in definition.get("params") or []:
            param = str(op.get("id") or "")
            if not param:
                continue
            value = _coerce_float(op.get("value"), 0.0)
            blend = str(op.get("blend") or "Add").lower()
            if blend == "multiply":
                result[param] = result.get(param, 1.0) * value
            elif blend == "overwrite":
                result[param] = value
            else:
                result[param] = result.get(param, 0.0) + value
    return result


def label_for_mood(settings: dict[str, Any], mood: str) -> dict[str, Any]:
    """精确名 → alias → 默认标签。"""
    text = (mood or "").strip()
    labels = settings.get("labels") or []
    for label in labels:
        if str(label.get("name")) == text:
            return label
    for label in labels:
        if text and text in {str(item) for item in (label.get("aliases") or [])}:
            return label
    default_label = str(settings.get("default_label") or "正常")
    for label in labels:
        if str(label.get("name")) == default_label:
            return label
    return labels[0] if labels else {
        "id": "normal",
        "name": "正常",
        "aliases": [],
        "params": {},
        "expressions": [],
        "transition_ms": None,
    }


def target_params_for_label(
    settings: dict[str, Any],
    mood: str,
    expression_defs: Iterable[dict[str, Any]] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    label = label_for_mood(settings, mood)
    params = {
        str(key): _coerce_float(value, 0.0)
        for key, value in (settings.get("base") or {}).items()
        if isinstance(value, (int, float))
    }
    params.update(
        {
            str(key): _coerce_float(value, 0.0)
            for key, value in (label.get("params") or {}).items()
            if isinstance(value, (int, float))
        }
    )
    if expression_defs is not None:
        params = apply_expressions(params, label.get("expressions") or [], expression_defs)
    return params, label


def labels_for_friend(friend_id: str | None, model_id: str | None = None) -> list[str]:
    _model, settings = settings_for_friend(friend_id, model_id)
    if not settings:
        return []
    labels: list[str] = []
    for item in settings.get("labels") or []:
        name = str(item.get("name") or "").strip()
        if name:
            labels.append(name)
    return labels


def emotion_labels_for_friend(
    friend_id: str | None,
    portrait_emotions: Iterable[str] = (),
    model_id: str | None = None,
    visual_mode: str = "live2d",
) -> list[str]:
    """提示词可用情绪：按朋友当前呈现方式只给对应那套标签。"""
    result: list[str] = []
    live2d_labels = labels_for_friend(friend_id, model_id) if visual_mode != "portrait" else []
    source = portrait_emotions if visual_mode == "portrait" else (live2d_labels or portrait_emotions)
    for item in source:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def all_models() -> list[Live2DModelInfo]:
    return discover_models()


def model_from_id(model_id: str) -> Live2DModelInfo | None:
    return get_model(model_id)


def model_id_for_model_file(model_file: Path) -> str | None:
    return model_id_for_file(model_file)
