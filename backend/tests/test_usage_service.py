"""D331：真实 LLM 用量统计。"""

from __future__ import annotations

import json

from backend.services import usage_service


def test_normalize_usage_handles_deepseek_cache_and_reasoning_fields():
    normalized = usage_service.normalize_usage(
        {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 400,
            "completion_tokens_details": {"reasoning_tokens": 180},
        }
    )
    assert normalized == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "reasoning_tokens": 180,
        "cache_hit_tokens": 800,
        "cache_miss_tokens": 400,
        "total_tokens": 1500,
    }


def test_merge_usage_adds_every_tool_round():
    first = usage_service.merge_usage(
        {},
        {"prompt_tokens": 100, "completion_tokens": 20},
    )
    merged = usage_service.merge_usage(
        first,
        {
            "prompt_tokens": 250,
            "completion_tokens": 50,
            "prompt_cache_hit_tokens": 200,
            "completion_tokens_details": {"reasoning_tokens": 30},
        },
    )
    assert merged["input_tokens"] == 350
    assert merged["output_tokens"] == 70
    assert merged["cache_hit_tokens"] == 200
    assert merged["reasoning_tokens"] == 30
    assert merged["total_tokens"] == 420


def test_record_and_summary_keep_each_real_call(tmp_path, monkeypatch):
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(usage_service, "USAGE_LOG_PATH", log_path)

    usage_service.record_call(
        provider="deepseek",
        model="deepseek-flash",
        purpose="game",
        usage={"prompt_tokens": 100, "completion_tokens": 20},
        request_id="req-1",
    )
    usage_service.record_call(
        provider="deepseek",
        model="deepseek-flash",
        purpose="game",
        usage={
            "prompt_tokens": 300,
            "completion_tokens": 40,
            "prompt_cache_hit_tokens": 250,
        },
        request_id="req-2",
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["request_id"] == "req-1"

    data = usage_service.summary(days=1, recent_limit=10)
    assert data["totals"]["calls"] == 2
    assert data["totals"]["input_tokens"] == 400
    assert data["totals"]["output_tokens"] == 60
    assert data["totals"]["cache_hit_tokens"] == 250
    assert data["by_model"]["deepseek-flash"]["calls"] == 2
    assert data["by_purpose"]["game"]["calls"] == 2
    assert len(data["recent"]) == 2
