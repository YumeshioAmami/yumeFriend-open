"""桌宠 Live2D 渲染（D424 / D425）。

默认模型是仓库自带、Apache-2.0 的 YUI；每个朋友可以在
`user_data/live2d/friends/<friend_id>/` 放一套覆盖模型。这里只负责：

* 找模型、补一份运行时 `model3.json`（把 Exp3 / motion3 挂上去）；
* 用 GLFW 的隐藏窗口开一个 OpenGL 上下文；
* 按情绪切表情、逐帧渲染成带透明通道的 PIL 图。

live2d-py / glfw 是可选依赖：缺了、模型坏了、显卡不给上下文，都返回 None，
调用方继续用原来的静态立绘兜底，不让桌宠整个挂掉。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from PIL import Image

from backend import config
from backend.services import live2d_settings_service

# PyOpenGL 在 Windows 上要走 WGL；必须在 import OpenGL 之前设。
if os.name == "nt":
    os.environ.setdefault("PYOPENGL_PLATFORM", "wgl")

RUNTIME_JSON = "_yumefriend.model3.json"
# 2026-11-07（用户："分辨率好低"）：按显示区域动态选渲染高度，
# 不再永远用 420×880 拉伸到整个桌宠/沉浸画面。
# 桌宠默认窗口较小，但用 800px 高度做超采样再下采样，发丝/眼睛边缘会明显
# 干净很多；配合稳定裁剪，默认窗口实测约 100fps。
RENDER_WIDTH = 800
MAX_RENDER_WIDTH = 1600
MAX_RENDER_HEIGHT = 2400
# 原生桌宠每帧还要经过 GL 读回 + GDI/DWM。1200 在去掉“每帧重启 idle”和
# 每帧重新裁 alpha 外接框之后，仍能维持在 120fps 附近；比 1000 更清晰。
PET_RENDER_HEIGHT = 1200
PET_RENDER_SUPERSAMPLE = 1.15
# alpha 外接框会随呼吸/头发/口型变化；如果每帧按它裁剪再缩放，角色会一帧一帧
# 轻微放大缩小。现在第一帧量完就冻结裁剪框（见 `_stable_crop_box`）。

_MOOD_PRESET_CACHE: dict[str, Any] | None = None


def load_mood_presets() -> dict[str, Any] | None:
    """加载前后端共用的情绪预设表；源码/打包都从只读资源目录找。"""
    global _MOOD_PRESET_CACHE
    if _MOOD_PRESET_CACHE is not None:
        return _MOOD_PRESET_CACHE
    candidates = (
        config.RESOURCE_DIR / "frontend" / "dist" / "live2d" / "mood_presets.json",
        config.ROOT_DIR / "frontend" / "public" / "live2d" / "mood_presets.json",
    )
    for path in candidates:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("presets"), dict):
                    _MOOD_PRESET_CACHE = data
                    logger.info("Live2D 情绪预设已加载：{}（{} 个）", path, len(data["presets"]))
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Live2D 情绪预设读取失败（{}）：{}", path, exc)
    logger.warning("Live2D 情绪预设表不存在，回退旧参数表")
    return None


def resolve_mood_preset(
    data: dict[str, Any], mood: str
) -> tuple[dict[str, float], dict[str, Any] | None]:
    aliases = data.get("aliases") or {}
    key = str(aliases.get(mood) or mood)
    presets = data.get("presets") or {}
    preset = presets.get(key) or presets.get("正常") or presets.get("平静")
    base = data.get("base") or {}
    return (
        {str(k): float(v) for k, v in base.items() if isinstance(v, (int, float))},
        preset if isinstance(preset, dict) else None,
    )

# 模型里的中文文件名 → ASCII 表情 id。Cubism 的 JSON 解析对中文 Name 不稳，
# 所以运行时统一用 ASCII 名字，文件路径仍保留中文。
EXPRESSION_ALIASES: dict[str, str] = {
    "＞＜": "happy",
    "脸红": "shy",
    "爱心眼": "love",
    "流泪": "sad",
    "流冷汗": "nervous",
    "屑屑眼": "smug",
    "圈圈眼": "dizzy",
    "黑框眼镜": "glasses",
    "单片眼镜": "monocle",
    "黑脸": "dark_face",
    "加载": "loading",
    "前倾": "lean",
    "兔牙": "fang",
    "尾巴开关": "tail",
    "胸口衣服": "chest_clothes",
    "裙子切换": "skirt",
    "脱鞋": "shoes_off",
}

# 情绪标签 → 统一情绪种类 + 表情候选。顺序有意义：更具体的词先匹配。
# 这里是唯一来源：程序化参数层和 exp3 候选都从同一张表来，避免两套逻辑
# 各自漏词（之前“担心/无奈/认真”会全部掉进 calm，看起来就没有差分）。
MOOD_KIND_RULES: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (("害羞", "羞耻", "脸红", "不好意思"), "shy", ("shy", "blush", "脸红", "mouth_worry")),
    (("心动", "喜欢", "爱慕", "宠溺", "色气", "发情"), "love", ("love", "blush", "drool", "eyes_guillemet")),
    (("慌张", "害怕", "惊恐"), "afraid", ("nervous", "eyes_oo", "mouth_o", "流冷汗")),
    (("紧张", "不安", "心虚", "焦虑"), "nervous", ("nervous", "流冷汗", "mouth_worry", "face_dark")),
    (("担心", "忧心", "挂念"), "worried", ("sad", "mouth_worry", "face_dark")),
    (("伤心", "难过", "委屈", "失落", "哭"), "sad", ("sad", "流泪", "mouth_worry", "face_dark")),
    (("疑惑", "困惑", "懵"), "confused", ("confused", "疑惑", "eyes_oo", "mouth_o")),
    (("思索", "思考", "沉思", "琢磨", "想事情"), "thinking", ("thinking", "confused", "eyes_oo", "mouth_o")),
    (("晕", "圈圈", "混乱"), "dizzy", ("dizzy", "圈圈眼", "eyes_xx")),
    (("得意", "自信", "骄傲", "调皮", "坏笑", "狡黠"), "smug", ("smug", "得意", "eyes_guillemet", "mouth_switch", "teeth")),
    (("惊讶", "吃惊", "震惊"), "surprised", ("surprised", "惊讶", "eyes_oo", "mouth_o")),
    (("生气", "愤怒", "厌恶", "不爽"), "angry", ("angry", "生气", "face_dark", "eyes_guillemet")),
    (("兴奋", "激动", "惊喜"), "excited", ("happy", "smile", "mouth_3", "teeth", "eyes_guillemet")),
    (("困", "睡", "疲惫", "累了"), "sleepy", ("sleepy", "sad", "mouth_worry")),
    (("嘟嘴", "不满", "别扭"), "pout", ("pout", "smug", "mouth_worry")),
    (("认真", "严肃", "专注"), "serious", ("serious", "face_dark", "mouth_worry")),
    (("无奈", "无语", "没办法"), "helpless", ("sad", "mouth_worry", "face_dark")),
    (("好奇", "感兴趣"), "curious", ("confused", "eyes_oo", "mouth_o")),
    (("开心", "愉快", "高兴", "笑", "轻笑"), "happy", ("happy", "smile", "mouth_3", "teeth", "eyes_guillemet")),
)


def expression_candidates_for_mood(mood: str) -> list[str]:
    """返回情绪对应的表情候选（按优先级）。"""
    text = (mood or "").strip()
    if not text:
        return []
    for words, _kind, names in MOOD_KIND_RULES:
        if any(word in text for word in words):
            return list(names)
    return []


def expression_for_mood(mood: str) -> str | None:
    """把中文情绪标签落到第一个候选表情 id 上（保持旧接口）。"""
    candidates = expression_candidates_for_mood(mood)
    return candidates[0] if candidates else None


def mood_expression_kind(mood: str) -> str:
    """情绪 → 程序化参数组（不依赖模型有没有 exp3）。"""
    text = (mood or "").strip()
    for words, kind, _names in MOOD_KIND_RULES:
        if any(word in text for word in words):
            return kind
    return "calm"


def _alias_for_expression(stem: str) -> str:
    # Path.stem 对 `脸红.exp3.json` 得到 `脸红.exp3`，先把扩展名前缀剥掉。
    stem = re.sub(r"\.exp3$", "", stem, flags=re.IGNORECASE)
    if stem in EXPRESSION_ALIASES:
        return EXPRESSION_ALIASES[stem]
    safe = re.sub(r"[^0-9A-Za-z_\-]+", "_", stem).strip("_")
    return safe[:40] or "extra"


def find_model_file(root: Path | None = None) -> Path | None:
    """找指定目录下第一个可用的 model3.json。

    优先同目录的 `default/` 子目录；跳过生成的 `_yumefriend.model3.json`。
    支持任意子目录，之后加别的 Live2D 模型不用改代码。
    """
    base = Path(root or config.LIVE2D_DIR)
    if not base.is_dir():
        return None
    preferred = base / "default"
    roots = [preferred, base] if preferred.is_dir() else [base]
    seen: set[Path] = set()
    for folder in roots:
        for path in sorted(folder.rglob("*.model3.json")):
            if path.name == RUNTIME_JSON or path in seen:
                continue
            seen.add(path)
            return path
    return None


def resolve_model_file(
    friend_id: str | None = None,
    model_id: str | None = None,
) -> Path | None:
    """按朋友找模型：模型 ID > 朋友目录覆盖 > 内置默认 > 用户数据目录。"""
    if model_id:
        model = live2d_settings_service.model_from_id(str(model_id))
        if model is not None and model.model_file.is_file():
            return model.model_file
    if friend_id:
        own = find_model_file(config.LIVE2D_FRIEND_DIR / friend_id)
        if own is not None:
            return own
    default = find_model_file(config.DEFAULT_LIVE2D_DIR)
    if default is not None:
        return default
    return find_model_file(config.LIVE2D_DIR)


def watermark_off_params(model_dir: Path) -> dict[str, float]:
    """模型自带 `no_watermark.exp3.json` 时，把它的参数作为每帧基础覆盖。

    有些免费 Live2D 模型把“关水印”做成表情文件；如果直接切表情，会和情绪
    表情互相覆盖，所以这里只取参数，每帧重新压回去。
    """
    result: dict[str, float] = {}
    for path in sorted(Path(model_dir).rglob("no_watermark.exp3.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            for item in data.get("Parameters", []):
                param = str(item.get("Id") or "").strip()
                if not param:
                    continue
                try:
                    result[param] = float(item.get("Value"))
                except (TypeError, ValueError):
                    continue
        except Exception:  # noqa: BLE001 —— 坏表情文件不影响正常加载
            logger.debug("关水印表情读不了：{}", path)
    return result


def prepare_runtime_model(model_file: Path) -> Path:
    """把模型旁边的 Exp3 / motion3 自动挂进 model3.json。

    官方模型常见「文件都在，但 model3.json 没写 Expressions/Motions」。
    这里不改原文件，只在同目录生成一份运行时副本，并保留原有配置。
    """
    source = Path(model_file)
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    refs = raw.setdefault("FileReferences", {})

    if not refs.get("Expressions"):
        expressions: list[dict[str, str]] = []
        used: set[str] = set()
        for path in sorted(source.parent.rglob("*.exp3.json")):
            alias = _alias_for_expression(path.stem)
            if alias in used:
                suffix = 2
                while f"{alias}_{suffix}" in used:
                    suffix += 1
                alias = f"{alias}_{suffix}"
            used.add(alias)
            expressions.append(
                {
                    "Name": alias,
                    "File": path.relative_to(source.parent).as_posix(),
                }
            )
        if expressions:
            refs["Expressions"] = expressions

    if not refs.get("Motions"):
        motion_files = sorted(source.parent.rglob("*.motion3.json"))
        idle = [
            path
            for path in motion_files
            if "idle" in path.stem.lower() or path.stem.upper().startswith("IDLE")
        ]
        others = [path for path in motion_files if path not in idle]
        motions: dict[str, list[dict[str, Any]]] = {}
        if idle:
            motions["Idle"] = [
                {
                    "File": path.relative_to(source.parent).as_posix(),
                    "FadeInTime": 0.5,
                    "FadeOutTime": 0.5,
                }
                for path in idle
            ]
        if others:
            motions["Action"] = [
                {
                    "File": path.relative_to(source.parent).as_posix(),
                    "FadeInTime": 0.3,
                    "FadeOutTime": 0.3,
                }
                for path in others
            ]
        if motions:
            refs["Motions"] = motions

    groups = raw.setdefault("Groups", [])
    if not any(group.get("Name") == "LipSync" for group in groups):
        groups.append(
            {
                "Target": "Parameter",
                "Name": "LipSync",
                "Ids": ["ParamMouthOpenY"],
            }
        )

    target = source.parent / RUNTIME_JSON
    target.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


class Live2DRenderer:
    """一个进程一个 Live2D 上下文；朋友切换时按需换模型。"""

    def __init__(self) -> None:
        self._model: Any = None
        self._glfw: Any = None
        self._window: Any = None
        self._gl: Any = None
        self._live2d: Any = None
        self._model_file: Path | None = None
        self._loaded_for: str | None = None
        self._loaded_key: str | None = None
        self._runtime_file: Path | None = None
        self._expressions: list[str] = []
        self._param_ids: set[str] = set()
        self._has_idle = False
        self._idle_started = False
        self._base_params: dict[str, float] = {}
        self._failed = False
        self._failed_for: str | None = None
        self._last_tick = time.perf_counter()
        self._mood = ""
        self._param_override = ""
        self._render_size = (0, 0)
        self._last_frame: Image.Image | None = None
        self._stable_content_box: tuple[int, int, int, int] | None = None
        self._pixel_buffer: np.ndarray | None = None
        self._pixel_size = (0, 0)
        self._mood_kind = "calm"
        self._mood_changed_at = time.perf_counter()
        self._param_current: dict[str, float] = {}
        self._param_last_at = time.perf_counter()
        self._idle_seed = random.random() * math.tau
        self._blink_seed = random.random() * 4.7
        self._settings: dict[str, Any] | None = None
        self._expression_defs: list[dict[str, Any]] = []
        self._model_id: str | None = None
        self._settings_mtime = 0
        self._settings_checked_at = 0.0
        self._idle_current = {
            "head_x": 0.0,
            "head_y": 0.0,
            "head_z": 0.0,
            "body_x": 0.0,
            "body_y": 0.0,
            "body_z": 0.0,
            "gaze_x": 0.0,
            "gaze_y": 0.0,
        }
        self._idle_target = dict(self._idle_current)
        self._idle_rate = 1.35
        self._idle_next_at = time.perf_counter() + random.uniform(2.0, 5.0)
        # 呼吸也做慢速随机变奏：频率/幅度每隔几秒换一次，不是死板的固定正弦。
        self._breath_phase = self._idle_seed
        self._breath_rate = random.uniform(1.0, 1.5)
        self._breath_gain = 1.0
        self._breath_next_at = time.perf_counter() + random.uniform(3.5, 8.0)

    @property
    def active(self) -> bool:
        return self._model is not None

    @property
    def model_file(self) -> Path | None:
        return self._model_file

    def _ensure_loaded(self, friend_id: str | None, model_id: str | None = None) -> bool:
        key = f"friend:{friend_id}:{model_id or ''}"
        if self._model is not None and self._loaded_key == key:
            return True
        if self._failed and self._failed_for == key:
            return False
        if self._failed:
            self._failed = False
        model_file = resolve_model_file(friend_id, model_id)
        if model_file is None:
            return False
        return self._ensure_loaded_file(model_file, key=key, friend_id=friend_id)

    def _ensure_loaded_file(
        self,
        model_file: Path,
        *,
        key: str,
        friend_id: str | None = None,
    ) -> bool:
        if self._model is not None and self._loaded_key == key:
            return True
        if self._failed and self._failed_for == key:
            return False
        if self._failed:
            self._failed = False
        if self._model is not None:
            # 换朋友/换模型：只卸模型，保留 GL 上下文（反复 terminate/init 在
            # Windows 上会让某些模型直接崩进程）。
            self._unload_model()
        model_file = Path(model_file).resolve()
        try:
            runtime = prepare_runtime_model(model_file)
            if self._window is None:
                import glfw  # noqa: PLC0415 —— 可选依赖，缺了就走静态立绘
                import live2d  # noqa: PLC0415
                import OpenGL.GL as GL  # noqa: PLC0415

                live2d.enableLog(False)
                if not glfw.init():
                    raise RuntimeError("GLFW 初始化失败")
                glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
                window = glfw.create_window(
                    MAX_RENDER_WIDTH,
                    MAX_RENDER_HEIGHT,
                    "yumeFriend-live2d",
                    None,
                    None,
                )
                if not window:
                    glfw.terminate()
                    raise RuntimeError("开不了隐藏 OpenGL 窗口")
                glfw.make_context_current(window)
                live2d.init()
                live2d.glInit()
                self._glfw = glfw
                self._window = window
                self._gl = GL
                self._live2d = live2d
            else:
                # 换模型只换 model，窗口/上下文一直复用。
                glfw = self._glfw
                live2d = self._live2d
                GL = self._gl
                glfw.make_context_current(self._window)
            model = live2d.Model()
            model.LoadModelJson(str(runtime), create_renderer=False)
            model.CreateRenderer(maskBufferCount=2)
            # ⚠️ 不要开 SDK 的自动眨眼/呼吸：我们每帧自己写
            # ParamEyeLOpen / ParamEyeROpen / ParamBreath（情绪、说话、待机都靠它）。
            # 两套机制同时写同一批参数时，Cubism 的物理会在两套输入之间来回算，
            # 表现为角色每隔几帧整体抽一下（D464 实测）。
            model.SetAutoBlink(False)
            model.SetAutoBreath(False)
            self._has_idle = bool(model.GetMotionGroups().get("Idle"))
            self._idle_started = False
            self._model = model
            self._loaded_for = friend_id
            self._loaded_key = key
            self._model_file = model_file
            self._runtime_file = runtime
            self._expressions = [str(item) for item in model.GetExpressions()]
            try:
                self._param_ids = {str(item) for item in model.GetParamIds()}
            except Exception:  # noqa: BLE001 —— 老模型没有这个接口也不影响
                self._param_ids = set()
            self._base_params = watermark_off_params(model_file.parent)
            self._reload_model_settings()
            self._idle_current = {key: 0.0 for key in self._idle_current}
            self._idle_target = dict(self._idle_current)
            self._idle_next_at = time.perf_counter() + random.uniform(0.8, 2.5)
            self._last_tick = time.perf_counter()
            logger.info(
                "桌宠 Live2D 已加载：{}（表情 {} 个）",
                model_file,
                len(self._expressions),
            )
            return True
        except Exception:  # noqa: BLE001
            self._failed = True
            self._failed_for = key
            logger.opt(exception=True).warning("Live2D 加载失败，回退静态立绘")
            return False

    def _reload_model_settings(self) -> None:
        if self._model_file is None:
            return
        model = live2d_settings_service.model_info_for_file(self._model_file)
        if model is None:
            self._settings = None
            self._expression_defs = []
            return
        self._settings = live2d_settings_service.load_settings(model)
        self._expression_defs = live2d_settings_service.expression_defs_for_model(model)
        self._model_id = model.id
        self._settings_mtime = live2d_settings_service.settings_mtime(model.id)
        self._settings_checked_at = time.perf_counter()

    def _set_expression(self, mood: str) -> None:
        if self._model is None or mood == self._mood:
            return
        self._mood = mood
        self._mood_kind = mood_expression_kind(mood)
        self._mood_changed_at = time.perf_counter()
        name = None
        normalized = {
            re.sub(r"\.exp3(?:\.json)?$", "", item, flags=re.IGNORECASE).lower(): item
            for item in self._expressions
        }
        for candidate in expression_candidates_for_mood(mood):
            key = candidate.lower()
            if key in normalized:
                name = normalized[key]
                break
            # 有的模型把表情名写成 `foo.exp3.json` / 带中文后缀，做一次包含匹配。
            for expr_key, expr_name in normalized.items():
                if key and key in expr_key:
                    name = expr_name
                    break
            if name:
                break
        try:
            if name:
                self._model.SetExpression(name)
            else:
                self._model.ResetExpression()
        except Exception:  # noqa: BLE001
            logger.debug("Live2D 切表情失败（继续用当前表情）")

    def _set_param(self, model: Any, param: str, value: float) -> None:
        """只在模型真有这个参数时写，顺手按模型的 min/max 夹一下。"""
        if self._param_ids and param not in self._param_ids:
            return
        try:
            low = float(model.GetParamMinById(param))
            high = float(model.GetParamMaxById(param))
            if high > low:
                value = max(low, min(high, value))
        except Exception:  # noqa: BLE001 —— 没范围接口就按标准 Cubism 范围来
            pass
        try:
            model.SetParamById(param, float(value))
        except Exception:  # noqa: BLE001
            pass

    def _update_idle_offsets(self, now: float, dt: float) -> None:
        # 呼吸相位用累计时间推进；频率/幅度随机慢变，避免固定正弦的机械感。
        self._breath_phase += dt * self._breath_rate
        if now >= self._breath_next_at:
            self._breath_rate = random.uniform(0.85, 1.65)
            self._breath_gain = random.uniform(0.72, 1.28)
            self._breath_next_at = now + random.uniform(3.5, 8.0)

        idle = (self._settings or {}).get("idle") or {}
        if not idle.get("enabled", True):
            self._idle_target = {key: 0.0 for key in self._idle_target}
        elif now >= self._idle_next_at:
            # D465：待机晃动不是固定节奏。幅度在设置值上给一个“可见下限”，
            # 目标间隔/追踪速度也随机变化，看起来像人在换重心而不是机械摆钟。
            amplitudes = {
                # D466：用户要的是模型参数里的身体倾斜/视线上下，不是平移。
                # 这套模型的 ParamBodyAngle 对画面影响偏小，所以下限给得比
                # 默认设置更明显；用户自己调得更大时仍用用户的。
                "head_x": max(4.0, float(idle.get("head_x", 2.0))),
                "head_y": max(2.6, float(idle.get("head_y", 1.2))),
                "head_z": max(2.8, float(idle.get("head_z", 1.5))),
                "body_x": max(3.6, float(idle.get("body_x", 1.2))),
                "body_y": max(2.2, float(idle.get("body_y", 1.4))),
                "body_z": max(1.8, float(idle.get("body_z", 0.8))),
                "gaze_x": max(0.28, float(idle.get("gaze_x", 0.14))),
                "gaze_y": max(0.20, float(idle.get("gaze_y", 0.10))),
            }
            self._idle_target = {
                # 保证每次都有明确的左右/上下方向，不在 0 附近磨蹭；
                # 方向、幅度、间隔都随机。
                key: random.choice((-1.0, 1.0))
                * float(value)
                * random.uniform(0.62, 1.0)
                for key, value in amplitudes.items()
            }
            self._idle_rate = random.uniform(1.0, 1.9)
            self._idle_next_at = now + random.uniform(2.4, 6.8)
        alpha = 1.0 - math.exp(-dt * self._idle_rate)
        for key, target in self._idle_target.items():
            current = self._idle_current.get(key, 0.0)
            self._idle_current[key] = current + (target - current) * alpha

    def _apply_settings_mood_params(self, model: Any, mood: str, now: float, speaking: bool) -> None:
        if self._model_id and now - self._settings_checked_at >= 2.0:
            self._settings_checked_at = now
            current_mtime = live2d_settings_service.settings_mtime(self._model_id)
            if current_mtime != self._settings_mtime:
                self._reload_model_settings()
        settings = self._settings or {}
        targets, label = live2d_settings_service.target_params_for_label(
            settings,
            mood,
            self._expression_defs,
        )
        now_at = time.perf_counter()
        dt = max(0.001, min(0.1, now_at - self._param_last_at))
        self._param_last_at = now_at
        self._update_idle_offsets(now, dt)

        idle_breath = math.sin(self._breath_phase) * self._breath_gain
        idle = settings.get("idle") or {}
        targets["ParamAngleX"] = targets.get("ParamAngleX", 0.0) + self._idle_current["head_x"]
        targets["ParamAngleY"] = targets.get("ParamAngleY", 0.0) + self._idle_current["head_y"]
        targets["ParamAngleZ"] = targets.get("ParamAngleZ", 0.0) + self._idle_current["head_z"]
        targets["ParamBodyAngleX"] = (
            targets.get("ParamBodyAngleX", 0.0) + self._idle_current["body_x"]
        )
        targets["ParamBodyAngleY"] = (
            targets.get("ParamBodyAngleY", 0.0) + self._idle_current["body_y"]
        )
        targets["ParamBodyAngleZ"] = (
            targets.get("ParamBodyAngleZ", 0.0) + self._idle_current["body_z"]
        )
        targets["ParamEyeBallX"] = (
            targets.get("ParamEyeBallX", 0.0) + self._idle_current["gaze_x"]
        )
        targets["ParamEyeBallY"] = (
            targets.get("ParamEyeBallY", 0.0) + self._idle_current["gaze_y"]
        )
        targets["ParamBreath"] = targets.get("ParamBreath", 0.5) + idle_breath * float(
            idle.get("breath", 0.24)
        )
        targets["ParamHairFront"] = targets.get("ParamHairFront", 0.0) + math.sin(
            now * 0.83 + self._idle_seed
        ) * 0.5
        targets["ParamHairSide"] = targets.get("ParamHairSide", 0.0) + math.sin(
            now * 0.71 + self._idle_seed
        ) * 0.45
        targets["ParamHairBack"] = targets.get("ParamHairBack", 0.0) + math.sin(
            now * 0.61 + self._idle_seed
        ) * 0.4

        blink_phase = (now + self._blink_seed) % 4.3
        blink = 1.0
        if blink_phase < 0.16:
            blink = 1.0 - 0.92 * math.sin(math.pi * blink_phase / 0.16)
        targets["ParamEyeLOpen"] = targets.get("ParamEyeLOpen", 1.0) * blink
        targets["ParamEyeROpen"] = targets.get("ParamEyeROpen", 1.0) * blink

        if speaking and (settings.get("speech") or {}).get("enabled", True):
            mouth_open = float((settings.get("speech") or {}).get("mouth_open", 0.62))
            syllable = max(0.0, math.sin(now * 11.3)) * (
                0.55 + 0.45 * math.sin(now * 3.1 + self._idle_seed)
            )
            targets["ParamMouthOpenY"] = 0.10 + syllable * mouth_open
            targets["ParamMouthForm"] = targets.get("ParamMouthForm", 0.0) + math.sin(
                now * 8.5
            ) * 0.12
            targets["ParamAngleX"] = targets.get("ParamAngleX", 0.0) + math.sin(now * 7.5) * 1.8
            targets["ParamAngleY"] = targets.get("ParamAngleY", 0.0) + math.sin(now * 5.1) * 1.0
            targets["ParamBodyAngleX"] = (
                targets.get("ParamBodyAngleX", 0.0) + math.sin(now * 5.7) * 1.2
            )

        duration_ms = label.get("transition_ms")
        if duration_ms is None:
            duration_ms = settings.get("transition_ms", 320)
        duration = max(0.05, float(duration_ms) / 1000.0)
        blend = 1.0 - math.exp(-dt * 3.0 / duration)
        for param, target in targets.items():
            current = self._param_current.get(param, target)
            current += (target - current) * blend
            self._param_current[param] = current
            self._set_param(model, param, current)

    def _apply_preset_mood_params(
        self,
        model: Any,
        mood: str,
        now: float,
        speaking: bool,
        data: dict[str, Any],
    ) -> None:
        """用共享 mood preset 直接写参数；大肥鱼的 PARAM_EXP 组合也在这里。"""
        base, preset = resolve_mood_preset(data, mood)
        targets = dict(base)
        if preset:
            for key, value in (preset.get("params") or {}).items():
                if isinstance(value, (int, float)):
                    targets[str(key)] = float(value)

        idle_sway = math.sin(now * 0.37 + self._idle_seed)
        idle_breath = math.sin(now * 1.25 + self._idle_seed * 0.7)
        targets["ParamAngleX"] = targets.get("ParamAngleX", 0.0) + math.sin(
            (now + self._idle_seed) * 0.9
        ) * 1.4
        targets["ParamAngleY"] = targets.get("ParamAngleY", 0.0) + math.sin(
            (now + self._idle_seed) * 0.66
        ) * 0.9
        targets["ParamAngleZ"] = targets.get("ParamAngleZ", 0.0) + idle_sway * 1.2
        targets["ParamBodyAngleX"] = targets.get("ParamBodyAngleX", 0.0) + math.sin(
            (now + self._idle_seed) * 0.56
        ) * 1.2
        targets["ParamBodyAngleZ"] = targets.get("ParamBodyAngleZ", 0.0) + idle_sway * 0.9
        targets["ParamBreath"] = targets.get("ParamBreath", 0.5) + idle_breath * 0.24
        targets["ParamEyeBallX"] = targets.get("ParamEyeBallX", 0.0) + math.sin(
            now * 0.53 + self._idle_seed
        ) * 0.14
        targets["ParamEyeBallY"] = targets.get("ParamEyeBallY", 0.0) + math.sin(
            now * 0.41 + self._idle_seed * 1.3
        ) * 0.1
        targets["ParamHairFront"] = targets.get("ParamHairFront", 0.0) + math.sin(
            now * 0.83 + self._idle_seed
        ) * 0.5
        targets["ParamHairSide"] = targets.get("ParamHairSide", 0.0) + math.sin(
            now * 0.71 + self._idle_seed
        ) * 0.45
        targets["ParamHairBack"] = targets.get("ParamHairBack", 0.0) + math.sin(
            now * 0.61 + self._idle_seed
        ) * 0.4

        blink_phase = (now + self._blink_seed) % 4.3
        blink = 1.0
        if blink_phase < 0.16:
            blink = 1.0 - 0.92 * math.sin(math.pi * blink_phase / 0.16)
        targets["ParamEyeLOpen"] = targets.get("ParamEyeLOpen", 1.0) * blink
        targets["ParamEyeROpen"] = targets.get("ParamEyeROpen", 1.0) * blink

        if speaking:
            syllable = max(0.0, math.sin(now * 11.3)) * (
                0.55 + 0.45 * math.sin(now * 3.1 + self._idle_seed)
            )
            targets["ParamMouthOpenY"] = 0.12 + syllable * 0.55
            targets["ParamMouthForm"] = targets.get("ParamMouthForm", 0.0) + math.sin(
                now * 8.5
            ) * 0.12
            targets["ParamAngleX"] = targets.get("ParamAngleX", 0.0) + math.sin(now * 7.5) * 2.2
            targets["ParamAngleY"] = targets.get("ParamAngleY", 0.0) + math.sin(now * 5.1) * 1.2
            targets["ParamAngleZ"] = targets.get("ParamAngleZ", 0.0) + math.sin(now * 6.3) * 1.4
            targets["ParamBodyAngleX"] = targets.get("ParamBodyAngleX", 0.0) + math.sin(now * 5.7) * 1.6
            targets["ParamBodyAngleZ"] = targets.get("ParamBodyAngleZ", 0.0) + math.sin(now * 4.9) * 1.0
            targets["ParamBreath"] = targets.get("ParamBreath", 0.5) + 0.08

        now_at = time.perf_counter()
        dt = max(0.001, min(0.1, now_at - self._param_last_at))
        self._param_last_at = now_at
        blend = 1.0 - math.exp(-dt * 10.5)
        for param, target in targets.items():
            current = self._param_current.get(param, target)
            current += (target - current) * blend
            self._param_current[param] = current
            self._set_param(model, param, current)

    def _apply_mood_params(
        self, model: Any, mood: str, now: float, speaking: bool = False
    ) -> None:
        """不依赖 exp3 的情绪参数层：脸、眉眼、嘴、脸颊、身体和头部都动。

        有些 CC0 模型只有一个表情文件（甚至文件名叫 `Mouse.exp3.json`），
        光切 exp3 根本看不出情绪。这里用 Cubism 标准参数兜底，让任何模型
        都能明显看出高兴 / 害羞 / 生气 / 难过 / 惊讶 / 疑惑等状态。
        """
        if self._settings is not None:
            self._apply_settings_mood_params(model, mood, now, speaking)
            return
        preset_data = load_mood_presets()
        if preset_data is not None:
            self._apply_preset_mood_params(model, mood, now, speaking, preset_data)
            return
        kind = mood_expression_kind(mood)
        phase = now + self._idle_seed
        burst_age = max(0.0, now - self._mood_changed_at)
        burst = math.exp(-burst_age * 2.4) * math.sin(burst_age * 11.0)

        # 先归零，避免上一个情绪的眼型/嘴型/眉毛残留。
        baseline = {
            "ParamEyeLOpen": 1.0,
            "ParamEyeROpen": 1.0,
            "ParamEyeLSmile": 0.0,
            "ParamEyeRSmile": 0.0,
            "ParamEyeSmile": 0.0,
            "ParamEyeAnger": 0.0,
            "ParamEyePuff": 0.0,
            "ParamMouthForm": 0.0,
            "ParamMouthOpenY": 0.0,
            "ParamCheek": 0.0,
            "ParamBrowLY": 0.0,
            "ParamBrowRY": 0.0,
            "ParamBrowLAngle": 0.0,
            "ParamBrowRAngle": 0.0,
            "ParamBrowLForm": 0.0,
            "ParamBrowRForm": 0.0,
            "ParamEyeBallX": 0.0,
            "ParamEyeBallY": 0.0,
            "ParamMouthX": 0.0,
            "ParamAngleX": 0.0,
            "ParamAngleY": 0.0,
            "ParamAngleZ": 0.0,
            "ParamBodyAngleX": 0.0,
            "ParamBodyAngleY": 0.0,
            "ParamBodyAngleZ": 0.0,
            "ParamBreath": 0.5,
            "ParamHairFront": 0.0,
            "ParamHairSide": 0.0,
            "ParamHairBack": 0.0,
            # 免费模型常用自定义参数做“脸红 / 黑脸 / 眼型 / 嘴型”；
            # 有就压到 0，再按情绪单独给值；没有就自动跳过。
            "PARAM_EXP1": 0.0,
            "PARAM_EXP2": 0.0,
            "PARAM_EXP3": 0.0,
            "PARAM_EXP4": 0.0,
            "PARAM_EXP5": 0.0,
            "PARAM_EXP6": 0.0,
            "PARAM_EXP7": 0.0,
            "PARAM_EXP8": 0.0,
        }
        targets = dict(baseline)
        # 待机微动：呼吸、重心、视线、头发。它们是“一直活着”的底噪，
        # 不抢情绪，但能让模型不像一张静止贴图。
        idle_sway = math.sin(now * 0.37 + self._idle_seed)
        idle_breath = math.sin(now * 1.25 + self._idle_seed * 0.7)
        gaze_x = math.sin(now * 0.53 + self._idle_seed) * 0.14
        gaze_y = math.sin(now * 0.41 + self._idle_seed * 1.3) * 0.1
        targets.update(
            {
                "ParamAngleX": math.sin(phase * 0.9) * 1.4,
                "ParamAngleY": math.sin(phase * 0.66) * 0.9,
                "ParamAngleZ": idle_sway * 1.2,
                "ParamBodyAngleX": math.sin(phase * 0.56) * 1.2,
                "ParamBodyAngleZ": idle_sway * 0.9,
                "ParamBreath": 0.5 + idle_breath * 0.24,
                "ParamEyeBallX": gaze_x,
                "ParamEyeBallY": gaze_y,
                "ParamHairFront": math.sin(now * 0.83 + self._idle_seed) * 0.5,
                "ParamHairSide": math.sin(now * 0.71 + self._idle_seed) * 0.45,
                "ParamHairBack": math.sin(now * 0.61 + self._idle_seed) * 0.4,
            }
        )

        values: dict[str, float] = {}
        if kind == "happy":
            values = {
                "ParamEyeLOpen": 0.82,
                "ParamEyeROpen": 0.82,
                "ParamEyeLSmile": 1.0,
                "ParamEyeRSmile": 1.0,
                "ParamEyeSmile": 1.0,
                "ParamMouthForm": 0.85,
                "ParamMouthOpenY": 0.3,
                "ParamCheek": 0.7,
                "ParamBrowLY": 0.35,
                "ParamBrowRY": 0.35,
                "ParamAngleX": 3.0,
                "ParamAngleY": -4.0,
                "ParamAngleZ": -8.0 + burst * 3.0,
                "ParamBodyAngleX": 2.6 + burst * 1.5,
                "PARAM_EXP2": 1.0,
            }
        elif kind == "excited":
            values = {
                "ParamEyeLOpen": 0.95,
                "ParamEyeROpen": 0.95,
                "ParamEyeLSmile": 1.0,
                "ParamEyeRSmile": 1.0,
                "ParamEyeSmile": 1.0,
                "ParamMouthForm": 0.9,
                "ParamMouthOpenY": 0.55,
                "ParamCheek": 0.9,
                "ParamBrowLY": 0.5,
                "ParamBrowRY": 0.5,
                "ParamAngleX": 5.0,
                "ParamAngleY": -6.0 + burst * 4.0,
                "ParamAngleZ": -10.0 + burst * 4.0,
                "ParamBodyAngleX": 4.0 + burst * 2.0,
                "PARAM_EXP2": 1.0,
            }
        elif kind == "shy":
            values = {
                "ParamEyeLOpen": 0.3,
                "ParamEyeROpen": 0.3,
                "ParamEyeLSmile": 0.6,
                "ParamEyeRSmile": 0.6,
                "ParamEyeSmile": 0.6,
                "ParamMouthForm": 0.5,
                "ParamMouthOpenY": 0.08,
                "ParamCheek": 1.0,
                "ParamBrowLY": -0.25,
                "ParamBrowRY": -0.25,
                "ParamAngleX": -5.0,
                "ParamAngleY": 6.0,
                "ParamAngleZ": 12.0 + burst * 3.0,
                "ParamEyeBallX": -0.25,
                "ParamEyeBallY": -0.22,
                "PARAM_EXP4": 1.0,
            }
        elif kind == "love":
            values = {
                "ParamEyeLOpen": 0.45,
                "ParamEyeROpen": 0.45,
                "ParamEyeLSmile": 1.0,
                "ParamEyeRSmile": 1.0,
                "ParamEyeSmile": 1.0,
                "ParamMouthForm": 0.8,
                "ParamMouthOpenY": 0.14,
                "ParamCheek": 1.0,
                "ParamAngleX": 4.0,
                "ParamAngleY": 3.0,
                "ParamAngleZ": 8.0 + burst * 3.0,
                "ParamBodyAngleX": 1.8,
                "ParamEyeBallX": 0.2,
                "ParamEyeBallY": -0.16,
                "PARAM_EXP4": 1.0,
                "PARAM_EXP7": 1.0,
            }
        elif kind == "sad":
            values = {
                "ParamEyeLOpen": 0.22,
                "ParamEyeROpen": 0.22,
                "ParamMouthForm": -0.8,
                "ParamMouthOpenY": 0.06,
                "ParamBrowLY": -0.55,
                "ParamBrowRY": -0.55,
                "ParamBrowLAngle": -0.55,
                "ParamBrowRAngle": 0.55,
                "ParamAngleX": -3.0,
                "ParamAngleY": 12.0,
                "ParamAngleZ": 6.0,
                "ParamBodyAngleX": -3.0,
                "ParamEyeBallY": -0.28,
                "PARAM_EXP3": 1.0,
                "PARAM_EXP7": -1.0,
            }
        elif kind == "worried":
            values = {
                "ParamEyeLOpen": 0.62,
                "ParamEyeROpen": 0.62,
                "ParamMouthForm": -0.55,
                "ParamBrowLY": 0.25,
                "ParamBrowRY": 0.25,
                "ParamBrowLAngle": 0.45,
                "ParamBrowRAngle": -0.45,
                "ParamAngleX": -4.0,
                "ParamAngleY": 8.0,
                "ParamAngleZ": 5.0,
                "ParamBodyAngleX": -1.5,
                "ParamEyeBallY": -0.12,
            }
        elif kind == "afraid":
            values = {
                "ParamEyeLOpen": 1.0,
                "ParamEyeROpen": 1.0,
                "ParamMouthForm": -0.4,
                "ParamMouthOpenY": 0.5,
                "ParamBrowLY": 0.55,
                "ParamBrowRY": 0.55,
                "ParamBrowLAngle": 0.35,
                "ParamBrowRAngle": -0.35,
                "ParamAngleX": 3.0,
                "ParamAngleY": 6.0,
                "ParamAngleZ": -6.0,
                "ParamBodyAngleX": -3.5,
                "ParamEyeBallY": 0.15,
                "ParamEyePuff": 0.25,
            }
        elif kind == "angry":
            values = {
                "ParamEyeLOpen": 0.5,
                "ParamEyeROpen": 0.5,
                "ParamMouthForm": -0.85,
                "ParamMouthOpenY": 0.12,
                "ParamBrowLAngle": -1.0,
                "ParamBrowRAngle": 1.0,
                "ParamBrowLY": 0.3,
                "ParamBrowRY": 0.3,
                "ParamAngleX": burst * 5.0,
                "ParamAngleY": 8.0,
                "ParamAngleZ": -7.0 + burst * 2.0,
                "ParamBodyAngleX": 3.5,
                "ParamEyeBallX": 0.25,
                "ParamEyeAnger": 1.0,
                "PARAM_EXP3": 1.0,
                "PARAM_EXP5": -1.0,
            }
        elif kind == "surprised":
            values = {
                "ParamEyeLOpen": 1.0,
                "ParamEyeROpen": 1.0,
                "ParamMouthOpenY": 0.78,
                "ParamMouthForm": 0.1,
                "ParamBrowLY": 0.7,
                "ParamBrowRY": 0.7,
                "ParamAngleX": 2.0,
                "ParamAngleY": -10.0 + burst * 4.0,
                "ParamAngleZ": 5.0,
                "ParamBodyAngleY": burst * 2.0,
                "ParamEyeBallY": 0.25,
                "PARAM_EXP5": 1.0,
                "PARAM_EXP8": 1.0,
            }
        elif kind == "nervous":
            values = {
                "ParamEyeLOpen": 0.7,
                "ParamEyeROpen": 0.7,
                "ParamMouthForm": -0.35,
                "ParamMouthOpenY": 0.08,
                "ParamBrowLY": 0.2,
                "ParamBrowRY": 0.2,
                "ParamAngleX": math.sin(now * 6.2) * 4.0,
                "ParamAngleY": 4.0,
                "ParamAngleZ": 4.0 + math.sin(now * 5.1) * 2.0,
                "ParamBodyAngleZ": math.sin(now * 4.4) * 1.8,
                "ParamEyeBallX": math.sin(now * 7.0) * 0.35,
                "ParamEyePuff": 0.45,
                "PARAM_EXP8": -1.0,
            }
        elif kind == "confused":
            values = {
                "ParamEyeLOpen": 0.85,
                "ParamEyeROpen": 0.75,
                "ParamBrowLY": 0.65,
                "ParamBrowRY": -0.2,
                "ParamBrowLAngle": 0.15,
                "ParamBrowRAngle": 0.45,
                "ParamMouthForm": -0.2,
                "ParamMouthOpenY": 0.08,
                "ParamAngleX": 7.0,
                "ParamAngleY": 2.0,
                "ParamAngleZ": -12.0 + burst * 2.0,
                "ParamEyeBallX": 0.25,
                "ParamEyeBallY": 0.12,
                "PARAM_EXP5": 1.0,
                "PARAM_EXP8": 1.0,
            }
        elif kind == "curious":
            values = {
                "ParamEyeLOpen": 1.0,
                "ParamEyeROpen": 0.85,
                "ParamBrowLY": 0.45,
                "ParamBrowRY": 0.1,
                "ParamMouthForm": 0.1,
                "ParamMouthOpenY": 0.1,
                "ParamAngleX": 6.0,
                "ParamAngleY": -2.0,
                "ParamAngleZ": -8.0,
                "ParamEyeBallX": 0.3,
                "ParamEyeBallY": 0.1,
            }
        elif kind == "smug":
            values = {
                "ParamEyeLOpen": 0.5,
                "ParamEyeROpen": 0.5,
                "ParamEyeLSmile": 0.8,
                "ParamEyeRSmile": 0.8,
                "ParamEyeSmile": 0.8,
                "ParamMouthForm": 0.65,
                "ParamMouthOpenY": 0.1,
                "ParamBrowLY": 0.1,
                "ParamBrowRY": 0.4,
                "ParamAngleX": 4.0,
                "ParamAngleY": 2.0,
                "ParamAngleZ": 9.0,
                "ParamEyeBallX": 0.2,
                "PARAM_EXP1": 1.0,
            }
        elif kind == "serious":
            values = {
                "ParamEyeLOpen": 0.9,
                "ParamEyeROpen": 0.9,
                "ParamMouthForm": -0.2,
                "ParamBrowLY": -0.15,
                "ParamBrowRY": -0.15,
                "ParamBrowLAngle": -0.2,
                "ParamBrowRAngle": 0.2,
                "ParamAngleY": 4.0,
                "ParamAngleZ": 2.0,
                "ParamBodyAngleX": 0.8,
                "ParamEyeBallY": -0.1,
            }
        elif kind == "helpless":
            values = {
                "ParamEyeLOpen": 0.45,
                "ParamEyeROpen": 0.45,
                "ParamMouthForm": -0.4,
                "ParamMouthOpenY": 0.1,
                "ParamBrowLY": 0.3,
                "ParamBrowRY": 0.3,
                "ParamBrowLAngle": 0.35,
                "ParamBrowRAngle": -0.35,
                "ParamAngleX": -3.0,
                "ParamAngleY": 8.0,
                "ParamAngleZ": -6.0,
                "ParamBodyAngleX": -1.2,
            }
        elif kind == "dizzy":
            values = {
                "ParamEyeLOpen": 0.15,
                "ParamEyeROpen": 0.15,
                "ParamMouthForm": -0.25,
                "ParamMouthOpenY": 0.12,
                "ParamAngleZ": 18.0 * math.sin(now * 3.2),
                "ParamBodyAngleZ": 4.0 * math.cos(now * 2.7),
                "PARAM_EXP6": 1.0,
            }
        elif kind == "thinking":
            values = {
                "ParamEyeLOpen": 0.85,
                "ParamEyeROpen": 0.7,
                "ParamBrowLY": 0.6,
                "ParamBrowRY": 0.1,
                "ParamBrowLAngle": 0.2,
                "ParamBrowRAngle": 0.4,
                "ParamMouthForm": -0.15,
                "ParamAngleX": 8.0,
                "ParamAngleY": 4.0,
                "ParamAngleZ": -12.0 + burst * 1.5,
                "ParamBodyAngleX": -0.8,
                "ParamEyeBallX": 0.32,
                "ParamEyeBallY": 0.22,
                "PARAM_EXP5": 1.0,
            }
        elif kind == "sleepy":
            values = {
                "ParamEyeLOpen": 0.18,
                "ParamEyeROpen": 0.18,
                "ParamMouthForm": -0.2,
                "ParamMouthOpenY": 0.16,
                "ParamAngleX": -2.0,
                "ParamAngleY": 10.0,
                "ParamAngleZ": 6.0,
                "ParamBodyAngleX": -1.5,
            }
        elif kind == "pout":
            values = {
                "ParamEyeLOpen": 0.65,
                "ParamEyeROpen": 0.65,
                "ParamMouthForm": -0.8,
                "ParamMouthOpenY": 0.08,
                "ParamCheek": 0.8,
                "ParamBrowLY": 0.25,
                "ParamBrowRY": 0.25,
                "ParamBrowLAngle": -0.3,
                "ParamBrowRAngle": 0.3,
                "ParamAngleX": -2.0,
                "ParamAngleY": 4.0,
                "ParamAngleZ": 8.0,
                "ParamMouthX": 0.35,
                "PARAM_EXP8": -1.0,
            }

        targets.update(values)

        # 眨眼：EyeBlink 组在某些免费模型里是空的，这里自己做一个。
        blink_phase = (now + self._blink_seed) % 4.3
        blink = 1.0
        if blink_phase < 0.16:
            blink = 1.0 - 0.92 * math.sin(math.pi * blink_phase / 0.16)
        targets["ParamEyeLOpen"] = targets.get("ParamEyeLOpen", 1.0) * blink
        targets["ParamEyeROpen"] = targets.get("ParamEyeROpen", 1.0) * blink

        if speaking:
            # 说话时口型按“音节包络”开合，不是机械正弦；头、肩、身体一起动。
            syllable = max(0.0, math.sin(now * 11.3)) * (
                0.55 + 0.45 * math.sin(now * 3.1 + self._idle_seed)
            )
            targets["ParamMouthOpenY"] = 0.12 + syllable * 0.55
            targets["ParamMouthForm"] = targets.get("ParamMouthForm", 0.0) + math.sin(
                now * 8.5
            ) * 0.12
            targets["ParamAngleX"] = targets.get("ParamAngleX", 0.0) + math.sin(now * 7.5) * 2.2
            targets["ParamAngleY"] = targets.get("ParamAngleY", 0.0) + math.sin(now * 5.1) * 1.2
            targets["ParamAngleZ"] = targets.get("ParamAngleZ", 0.0) + math.sin(now * 6.3) * 1.4
            targets["ParamBodyAngleX"] = targets.get("ParamBodyAngleX", 0.0) + math.sin(now * 5.7) * 1.6
            targets["ParamBodyAngleZ"] = targets.get("ParamBodyAngleZ", 0.0) + math.sin(now * 4.9) * 1.0
            targets["ParamBreath"] = targets.get("ParamBreath", 0.5) + 0.08
        # 平滑过渡：情绪切换不硬切，参数朝目标靠，同时保留呼吸/摆动的连续感。
        now_at = time.perf_counter()
        dt = max(0.001, min(0.1, now_at - self._param_last_at))
        self._param_last_at = now_at
        blend = 1.0 - math.exp(-dt * 10.5)
        for param, target in targets.items():
            current = self._param_current.get(param, target)
            current += (target - current) * blend
            self._param_current[param] = current
            self._set_param(model, param, current)

    def _resize_if_needed(self, width: int, height: int) -> None:
        if (width, height) == self._render_size or self._model is None:
            return
        self._model.Resize(width, height)
        self._render_size = (width, height)
        # 裁剪框坐标跟画布尺寸绑定；换尺寸后必须重新量一次，
        # 否则旧框会被拿去裁新画布，角色会突然偏掉/被切。
        self._stable_content_box = None

    def _read_frame(self, width: int, height: int) -> Image.Image:
        """读回**预乘 alpha** 的帧。

        Cubism 的混合公式是 `ONE / ONE_MINUS_SRC_ALPHA`，所以 glReadPixels 拿到的
        本来就是预乘 RGBA。这里直接交给 PIL 的 ``RGBa`` 模式（它原生就是预乘
        alpha），不做逐像素 numpy 反预乘 —— 原来那一步光 420×880 就要 8~14ms。
        """
        GL = self._gl
        # 复用一个 GL 上下文换模型时，上一轮可能留下无害的 GL 状态错误；
        # 先清掉，否则 PyOpenGL 会在 glFinish/glReadPixels 处把它抛出来。
        for _ in range(16):
            if GL.glGetError() == GL.GL_NO_ERROR:
                break
        GL.glFinish()
        if (
            self._pixel_buffer is None
            or self._pixel_size != (width, height)
        ):
            self._pixel_buffer = np.empty((height, width, 4), dtype=np.uint8)
            self._pixel_size = (width, height)
        GL.glReadPixels(
            0,
            0,
            width,
            height,
            GL.GL_RGBA,
            GL.GL_UNSIGNED_BYTE,
            self._pixel_buffer,
        )
        # OpenGL 原点在左下，PIL 在左上；flip 交给 Pillow 的 C 实现。
        frame = Image.frombuffer(
            "RGBa",
            (width, height),
            self._pixel_buffer,
            "raw",
            "RGBa",
            0,
            1,
        )
        return frame.transpose(Image.Transpose.FLIP_TOP_BOTTOM)

    def _stable_crop_box(self, frame: Image.Image) -> tuple[int, int, int, int]:
        """只量一次透明边，之后**永远用同一个固定裁剪框**。

        以前是"外接框只扩不缩"：角色呼吸/摆头时外接框慢慢变大，裁出来的
        角色图尺寸也跟着变；桌宠再用 GDI 按这张图重新缩放，于是角色每一两秒
        就轻微缩放一下，看起来一直在抖。

        现在第一帧量出内容框，四周留足余量后冻结。之后不管模型怎么呼吸、
        摆头，输出的图片尺寸都不变，桌宠的缩放/定位也就完全稳定。
        """
        width, height = frame.size
        if self._stable_content_box is not None:
            return self._stable_content_box
        box = frame.getbbox() or (0, 0, width, height)
        content_width = max(1, box[2] - box[0])
        content_height = max(1, box[3] - box[1])
        # 横向给 10% 余量（待机摆头一般只占 2~4%），纵向给 3% / 1%。
        # 这些余量是"防切边"的，不是拿来逐帧扩框的。
        pad_x = max(16, int(content_width * 0.10))
        pad_top = max(8, int(content_height * 0.03))
        pad_bottom = max(4, int(content_height * 0.01))
        padded = (
            max(0, box[0] - pad_x),
            max(0, box[1] - pad_top),
            min(width, box[2] + pad_x),
            min(height, box[3] + pad_bottom),
        )
        self._stable_content_box = padded
        return padded

    @staticmethod
    def _fit_to_area(frame: Image.Image, area_width: int, area_height: int) -> Image.Image:
        """把预乘 alpha 图裁掉透明边、按比例缩到区域里。

        预乘图可以直接 resize（线性插值在预乘空间里才是对的），不用先转直通 alpha。
        """
        if area_width <= 0 or area_height <= 0:
            return frame
        ratio = min(area_width / frame.width, area_height / frame.height)
        size = (
            max(1, int(frame.width * ratio)),
            max(1, int(frame.height * ratio)),
        )
        # 上采样用 NEAREST：桌宠原生窗口每帧都要再把图交给 GDI，
        # 双线性放大 900→1200+ 会吃掉 7~10ms；这里优先保住高帧。
        # 下采样仍用双线性，缩小不会变慢太多。
        method = (
            Image.Resampling.BILINEAR
            if ratio < 1.0
            else Image.Resampling.NEAREST
        )
        return frame.resize(size, method)

    def render(
        self,
        *,
        friend_id: str | None = None,
        model_id: str | None = None,
        mood: str,
        speaking: bool,
        area_width: int,
        area_height: int,
        premultiplied: bool = False,
        fit: bool = True,
    ) -> Image.Image | None:
        """渲染一帧；不可用时返回 None（调用方回退静态立绘）。

        ``premultiplied=True`` 时返回 PIL 的 ``RGBa`` 图（预乘 alpha），给桌宠的
        快速合成路径直接贴到分层窗口；默认仍返回普通 ``RGBA``，方便测试和旧调用。
        ``fit=False`` 时只裁掉透明边、不缩放到区域大小；桌宠用 GDI StretchBlt
        在窗口 DIB 里做硬件更快的放大，避免 PIL 在大尺寸下逐帧重采样。
        """
        if not self._ensure_loaded(friend_id, model_id):
            return None
        try:
            assert self._model is not None
            model = self._model

            # ⚠️ Windows 上 time.monotonic() 只有 ~15.6ms 分辨率（GetTickCount64），
            # 120fps 下会出现"0ms / 16ms"交替的 delta，动画和物理一顿一顿。
            # 动画时间轴必须用高精度 QPC（perf_counter）。
            now = time.perf_counter()
            delta = max(0.0, min(0.1, now - self._last_tick))
            self._last_tick = now

            cw, ch = model.GetCanvasSize()
            # 先按显示高度做 1.15× 超采样，再交给 GDI 缩到窗口里。
            # 这样大窗口不会只拿 1000px 的图硬拉，同时仍给 120fps 留余量。
            desired_height = int(max(1, area_height) * PET_RENDER_SUPERSAMPLE)
            target_height = min(
                MAX_RENDER_HEIGHT,
                PET_RENDER_HEIGHT,
                max(RENDER_WIDTH, desired_height),
            )
            target_width = int(target_height * (cw / max(1.0, ch)))
            if target_width > MAX_RENDER_WIDTH:
                target_width = MAX_RENDER_WIDTH
                target_height = int(target_width * (ch / max(1.0, cw)))
            render_width = max(1, min(MAX_RENDER_WIDTH, target_width))
            render_height = max(1, min(MAX_RENDER_HEIGHT, target_height))
            self._resize_if_needed(render_width, render_height)

            model.Update(delta)
            for param, value in self._base_params.items():
                model.SetParamById(param, value)
            # 以前每帧只要 IsMotionFinished() 为真就重启 Idle；YUI 会每帧都
            # 返回 true，等于每帧重启动画（约 1ms 额外开销），视觉上也会抖。
            # 模型自己的 Idle 只在载入后启动一次，后续连续待机交给参数层。
            if self._has_idle and not self._idle_started:
                try:
                    model.StartMotion("Idle", 0)
                except Exception:  # noqa: BLE001
                    logger.debug("Live2D 模型没有可用 Idle 动作")
                self._idle_started = True

            # 不依赖 exp3 的情绪参数层：任何模型都能看出脸和身体在变。
            self._apply_mood_params(model, mood, now, speaking)
            GL = self._gl
            GL.glViewport(0, 0, render_width, render_height)
            self._live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
            model.Draw()
            frame = self._read_frame(render_width, render_height)
            cropped = frame.crop(self._stable_crop_box(frame))
            if fit:
                fitted = self._fit_to_area(cropped, area_width, area_height)
            else:
                fitted = cropped
            if premultiplied:
                self._last_frame = fitted
                return fitted
            straight = fitted.convert("RGBA")
            self._last_frame = straight
            return straight
        except Exception:  # noqa: BLE001
            self._failed = True
            self._failed_for = friend_id
            logger.opt(exception=True).warning("Live2D 渲染失败，回退静态立绘")
            self.close()
            return None

    def close(self) -> None:
        self._unload_model()
        try:
            import live2d  # noqa: PLC0415

            live2d.dispose()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._glfw is not None:
                self._glfw.terminate()
        except Exception:  # noqa: BLE001
            pass
        self._glfw = None
        self._window = None
        self._gl = None
        self._live2d = None
        self._last_frame = None
        self._stable_content_box = None
        self._pixel_buffer = None
        self._pixel_size = (0, 0)

    def _unload_model(self) -> None:
        """卸掉当前模型，保留可复用的 GL 上下文。"""
        try:
            if self._model is not None:
                self._model.DestroyRenderer()
        except Exception:  # noqa: BLE001
            pass
        self._model = None
        self._loaded_for = None
        self._loaded_key = None
        self._model_file = None
        self._runtime_file = None
        self._expressions = []
        self._has_idle = False
        self._idle_started = False
        self._base_params = {}
        self._param_current = {}
        self._render_size = (0, 0)
        self._stable_content_box = None
        self._pixel_buffer = None
        self._pixel_size = (0, 0)
        self._mood = ""
        self._param_override = ""
        self._settings = None
        self._expression_defs = []
        self._model_id = None
