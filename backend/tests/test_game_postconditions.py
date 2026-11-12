"""v5.1-P3 / D288：结构化后置条件。"""

from backend.game.postconditions import evaluate_checks, failure_text, passed


def _snapshot():
    return {
        "player": {"x": 10.0, "y": 64.0, "z": -5.0, "health": 16, "food": 12},
        "inventory": [{"item": "oak_log", "count": 5}],
        "held": {"item": "stone_pickaxe", "count": 1},
        "blocks": {
            "below": {"name": "dirt", "x": 10, "y": 63, "z": -5},
            "front": {"name": "oak_planks", "x": 10, "y": 64, "z": -4},
        },
        "nearby_blocks": [
            {"name": "torch", "count": 4},
            {"name": "stone", "count": 20},
        ],
        "nearby_entities": [
            {"name": "cow", "type": "mob", "distance": 2.8},
            {"name": "YumeshioAmami", "type": "player", "distance": 6.0},
        ],
    }


def test_all_check_types_pass_with_structured_observations():
    checks = [
        {"type": "inventory_has", "item": "oak_log", "count": ">=5"},
        {"type": "block_at", "x": 10, "y": 64, "z": -4, "block": "oak_planks"},
        {"type": "block_count", "block": "torch", "count": ">=4", "radius": 10},
        {"type": "entity_near", "name": "cow", "radius": 3, "count": ">=1"},
        {"type": "position_near", "x": 10, "y": 64, "z": -5, "distance": 0.1},
        {"type": "health", "value": ">=10"},
        {"type": "food", "value": ">=10"},
        {"type": "task_result", "path": "mined", "value": ">=4"},
    ]
    results = evaluate_checks(
        checks,
        _snapshot(),
        {"mined": 6, "ok": True},
    )
    assert len(results) == 8
    assert passed(results), failure_text(results)
    assert {item["type"] for item in results} == {
        "inventory_has",
        "block_at",
        "block_count",
        "entity_near",
        "position_near",
        "health",
        "food",
        "task_result",
    }


def test_failures_report_observed_values():
    results = evaluate_checks(
        [
            {"type": "inventory_has", "item": "oak_log", "count": ">=8"},
            {"type": "block_at", "x": 10, "y": 64, "z": -4, "block": "stone"},
            {"type": "entity_near", "name": "cow", "radius": 1},
            {"type": "position_near", "x": 100, "y": 64, "z": -5, "distance": 2},
            {"type": "health", "value": ">=20"},
            {"type": "food", "value": ">=20"},
            {"type": "task_result", "path": "mined", "value": ">=8"},
        ],
        _snapshot(),
        {"mined": 2},
    )
    assert not passed(results)
    assert results[0]["observed"] == 5
    assert results[1]["observed"] == "oak_planks"
    assert "背包 oak_log = 5" in failure_text(results)


def test_unknown_check_fails_closed():
    results = evaluate_checks([{"type": "made_up"}], _snapshot())
    assert not passed(results)
    assert "不认识" in results[0]["message"]
