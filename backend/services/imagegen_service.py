"""图片生成（v2.2 / T2.2-5 / D117）。

口径：
· 走 NanoGPT 的 **OpenAI 兼容**端点 `POST {base_url}/images/generations`
  （实测：{model, prompt, n, size} → `{created, data:[{b64_json}], cost, remainingBalance, requestId}`）
· 默认模型 **`nsfw-gen-illustrious`**（在网关目录里显示为 "Animagine XL 4.0"，
  `censored: false`，$0.0051/张，1024×1024，实测 7 秒出图）
· 配置在设置的「图片生成」用途里；**没配就跟随聊天那条**（同一个网关、同一把 key）
· 失败一律给人话（D83），绝不让聊天崩掉；**一次只画一张**（省钱）
"""

from __future__ import annotations

import base64
import json
import re
import time

import httpx
from loguru import logger

from backend import config
from backend.services.errors import ServiceError

DEFAULT_MODEL = "nsfw-gen-illustrious"
DEFAULT_SIZE = "1024x1024"
# 图生图（D129）：denoising_strength = "像参考图多少"。0.5 很贴参考图、0.85 很自由。
DEFAULT_STRENGTH = 0.7
# 出图是"秒级"的慢活（实测 7s；慢的时候几十秒）——给足时间，但别无限等
TIMEOUT_SECONDS = 180.0

# 画风后缀：Animagine XL 官方推荐的质量标签（照抄，别自己发明）
STYLE_SUFFIX = "masterpiece, best quality, very aesthetic, absurdres"

# D118：这个模型（Animagine XL 4.0 / SDXL 底模）**只认英文 Danbooru 标签**。
# 实测：中文自然语言提示词 → 一格一格糊脸；英文标签 → 正常出图（连露骨内容都出得来）。
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")


def _purpose_conf(session) -> dict:  # noqa: ANN001 —— Session（避免循环 import）
    from backend.services import settings_service  # noqa: PLC0415

    purposes = settings_service.effective_purposes_from(settings_service.get(session))
    conf = purposes.get("image") if isinstance(purposes.get("image"), dict) else {}
    chat = purposes.get("chat") if isinstance(purposes.get("chat"), dict) else {}
    return {
        "base_url": (conf.get("base_url") or chat.get("base_url") or "").rstrip("/"),
        "api_key": conf.get("api_key") or chat.get("api_key") or "",
        "model": conf.get("model") or DEFAULT_MODEL,
        "size": conf.get("size") or DEFAULT_SIZE,
        "strength": conf.get("strength"),
        # 2026-09-27：画图也认「网络走法」—— nginx 那类境外网关直连不通时要走梯子。
        # 图片那栏没配就跟随聊天那栏（跟 base_url / api_key 一个口径）。
        "proxy": conf.get("proxy") if conf.get("proxy") is not None else chat.get("proxy", ""),
    }


def _client(conf: dict, *, timeout: float | None = None) -> httpx.AsyncClient:
    """按「网络走法」造 client（2026-09-27 加）。

    以前这里写死 `trust_env=False`（纯直连）—— 梯子开着也用不上，nano-gpt 直连不通时
    画图就整个废掉。现在：
      · 显式指定了代理 → 就用它；`none/direct` → 强制直连；
      · `auto` 且系统代理活着 → **直接走它**（画图是一次很贵的长请求，不学聊天那样先白等一次直连）；
      · 空 → 跟随系统/环境变量。
    """
    from backend.llm.openai_compat import resolve_proxy, system_proxy_alive  # noqa: PLC0415

    value = (conf.get("proxy") or "").strip()
    limit = TIMEOUT_SECONDS if timeout is None else timeout
    if value.lower() == "auto":
        # ⚠️ "auto" 不能直接丢给 httpx（它只认空串/URL，"auto" 会报 "Unknown scheme for proxy URL"）。
        # 画图这条：系统代理活着就直接走它，不在就直连。
        return httpx.AsyncClient(timeout=limit, trust_env=system_proxy_alive())
    proxy, trust_env = resolve_proxy(value)
    return httpx.AsyncClient(timeout=limit, proxy=proxy, trust_env=trust_env)


def _strength_of(conf: dict) -> float:
    """图生图的强度（D129）：没配就 0.7；夹在 0.1~1.0。"""
    try:
        value = float(conf.get("strength"))
    except (TypeError, ValueError):
        return DEFAULT_STRENGTH
    return max(0.1, min(1.0, value))


