"""嵌入 + 向量检索（D231 / D450）。

三类 provider：
  · `local`      = fastembed + 已缓存的本地模型名（默认 Jina 中文增强模型）；
  · `local_file` = 直接加载用户选的本地 ONNX 文件夹（model.onnx + tokenizer.json）；
  · `openai`     = 远程 embedding（protocol=openai 走 OpenAI 兼容 `/embeddings`，
                   protocol=gemini 走 Gemini 原生 `batchEmbedContents`）。

**默认不联网下载模型**：本地模型一律 `local_files_only=True`，只有高级设置里
显式打开 `allow_download` 时才让 fastembed 自己去 HuggingFace 拉。普通设置的
可选下载走 `embedding_download_service`，从我们的资源页拉已经打包好的缓存。

口径（跟其它"可选能力"一致，D83）：
  · 没装模型 / 模型加载失败 / 编码报错 → **返回 None**，调用方退回纯关键词，绝不 500；
  · 模型懒加载；失败一次就记 WARNING，不反复重试。
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config
from backend.models import Memory, MemoryEmbedding

# 兼容旧调用点 / 测试：默认模型的别名。
MODEL_NAME = config.EMBEDDING_DEFAULT_MODEL
DIM = config.EMBEDDING_DEFAULT_DIM
VECTOR_MIN_SIMILARITY = config.EMBEDDING_DEFAULT_MIN_SIMILARITY

BACKFILL_BATCH = 64
VECTOR_TOP_K = 12

_lock = threading.Lock()
_model = None
_model_signature: tuple[str, str, bool] | None = None
_load_failed = False
_active: EmbeddingConfig | None = None
_custom_registered = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool = False
    provider: str = "local"
    protocol: str = "openai"
    model: str = MODEL_NAME
    dim: int = DIM
    min_similarity: float = VECTOR_MIN_SIMILARITY
    cache_dir: str = ""
    allow_download: bool = False
    base_url: str = ""
    api_key: str = ""
    model_path: str = ""
    pooling: str = "mean"
    max_length: int = 512

    @property
    def local_cache_dir(self) -> Path:
        return Path(self.cache_dir).expanduser() if self.cache_dir else config.MODEL_CACHE_DIR

    @property
    def signature(self) -> tuple:
        return (
            self.provider,
            self.protocol,
            self.model,
            str(self.local_cache_dir),
            self.allow_download,
            self.model_path,
            self.pooling,
            self.max_length,
        )

    @property
    def storage_model(self) -> str:
        """写进 `memory_embeddings.model` 的标识；换模型 / 换端点后旧向量自动过期。"""
        if self.provider == "openai":
            return f"{self.protocol}:{self.base_url}:{self.model}"
        if self.provider == "local_file":
            return f"local_file:{self.model_path}"
        return self.model


def config_from_dict(conf: dict | None) -> EmbeddingConfig:
    data = conf if isinstance(conf, dict) else {}
    provider = str(data.get("provider") or "local").strip().lower()
    if provider in {"api", "remote", "openai-compatible"}:
        provider = "openai"
    if provider not in {"local", "local_file", "openai"}:
        provider = "local"
    protocol = str(data.get("protocol") or "openai").strip().lower()
    if protocol not in {"openai", "gemini"}:
        protocol = "openai"
    try:
        dim = int(data.get("dim") or DIM)
    except (TypeError, ValueError):
        dim = DIM
    try:
        threshold = float(data.get("min_similarity") or VECTOR_MIN_SIMILARITY)
    except (TypeError, ValueError):
        threshold = VECTOR_MIN_SIMILARITY
    try:
        max_length = int(data.get("max_length") or 512)
    except (TypeError, ValueError):
        max_length = 512
    pooling = str(data.get("pooling") or "mean").strip().lower()
    if pooling not in {"mean", "cls"}:
        pooling = "mean"
    return EmbeddingConfig(
        enabled=bool(data.get("enabled", False)),
        provider=provider,
        protocol=protocol,
        model=str(data.get("model") or MODEL_NAME).strip() or MODEL_NAME,
        dim=max(1, dim),
        min_similarity=min(1.0, max(0.0, threshold)),
        cache_dir=str(data.get("cache_dir") or "").strip(),
        allow_download=bool(data.get("allow_download", False)),
        base_url=str(data.get("base_url") or "").strip().rstrip("/"),
        api_key=str(data.get("api_key") or ""),
        model_path=str(data.get("model_path") or "").strip(),
        pooling=pooling,
        max_length=max(16, min(8192, max_length)),
    )


def config_from_session(session: Session) -> EmbeddingConfig:
    from backend.services import settings_service  # noqa: PLC0415 —— 避免循环 import

    conf = settings_service.effective_purposes_from(settings_service.get(session)).get(
        "embedding"
    )
    return config_from_dict(conf if isinstance(conf, dict) else {})


def _active_config() -> EmbeddingConfig:
    global _active
    if _active is not None:
        return _active
    try:
        from backend.db import session_scope  # noqa: PLC0415

        with session_scope() as session:
            _active = config_from_session(session)
    except Exception:  # noqa: BLE001 —— 设置读不出来就退回默认（关）
        _active = EmbeddingConfig()
    return _active


def invalidate() -> None:
    """设置变了 / 模型下载完了：下次调用重新读配置、重新懒加载。"""
    global _active, _model, _model_signature, _load_failed
    with _lock:
        _active = None
        _model = None
        _model_signature = None
        _load_failed = False


# ---------- 模型（懒加载）----------


def _ensure_custom_models() -> None:
    """把「Jina 中文量化版」注册进 fastembed（自定义模型名，源仓库仍是原模型）。"""
    global _custom_registered
    if _custom_registered:
        return
    if config.EMBEDDING_DEFAULT_MODEL == "jinaai/jina-embeddings-v2-base-zh-quantized":
        from fastembed import TextEmbedding  # noqa: PLC0415
        from fastembed.common.model_description import ModelSource, PoolingType  # noqa: PLC0415

        try:
            TextEmbedding.add_custom_model(
                model=config.EMBEDDING_DEFAULT_MODEL,
                pooling=PoolingType.MEAN,
                normalization=True,
                sources=ModelSource(hf=config.EMBEDDING_MODEL_SOURCE_REPO),
                dim=config.EMBEDDING_DEFAULT_DIM,
                model_file="onnx/model_quantized.onnx",
                description="Jina Chinese v2 quantized (fastembed)",
                license="apache-2.0",
                size_in_gb=0.16,
            )
        except ValueError:
            # 已注册过（同一个进程里多次加载）就跳过。
            pass
    _custom_registered = True


class _LocalOnnxEmbedding:
    """最小本地 ONNX embedding：tokenizers + onnxruntime，不依赖 fastembed 注册表。"""

    def __init__(self, path: str, *, pooling: str = "mean", max_length: int = 512) -> None:
        from tokenizers import Tokenizer  # noqa: PLC0415
        import onnxruntime as ort  # noqa: PLC0415

        root = Path(path).expanduser()
        if root.is_file() and root.suffix.lower() == ".onnx":
            root = root.parent
        self.root = root
        onnx_files = sorted(root.rglob("*.onnx"))
        tokenizer_files = sorted(root.rglob("tokenizer.json"))
        if not tokenizer_files:
            for parent in (root.parent, root.parent.parent):
                candidate = parent / "tokenizer.json"
                if candidate.is_file():
                    tokenizer_files = [candidate]
                    break
        if not onnx_files:
            raise FileNotFoundError(f"模型文件夹里没有 .onnx：{root}")
        if not tokenizer_files:
            raise FileNotFoundError(f"模型文件夹里没有 tokenizer.json：{root}")
        self.tokenizer = Tokenizer.from_file(str(tokenizer_files[0]))
        self.tokenizer.enable_truncation(max_length=max_length)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self.session = ort.InferenceSession(
            str(onnx_files[0]), providers=["CPUExecutionProvider"]
        )
        self.input_names = [item.name for item in self.session.get_inputs()]
        self.pooling = pooling

    def _pool(self, output, attention_mask):
        import numpy as np  # noqa: PLC0415

        if output.ndim == 2:
            return output
        if output.ndim != 3:
            raise ValueError(f"不认识的 ONNX 输出维度：{output.shape}")
        if self.pooling == "cls":
            return output[:, 0, :]
        mask = attention_mask.astype(np.float32)[:, :, None]
        summed = (output.astype(np.float32) * mask).sum(axis=1)
        counts = np.maximum(mask.sum(axis=1), 1.0)
        return summed / counts

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import numpy as np  # noqa: PLC0415

        encodings = self.tokenizer.encode_batch(list(texts))
        input_ids = np.asarray([item.ids for item in encodings], dtype=np.int64)
        attention_mask = np.asarray(
            [item.attention_mask for item in encodings], dtype=np.int64
        )
        feeds: dict[str, object] = {}
        for name in self.input_names:
            lowered = name.lower()
            if "attention" in lowered or lowered == "mask":
                feeds[name] = attention_mask
            elif "token_type" in lowered or "segment" in lowered:
                feeds[name] = np.zeros_like(input_ids)
            else:
                feeds[name] = input_ids
        outputs = self.session.run(None, feeds)
        output = next((item for item in outputs if getattr(item, "ndim", 0) == 3), outputs[0])
        pooled = self._pool(np.asarray(output), attention_mask)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (pooled / norms).astype(float).tolist()


def local_file_ready(path: str) -> bool:
    """本地模型文件夹是否同时有 ONNX + tokenizer.json。"""
    try:
        root = Path(path).expanduser()
        if root.is_file() and root.suffix.lower() == ".onnx":
            root = root.parent
        if not root.is_dir():
            return False
        has_onnx = bool(list(root.rglob("*.onnx")))
        has_tokenizer = bool(list(root.rglob("tokenizer.json"))) or any(
            (parent / "tokenizer.json").is_file()
            for parent in (root.parent, root.parent.parent)
        )
        return has_onnx and has_tokenizer
    except OSError:
        return False


def _get_local_model(cfg: EmbeddingConfig):
    global _model, _model_signature, _load_failed
    with _lock:
        if _model is not None and _model_signature == cfg.signature:
            return _model
        if _load_failed and _model_signature == cfg.signature:
            return None
        try:
            if cfg.provider == "local_file":
                _model = _LocalOnnxEmbedding(
                    cfg.model_path,
                    pooling=cfg.pooling,
                    max_length=cfg.max_length,
                )
            else:
                from fastembed import TextEmbedding  # noqa: PLC0415 —— 重依赖，用到才 import

                _ensure_custom_models()
                cfg.local_cache_dir.mkdir(parents=True, exist_ok=True)
                _model = TextEmbedding(
                    model_name=cfg.model,
                    cache_dir=str(cfg.local_cache_dir),
                    # D450：默认绝不联网下载；高级设置显式允许才放开。
                    local_files_only=not cfg.allow_download,
                )
            _model_signature = cfg.signature
            _load_failed = False
            logger.info("本地嵌入模型已就绪：{}（{} 维）", cfg.model, cfg.dim)
        except Exception as exc:  # noqa: BLE001
            _model = None
            _model_signature = cfg.signature
            _load_failed = True
            logger.warning(
                "本地嵌入模型不可用（默认不自动下载；普通设置里下载模型，"
                "或高级设置允许联网）：{}",
                exc,
            )
            return None
    return _model


def ready(config_override: EmbeddingConfig | None = None) -> bool:
    """模型**已经**加载好了吗（不触发加载 / 不触发下载）。"""
    cfg = config_override or _active_config()
    if not cfg.enabled:
        return False
    if os.environ.get("YF_MOBILE") == "1":
        from backend import mobile_embedding  # noqa: PLC0415

        return mobile_embedding.ready()
    if cfg.provider == "openai":
        return bool(cfg.base_url and cfg.model)
    return _model is not None and _model_signature == cfg.signature


def warm(config_override: EmbeddingConfig | None = None) -> bool:
    """显式加载模型（后台回填 / 设置页测试用）。返回是否可用。"""
    cfg = config_override or _active_config()
    if not cfg.enabled:
        return False
    if os.environ.get("YF_MOBILE") == "1":
        from backend import mobile_embedding  # noqa: PLC0415

        return mobile_embedding.ready()
    if cfg.provider == "openai":
        return bool(cfg.base_url and cfg.model)
    return _get_local_model(cfg) is not None


def enabled(session: Session) -> bool:
    """设置里开没开（默认关，D450）。"""
    try:
        return config_from_session(session).enabled
    except Exception:  # noqa: BLE001 —— 设置读不出来就按默认（关）走
        return False


# ---------- 编码 ----------


def _openai_embed(texts: Sequence[str], cfg: EmbeddingConfig) -> list[list[float]] | None:
    url = f"{cfg.base_url.rstrip('/')}/embeddings"
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                url,
                headers=headers,
                json={"model": cfg.model, "input": list(texts)},
            )
            response.raise_for_status()
            payload = response.json()
        items = payload.get("data")
        if not isinstance(items, list):
            raise ValueError("embedding 接口没有返回 data")
        items.sort(key=lambda item: int(item.get("index", 0)))
        vectors = [[float(value) for value in item["embedding"]] for item in items]
        if len(vectors) != len(texts):
            raise ValueError(f"返回向量数 {len(vectors)} != 输入数 {len(texts)}")
        return vectors
    except Exception as exc:  # noqa: BLE001 —— 远程失败也要退回关键词
        logger.warning("远程嵌入失败（本轮退回关键词）：{}", exc)
        return None


def _gemini_embed(texts: Sequence[str], cfg: EmbeddingConfig) -> list[list[float]] | None:
    base = cfg.base_url.rstrip("/")
    model = cfg.model
    url = f"{base}/models/{model}:batchEmbedContents"
    params = {"key": cfg.api_key} if cfg.api_key else None
    body = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": text}]},
            }
            for text in texts
        ]
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(url, params=params, json=body)
            response.raise_for_status()
            payload = response.json()
        items = payload.get("embeddings")
        if not isinstance(items, list):
            raise ValueError("Gemini embedding 接口没有返回 embeddings")
        vectors = [[float(value) for value in item["values"]] for item in items]
        if len(vectors) != len(texts):
            raise ValueError(f"返回向量数 {len(vectors)} != 输入数 {len(texts)}")
        return vectors
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini 嵌入失败（本轮退回关键词）：{}", exc)
        return None


def _remote_embed(texts: Sequence[str], cfg: EmbeddingConfig) -> list[list[float]] | None:
    if cfg.protocol == "gemini":
        return _gemini_embed(texts, cfg)
    return _openai_embed(texts, cfg)


def embed_texts(
    texts: Sequence[str],
    *,
    query: bool = False,
    config_override: EmbeddingConfig | None = None,
) -> list[list[float]] | None:
    """编码一批文本；不可用/报错 → None（调用方退回关键词）。"""
    if not texts:
        return []
    if os.environ.get("YF_MOBILE") == "1":
        from backend import mobile_embedding  # noqa: PLC0415

        return mobile_embedding.embed_texts(list(texts))
    cfg = config_override or _active_config()
    if cfg.provider == "openai":
        return _remote_embed(texts, cfg)
    model = _get_local_model(cfg)
    if model is None:
        return None
    try:
        if cfg.provider == "local_file":
            return model.embed(texts)
        if query:
            vectors = list(model.query_embed(list(texts)))
        else:
            vectors = list(model.passage_embed(list(texts)))
        return [[float(value) for value in vector] for vector in vectors]
    except Exception as exc:  # noqa: BLE001
        logger.warning("嵌入失败（本轮退回关键词）：{}", exc)
        return None


def _normalize(vector: Iterable[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _to_blob(vector: Iterable[float]) -> bytes:
    return array("f", _normalize(vector)).tobytes()


def _from_blob(blob: bytes, dim: int) -> list[float]:
    values = array("f")
    values.frombytes(blob[: dim * 4])
    return list(values)


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


# ---------- 表 I/O ----------


def _upsert(
    session: Session, memory: Memory, vector: list[float], *, storage_model: str
) -> None:
    row = session.get(MemoryEmbedding, memory.id)
    blob = _to_blob(vector)
    digest = content_hash(memory.content)
    if row is None:
        session.add(
            MemoryEmbedding(
                memory_id=memory.id,
                friend_id=memory.friend_id,
                model=storage_model,
                dim=len(vector),
                content_hash=digest,
                vector=blob,
                created_at=_now(),
            )
        )
    else:
        row.model = storage_model
        row.dim = len(vector)
        row.content_hash = digest
        row.vector = blob
        row.created_at = _now()


def pending(
    session: Session,
    *,
    limit: int = BACKFILL_BATCH,
    config_override: EmbeddingConfig | None = None,
) -> list[Memory]:
    """还缺向量 / 内容变了 / 换了模型要重算的记忆。"""
    cfg = config_override or config_from_session(session)
    memories = list(
        session.execute(
            select(Memory).where(Memory.deleted_at.is_(None)).order_by(Memory.created_at)
        ).scalars()
    )
    if not memories:
        return []
    stored = {
        row.memory_id: row
        for row in session.execute(
            select(MemoryEmbedding).where(
                MemoryEmbedding.memory_id.in_([memory.id for memory in memories])
            )
        ).scalars()
    }
    missing: list[Memory] = []
    for memory in memories:
        row = stored.get(memory.id)
        if (
            row is None
            or row.model != cfg.storage_model
            or row.content_hash != content_hash(memory.content)
        ):
            missing.append(memory)
        if len(missing) >= limit:
            break
    return missing


def backfill(session: Session, *, batch: int = BACKFILL_BATCH) -> int:
    """补一批向量。返回补了几条（0 = 都齐了 / 或模型不可用）。"""
    cfg = config_from_session(session)
    if not cfg.enabled:
        return 0
    todo = pending(session, limit=batch, config_override=cfg)
    if not todo:
        return 0
    vectors = embed_texts([memory.content for memory in todo], config_override=cfg)
    if vectors is None or len(vectors) != len(todo):
        return 0
    for memory, vector in zip(todo, vectors):
        _upsert(session, memory, vector, storage_model=cfg.storage_model)
    session.flush()
    return len(todo)


# ---------- 向量检索 ----------


def search(
    session: Session,
    friend_id: str,
    query: str,
    *,
    limit: int = VECTOR_TOP_K,
    exclude_ids: set[str] | None = None,
    include_short_term: bool = False,
) -> list[tuple[Memory, float]]:
    """向量召回（按余弦相似度降序）。模型不可用 / 没向量 / 没命中 → 空列表。"""
    cfg = config_from_session(session)
    if not cfg.enabled:
        return []
    vectors = embed_texts([query], query=True, config_override=cfg)
    if not vectors:
        return []
    query_vec = _normalize(vectors[0])
    rows = session.execute(
        select(MemoryEmbedding, Memory)
        .join(Memory, Memory.id == MemoryEmbedding.memory_id)
        .where(
            MemoryEmbedding.friend_id == friend_id,
            MemoryEmbedding.model == cfg.storage_model,
            Memory.deleted_at.is_(None),
        )
    ).all()
    excluded = exclude_ids or set()
    scored: list[tuple[Memory, float]] = []
    for embedding, memory in rows:
        if memory.id in excluded or memory.tag == "rp_play_style":
            continue
        if not include_short_term and memory.layer == "short_term":
            continue
        if len(embedding.vector) != embedding.dim * 4:
            continue
        score = _dot(query_vec, _from_blob(embedding.vector, embedding.dim))
        if score >= cfg.min_similarity:
            scored.append((memory, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def vector_hits(
    session: Session,
    friend_id: str,
    query: str,
    *,
    limit: int = VECTOR_TOP_K,
    exclude_ids: set[str] | None = None,
) -> list[Memory]:
    """给"自动注入"用的向量命中（排除 short_term / rp_play_style）。"""
    return [
        memory
        for memory, _ in search(
            session, friend_id, query, limit=limit, exclude_ids=exclude_ids
        )
    ]


def hybrid_recall(
    session: Session, friend_id: str, query: str, *, limit: int = 10
) -> list[Memory]:
    """`recall_memory` 工具的实现：**关键词 ∪ 向量**，去重。"""
    from backend.services import memory_service  # noqa: PLC0415

    keyword_hits = memory_service.recall(session, friend_id, query, limit=limit)
    seen = {memory.id for memory in keyword_hits}
    merged = list(keyword_hits)
    for memory, _score in search(
        session, friend_id, query, limit=limit, exclude_ids=seen
    ):
        if memory.id in seen:
            continue
        seen.add(memory.id)
        merged.append(memory)
        if len(merged) >= limit:
            break
    return merged[:limit]
