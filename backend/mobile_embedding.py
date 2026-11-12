"""Android 本地 int8 向量模型桥（D301）。

Kotlin 负责 WordPiece + ONNX Runtime；Python 只调用并解析 JSON。
"""

from __future__ import annotations

import json


def ready() -> bool:
    try:
        from java import jclass  # noqa: PLC0415

        return bool(jclass("com.yumefriend.app.EmbeddingBridge").isReady())
    except Exception:  # noqa: BLE001
        return False


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    if not texts:
        return []
    try:
        from java import jclass  # noqa: PLC0415

        bridge = jclass("com.yumefriend.app.EmbeddingBridge")
        result: list[list[float]] = []
        for text in texts:
            raw = str(bridge.embed(str(text)))
            vector = json.loads(raw)
            if not isinstance(vector, list) or not vector:
                return None
            result.append([float(value) for value in vector])
        return result
    except Exception:  # noqa: BLE001
        return None