def build_request(*, conf: dict, prompt: str, ref_png: bytes | None = None) -> tuple[str, str, dict]:
    """拼请求：返回 `(路径后缀, 模式, 参数字典)`。**纯函数，好测**。

    带参考图（`ref_png`）→ 走 `/images/edits`（多部分表单）+ `denoising_strength`（实测有效，
    出来会"照着参考图的构图换内容"）；不带 → 照旧 `/images/generations`（D117 那条路不许坏）。
    """
    text = f"{prompt}, {STYLE_SUFFIX}"
    if ref_png is not None:
        return (
            "/images/edits",
            "multipart",
            {
                "model": conf["model"],
                "prompt": text,
                "n": "1",  # 表单字段都是字符串
                "size": conf["size"],
                # 实测：`denoising_strength` 有效；`strength` 那个字段名网关也认（两个都留着更稳）
                "denoising_strength": str(_strength_of(conf)),
            },
        )
    return (
        "/images/generations",
        "json",
        {
            "model": conf["model"],
            "prompt": text,
            "n": 1,  # 一次只画一张：省钱，也让等待短
            "size": conf["size"],
        },
    )


async def generate(prompt: str, *, session, ref_png: bytes | None = None) -> dict:  # noqa: ANN001
    """画一张（带参考图就是"照着这张画"，D129），存进 `user_data/images/`。

    返回 `{rel,width,height,bytes,cost,model}`；失败抛 `ServiceError`（带人话），
    调用方会把它当工具结果回灌给模型（D83）。
    """
    text = (prompt or "").strip()
    if not text:
        raise ServiceError("IMAGE_PROMPT_EMPTY", "你想画什么？给我一个画面描述就行", 400)

    conf = _purpose_conf(session)
    if not conf["base_url"]:
        raise ServiceError(
            "IMAGE_NOT_CONFIGURED",
            "还没配图片生成（设置 → 模型 → 图片生成：填服务地址和模型）",
            400,
        )
    if not conf["api_key"]:
        raise ServiceError("IMAGE_NO_KEY", "图片生成没有可用的密钥（设置 → 模型 → 图片生成）", 400)

    if _CJK.search(text):
        # 不拦（用户要什么就画什么），但要留痕：中文提示词出来的图基本是糊的
        logger.warning("画图提示词里有中文 —— 这个模型只认英文 Danbooru 标签，出图可能糊")

    suffix, mode, fields = build_request(conf=conf, prompt=text, ref_png=ref_png)
    if ref_png is not None:
        logger.info("图生图：{}（强度 {}）", conf["model"], _strength_of(conf))
    started = time.monotonic()
    try:
        async with _client(conf) as client:
            if mode == "multipart":
                response = await client.post(
                    f"{conf['base_url']}{suffix}",
                    headers={"Authorization": f"Bearer {conf['api_key']}"},
                    data=fields,
                    files={"image": ("ref.png", ref_png, "image/png")},
                )
            else:
                response = await client.post(
                    f"{conf['base_url']}{suffix}",
                    headers={"Authorization": f"Bearer {conf['api_key']}"},
                    json=fields,
                )
    except httpx.HTTPError as exc:
        raise ServiceError(
            "IMAGE_UNREACHABLE",
            f"画图服务连不上（{type(exc).__name__}）—— 检查网络/梯子，等会儿再试",
            503,
        ) from exc

    if response.status_code >= 400:
        raise ServiceError(
            "IMAGE_FAILED",
            f"画不出来（HTTP {response.status_code}）：{response.text[:160]}",
            502,
        )
    try:
        body = response.json()
        item = (body.get("data") or [{}])[0]
    except (json.JSONDecodeError, AttributeError, IndexError) as exc:
        raise ServiceError("IMAGE_BAD_RESPONSE", "画图服务回了个看不懂的东西", 502) from exc

    raw: bytes | None = None
    if item.get("b64_json"):
        try:
            raw = base64.b64decode(item["b64_json"])
        except Exception as exc:  # noqa: BLE001
            raise ServiceError("IMAGE_BAD_BASE64", "图片数据坏了", 502) from exc
    elif item.get("url"):
        try:
            async with _client(conf, timeout=120.0) as client:
                got = await client.get(item["url"])
                got.raise_for_status()
                raw = got.content
        except httpx.HTTPError as exc:
            raise ServiceError("IMAGE_DOWNLOAD_FAILED", "图生成了，但下载不回来", 502) from exc
    if not raw:
        raise ServiceError("IMAGE_EMPTY", "画图服务没给图", 502)

    from backend.services import image_service  # noqa: PLC0415 —— 懒导入，避免循环

    saved = image_service.save_capture(raw)
    logger.info(
        "画图完成：{} {} 秒（{}×{}，{} 字节，费用 {}）",
        conf["model"],
        f"{time.monotonic() - started:.1f}",
        saved["width"],
        saved["height"],
        saved["bytes"],
        body.get("cost"),
    )
    return {
        "rel": saved["rel"],
        "width": saved["width"],
        "height": saved["height"],
        "bytes": saved["bytes"],
        "cost": body.get("cost"),
        "model": conf["model"],
    }
