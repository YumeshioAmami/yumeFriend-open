"""桌宠的门面（D121）：真正的窗口在 `pet_native.py`。

`app.py` / 托盘 / 提醒气泡只认这里的几个方法。以前这里是 pywebview 的第二窗口
（做法在 D119/D120 里，透明做不到），现在换成**原生分层窗口**（真透明）。
"""

from __future__ import annotations

from typing import Any, Callable

from backend.features import flags as feature_flags
from backend.desktop.pet_native import (
    DEFAULT_SCALE,
    STATE_FILE,
    PetNativeWindow,
    clamp_scale,
    load_state,
    offscreen_delta,
    save_state,
    scaled_size,
)

__all__ = [
    "DEFAULT_SCALE",
    "STATE_FILE",
    "PetWindow",
    "clamp_scale",
    "load_state",
    "offscreen_delta",
    "save_state",
    "scaled_size",
]


class ShellApi:
    """暴露给**主窗口**的 pywebview js_api（点「桌宠」按钮时用）。

    桌宠本身是原生窗口，不走 js_api；这里只是主窗口 → 外壳的那条路。
    """

    def __init__(
        self,
        pet: "PetWindow",
        window: Any = None,
        ask_save_path: Callable[[str], str | None] | None = None,
    ) -> None:
        self._pet = pet
        # 主窗口（app.py 建好之后交进来）—— 「保存图片」要它弹原生对话框
        self._window = window
        # 测试用的注入口（真跑的时候是 None → 走 pywebview 那个原生框）
        self._ask_save_path = ask_save_path

    def set_window(self, window: Any) -> None:
        """app.py 建好主窗口之后把它交进来（`create_window` 在 PetWindow 之后才跑）。"""
        self._window = window

    def open_pet(self, friend_id: str | None = None) -> dict[str, Any]:
        if not feature_flags.enabled("pet"):
            return {
                "ok": False,
                "error": "「桌宠」这个功能现在是关着的 —— 去设置 → 功能里打开再用",
            }
        return {"ok": self._pet.show(friend_id), "scale": self._pet.scale}

    def hide_pet(self) -> dict[str, Any]:
        self._pet.hide()
        return {"ok": True}

    def exit_pet(self) -> dict[str, Any]:
        self._pet.close()
        return {"ok": True}

    def open_main(self, friend_id: str | None = None) -> dict[str, Any]:
        self._pet.open_main(friend_id)
        return {"ok": True}

    def pet_state(self) -> dict[str, Any]:
        return {
            "open": self._pet.is_open,
            "visible": self._pet.is_visible,
            "friend_id": self._pet.friend_id,
            "scale": self._pet.scale,
        }

    def set_pet_scale(self, scale: float) -> dict[str, Any]:
        return {"scale": self._pet.set_scale(scale)}

    # ── 主窗口那边要的两个外壳动作（D131）──────────────────────────
    def toggle_fullscreen(self) -> dict[str, Any]:
        callback = getattr(self._pet, "_on_toggle_fullscreen", None)
        if callback:
            callback()
        return {"ok": True}

    def set_theme(self, theme: str | None = None) -> dict[str, Any]:
        """前端告诉我们现在是深色还是浅色（用来把标题栏也变深）。"""
        callback = getattr(self._pet, "_on_set_theme", None)
        if callback:
            callback(str(theme or "light"))
        return {"ok": True}

    def _ask_path_default(self, default_name: str) -> str | None:
        """弹**原生「另存为」对话框**（资源管理器那个保存框）—— D163。取消 → None。"""
        import os
        from pathlib import Path

        import webview

        home = Path(os.path.expanduser("~"))
        pictures = home / "Pictures"
        result = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(pictures if pictures.is_dir() else home),
            save_filename=default_name,
            file_types=("图片 (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp)", "所有文件 (*.*)"),
        )
        if not result:
            return None
        chosen = result[0] if isinstance(result, (list, tuple)) else result
        return str(chosen) if chosen else None

    def save_image(self, href: str = "") -> dict[str, Any]:
        """把一张图存到**用户自己选的位置**（D163），并把真实路径回给前端。

        为什么不让网页自己下载：桌面外壳是 WebView2，**它不会自己弹下载、也不会自己存**
        （点了 `<a download>` 什么都不发生 —— 用户实测反馈）。
        所以由 Python 这边：取图 → 弹原生「另存为」→ 写到用户选的文件。
        """
        import re
        from pathlib import Path
        from urllib.parse import unquote, urlparse

        import httpx
        from loguru import logger

        url = str(href or "").strip()
        if not url:
            return {"ok": False, "error": "没给图片地址"}
        # 先把图取下来：默认文件名要靠它，而且别让用户选完位置才发现取不到图
        try:
            response = httpx.get(url, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
            raw = response.content
        except Exception as exc:  # noqa: BLE001 —— 失败要给人话，别抛
            logger.warning("保存图片失败（取图）：{}", exc)
            return {"ok": False, "error": f"取图失败（{type(exc).__name__}）"}

        name = unquote(Path(urlparse(url).path).name) or "image.png"
        if not re.search(r"\.[A-Za-z0-9]{2,5}$", name):
            name += ".png"

        ask = self._ask_save_path
        if ask is None:
            if self._window is None:  # 理论上不会（app.py 会交窗口进来）
                return {"ok": False, "error": "外壳还没准备好（没有主窗口）"}
            ask = self._ask_path_default
        chosen = ask(name)
        if not chosen:
            return {"ok": False, "error": "你取消了"}
        target = Path(chosen)
        if not target.suffix:  # 用户在保存框里没打后缀 → 补回原图那个
            target = target.with_suffix(Path(name).suffix or ".png")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        except OSError as exc:
            logger.warning("保存图片失败（写盘）：{}", exc)
            return {"ok": False, "error": f"写不进文件（{exc}）"}
        logger.info("图片已保存：{}", target)
        return {"ok": True, "path": str(target)}


class PetWindow:
    """桌宠。**公开接口跟 D119 那版一模一样**（app.py / 托盘 / 气泡都不用改）。"""

    def __init__(
        self,
        base_url: str,
        *,
        on_open_main: Callable[[str | None], None] | None = None,
        on_open_game: Callable[[str | None], None] | None = None,
        on_toggle_fullscreen: Callable[[], None] | None = None,
        on_set_theme: Callable[[str], None] | None = None,
        theme: Any = None,  # 兼容旧调用：原生窗口自己决定配色，这个参数不再用
        state_path=STATE_FILE,
    ) -> None:
        del theme
        self._on_toggle_fullscreen = on_toggle_fullscreen
        self._on_set_theme = on_set_theme
        self._window = PetNativeWindow(
            base_url,
            on_open_main=on_open_main,
            on_open_game=on_open_game,
            state_path=state_path,
        )
        self.api = ShellApi(self)

    @property
    def is_open(self) -> bool:
        return self._window.is_open

    @property
    def is_visible(self) -> bool:
        return self._window.is_visible

    @property
    def friend_id(self) -> str | None:
        return self._window.friend_id

    @property
    def scale(self) -> float:
        return self._window.scale

    def show(self, friend_id: str | None = None) -> bool:
        return self._window.show(friend_id)

    def hide(self) -> None:
        self._window.hide()

    def close(self) -> None:
        self._window.close()

    def open_main(self, friend_id: str | None = None) -> None:
        self._window.open_main(friend_id)

    def set_scale(self, value: Any) -> float:
        return self._window.set_scale(value)

    def rect_if_visible(self) -> tuple[int, int, int, int] | None:
        return self._window.rect_if_visible()
