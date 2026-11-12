"""v5.1-P3 / D288：确定性 SkillRunner。"""

from __future__ import annotations

import asyncio

import pytest

from backend.game.skills import Skill, SkillRunner


@pytest.mark.asyncio
async def test_three_step_skill_runs_without_llm_and_checks_progress():
    calls: list[str] = []
    state = {"inventory": 0}

    async def command(step, args, _timeout):
        calls.append(step.action)
        if step.action == "mine":
            state["inventory"] += int(args.get("count", 1))
            return {"ok": True, "result": {"mined": args.get("count", 1)}}
        return {"ok": True}

    async def snapshot():
        return {
            "player": {"x": 10, "y": 64, "z": -5},
            "inventory": [{"item": "oak_log", "count": state["inventory"]}],
        }

    skill = Skill.from_dict(
        {
            "name": "wood_three_steps",
            "params": {"count": 3, "x": 10, "y": 64, "z": -5},
            "steps": [
                {
                    "action": "goto",
                    "args": {"x": "{{x}}", "y": "{{y}}", "z": "{{z}}"},
                    "checks": [
                        {
                            "type": "position_near",
                            "x": "{{x}}",
                            "y": "{{y}}",
                            "z": "{{z}}",
                            "distance": 1,
                        }
                    ],
                },
                {
                    "action": "mine",
                    "args": {"block": "oak_log", "count": "{{count}}"},
                    "checks": [
                        {
                            "type": "inventory_has",
                            "item": "oak_log",
                            "count": ">={{count}}",
                        }
                    ],
                },
                {"action": "collect", "checks": []},
            ],
        }
    )
    runner = SkillRunner(command, snapshot, retry_delay=0)
    result = await runner.run(skill)
    assert result.ok is True, result.view()
    assert calls == ["goto", "mine", "collect"]
    assert [step["status"] for step in result.steps] == ["success", "success", "success"]
    assert result.steps[1]["checks"][0]["observed"] == 3


@pytest.mark.asyncio
async def test_timeout_stops_and_reports():
    async def command(_step, _args, _timeout):
        await asyncio.sleep(1)
        return {"ok": True}

    async def snapshot():
        return {}

    runner = SkillRunner(command, snapshot, retry_delay=0)
    result = await runner.run(
        {
            "name": "timeout_skill",
            "steps": [{"action": "goto", "timeout": 0.05, "max_attempts": 1}],
        }
    )
    assert result.ok is False
    assert result.reason == "timeout"
    assert "超时" in result.error


@pytest.mark.asyncio
async def test_three_identical_failed_checks_stop_as_no_progress():
    async def command(_step, _args, _timeout):
        return {"ok": True}

    async def snapshot():
        return {"inventory": []}

    runner = SkillRunner(command, snapshot, retry_delay=0)
    result = await runner.run(
        {
            "name": "no_progress",
            "steps": [
                {
                    "action": "mine",
                    "max_attempts": 3,
                    "checks": [
                        {"type": "inventory_has", "item": "oak_log", "count": ">=1"}
                    ],
                }
            ],
        }
    )
    assert result.ok is False
    assert result.reason == "no_progress"
    assert "没有净进度" in result.error


@pytest.mark.asyncio
async def test_same_error_three_times_stops():
    calls = 0

    async def command(_step, _args, _timeout):
        nonlocal calls
        calls += 1
        raise RuntimeError("路被挡住了")

    async def snapshot():
        return {}

    runner = SkillRunner(command, snapshot, retry_delay=0)
    result = await runner.run(
        {
            "name": "same_error",
            "steps": [{"action": "goto", "max_attempts": 3}],
        }
    )
    assert result.ok is False
    assert result.reason == "same_failure"
    assert calls == 3
