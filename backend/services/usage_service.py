"""LLM 真实用量统计（D331）。

每个 provider 请求单独落一行 JSONL；agent 回合再把所有工具轮加总。
日志不记聊天明文、不记 key，只记 token / 模型 / 用途 / request_id。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from backend.config import LOG_DIR

USAGE_LOG_PATH = LOG_DIR / "usage.jsonl"
_LOCK = threading.Lock()

_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "total_tokens",
)


def _number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """把不同 provider 的 usage 字段归一成同一套计数。"""
    raw = _mapping(usage)
    prompt_details = _mapping(
        raw.get("prompt_tokens_details") or raw.get("input_tokens_details")
    )
    completion_details = _mapping(
        raw.get("completion_tokens_details") or raw.get("output_tokens_details")
    )

    input_tokens = _number(raw.get("prompt_tokens") or raw.get("input_tokens"))
    output_tokens = _number(
        raw.get("completion_tokens") or raw.get("output_tokens")
    )
    total_tokens = _number(raw.get("total_tokens")) or (input_tokens + output_tokens)
    reasoning_tokens = _number(
        raw.get("reasoning_tokens") or completion_details.get("reasoning_tokens")
    )
    cache_hit_tokens = _number(
        raw.get("prompt_cache_hit_tokens")
        or raw.get("cache_hit_tokens")
        or prompt_details.get("cached_tokens")
    )
    cache_miss_tokens = _number(
        raw.get("prompt_cache_miss_tokens") or raw.get("cache_miss_tokens")
    )
    if not cache_miss_tokens and input_tokens:
        cache_miss_tokens = max(0, input_tokens - cache_hit_tokens)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "total_tokens": total_tokens,
    }


def merge_usage(
    total: Mapping[str, Any] | None, usage: Mapping[str, Any] | None
) -> dict[str, int]:
    """累加一轮里的多次 provider 调用。"""
    merged = {field: _number(_mapping(total).get(field)) for field in _FIELDS}
    normalized = normalize_usage(usage)
    for field in _FIELDS:
        merged[field] += normalized[field]
    return merged


def record_call(
    *,
    provider: str,
    model: str,
    purpose: str,
    usage: Mapping[str, Any] | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """记录一次真实的模型请求；返回落盘的那条记录。"""
    normalized = normalize_usage(usage)
    reported = bool(usage)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": model,
        "purpose": purpose,
        "request_id": request_id,
        "usage_reported": reported,
        **normalized,
    }
    USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _LOCK:
        with USAGE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line)
    return record


def _empty_totals() -> dict[str, int]:
    return {
        "calls": 0,
        "usage_missing_calls": 0,
        **{field: 0 for field in _FIELDS},
    }


def _add_record(totals: dict[str, int], record: Mapping[str, Any]) -> None:
    totals["calls"] += 1
    if not record.get("usage_reported"):
        totals["usage_missing_calls"] += 1
    for field in _FIELDS:
        totals[field] += _number(record.get(field))


def summary(days: int = 1, *, recent_limit: int = 20) -> dict[str, Any]:
    """按天汇总 usage.jsonl；不存在的文件按 0 处理。"""
    days = max(1, min(int(days or 1), 90))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    totals = _empty_totals()
    by_model: dict[str, dict[str, int]] = {}
    by_purpose: dict[str, dict[str, int]] = {}
    by_day: dict[str, dict[str, int]] = {}
    recent: list[dict[str, Any]] = []

    if USAGE_LOG_PATH.is_file():
        with USAGE_LOG_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                try:
                    stamp = datetime.fromisoformat(str(record.get("ts") or ""))
                except ValueError:
                    continue
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if stamp < since:
                    continue
                _add_record(totals, record)
                model = str(record.get("model") or "?")
                purpose = str(record.get("purpose") or "chat")
                day = stamp.astimezone(timezone.utc).date().isoformat()
                _add_record(by_model.setdefault(model, _empty_totals()), record)
                _add_record(by_purpose.setdefault(purpose, _empty_totals()), record)
                _add_record(by_day.setdefault(day, _empty_totals()), record)
                recent.append(record)

    recent = recent[-max(1, int(recent_limit or 1)) :]
    recent.reverse()
    return {
        "days": days,
        "since": since.isoformat(),
        "totals": totals,
        "by_model": by_model,
        "by_purpose": by_purpose,
        "by_day": by_day,
        "recent": recent,
    }
