"""手机端调度器与桌面端能力对齐（D356）。"""

from __future__ import annotations

from backend.mobile_scheduler import build_mobile_scheduler


def test_mobile_scheduler_includes_memory_and_learning_jobs():
    scheduler = build_mobile_scheduler(heartbeat_minutes=60)
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert {
        "mobile_catchup",
        "mobile_reflection",
        "mobile_expression_learning",
        "mobile_embedding_backfill",
        "mobile_schedule",
        "mobile_heartbeat",
        "mobile_cleanup",
    } <= job_ids
