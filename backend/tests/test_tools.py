"""T3.1 验收：工具执行层（registry / 兜底 / 权限）。"""

import json
from datetime import datetime, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.agent import tools
from backend.models import Friend
from backend.services import memory_service, schedule_service
from backend.services.actor import Actor


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "tools.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as s:
        s.add(Friend(id="f1", name="阿月", created_at=now, updated_at=now))
    return "f1"


def call(name: str, **arguments) -> dict:
    return {"id": f"tool_{name}", "name": name, "arguments": arguments}


# ---------- look_screen（2026-09-27 / D190）：她自己看一眼屏幕；**一轮只看一次** ----------


def _manifest_names(mode: str) -> set[str]:
    return {line.split("(")[0].lstrip("- ").strip() for line in tools.prompt_lines(mode)}


def test_look_screen_only_in_normal_and_tool_modes():
    """入戏模式不给（一截屏就出戏）——跟 draw_image / web_search 一个口径。"""
    assert "look_screen" in _manifest_names("normal")
    assert "look_screen" in _manifest_names("tool")
    assert "look_screen" not in _manifest_names("roleplay")


def test_look_screen_second_call_in_the_same_turn_is_refused(friend_id):
    """一轮只看一次：第二次直接回一句人话，**不截图、不调视觉模型**。"""
    with db.session_scope() as s:
        refused = tools.execute(s, friend_id, call("look_screen"), allow_screen_lookup=False)
    assert refused["ok"] is False
    assert "一轮只看一次" in refused["error"]


# ---------- manifest ----------


def test_prompt_lines_come_from_the_manifest():
    lines = tools.prompt_lines(game_active=True)
    assert len(lines) == len(tools.TOOLS) - 1  # game_cheat 要作弊开启才出现
    for tool in tools.TOOLS:
        if tool.cheat_only:
            continue
        assert any(tool.name in line for line in lines)
    assert "game_cheat" not in "\n".join(lines)
    cheat_lines = tools.prompt_lines(game_active=True, cheat_active=True)
    assert len(cheat_lines) == len(tools.TOOLS)
    assert "game_cheat" in "\n".join(cheat_lines)
    # v5.0（P3 / D250）：游戏工具只在开局时出现
    default_lines = tools.prompt_lines()
    game_only_count = sum(1 for tool in tools.TOOLS if tool.game_only)
    assert len(default_lines) == len(tools.TOOLS) - game_only_count
    for name in ("game_status", "game_inventory", "game_do", "game_say", "game_cancel", "game_plan", "game_mode"):
        assert name not in "\n".join(default_lines)
        assert name in "\n".join(lines)
    # M3 四个记忆工具 + M4 三个 mode 工具（reminder 系列是 M5）
    assert tools.tool_names() == {
        "draw_image",  # v2.2（T2.2-5 / D117）
        "save_memory",
        "update_memory",
        "delete_memory",
        "recall_memory",
        "switch_mode",
        "switch_role",
        "adjust_rp_playstyle",
        "get_time",  # v3.8（D170）
        "add_schedule",
        "list_schedules",
        "update_schedule",
        "remove_schedule",
        "look_image",  # v2（T2.0-6b / D82 第三层）
        "list_images",  # v3.10（P3 / D176：翻相册）
        "web_search",  # v2（T2.0-7 / D92）
        "search_knowledge",  # v2.1（T2.1-3 / D98）
        "delete_path",  # v2.1（T2.1-4 / D99）
        "move_path",  # v2.1（T2.1-4 / D99）
        "write_file",  # v2.1（T2.1-4 / D99）
        "list_dir",  # v2.1（T2.1-4 / D99）
        "read_file",  # v2.1（T2.1-4 / D99）
        "look_screen",  # 2026-09-27（D190）：她自己看一眼屏幕（一轮只看一次）
        "save_thought",  # 2026-09-27（D192）：她给自己留一张小纸条（有寿命的惦记）
        "reflect_speech",  # 2026-10-26（D347）：把说话方式反馈写进长期档案
        "game_status",  # v5.0（P3 / D250）：游戏进行中才出现
        "game_inventory",  # v6.1（D328）：完整背包事实
        "game_inspect",  # v5.5（D318）：精确查看局部地形
        "game_survey",  # v7.8（D332）：大范围紧凑地形图
        "game_do",
        "game_say",
        "game_cancel",
        "game_plan",
        "game_mode",
        "game_cheat",  # v5.1（P2 / D290）：只有用户开启作弊后才出现
        "game_skill",  # v5.1（P4 / D288）：执行持久化技能
        "game_build",  # v5.1（P5 / D289）：蓝图 / 自由创作
        "game_knowledge",  # v5.1（P6 / D288）：minecraft-data 事实
        "game_plugin",  # v5.1（P7 / D288）：模组插件桥
    }


def test_prompt_lines_are_grouped_by_mode():
    """T2.0-8：工具按 mode 分组 —— 入戏不给搜网（一搜就出戏），工具模式才全给。"""
    normal = "\n".join(tools.prompt_lines("normal"))
    roleplay = "\n".join(tools.prompt_lines("roleplay"))
    tool_mode = "\n".join(
        tools.prompt_lines("tool", game_active=True, cheat_active=True)
    )

    assert "save_memory" in normal and "add_schedule" in normal
    assert "web_search" in normal and "look_image" in normal

    assert "web_search" not in roleplay, "入戏模式不该看见搜网"
    assert "search_knowledge" not in roleplay, "入戏模式也不给资料检索（会往戏里灌内容）"
    assert "save_memory" in roleplay and "look_image" in roleplay
    # v2.1（D99）：文件工具同样不给入戏（既出戏、又危险）
    for name in ("read_file", "list_dir", "write_file", "delete_path", "move_path"):
        assert name not in roleplay, f"入戏模式不该看见 {name}"

    # 工具模式 = 全给（一个都不少）
    for name in tools.tool_names():
        assert name in tool_mode, f"{name} 应该在工具模式里"
    assert len(
        tools.prompt_lines("tool", game_active=True, cheat_active=True)
    ) == len(tools.TOOLS)
    # 不传 mode = 列全部（兼容老调用点）
    assert tools.prompt_lines() == tools.prompt_lines("tool")


# ---------- 提醒工具（M5 / A1）----------


def test_get_time_returns_a_usable_now(friend_id):
    """v3.8（D170）：`get_time` 给她一个"明确查一次现在几点"的入口。

    system prompt 的 `# 现在` 里本来就有时间，但模型经常不当回事（用户实测："问她几点了不知道"）。
    """
    from datetime import datetime

    with db.session_scope() as s:
        record = tools.execute(s, friend_id, call("get_time"))

    assert record["ok"] is True, record
    result = record["result"]
    assert result["今日"] == f"{datetime.now():%Y-%m-%d}"
    assert len(result["现在"]) == 5 and result["现在"][2] == ":"  # HH:MM
    assert result["星期"].startswith("周")
    assert result["UTC"]  # 也给她 UTC（跨时区/推算用）


def test_get_time_is_visible_in_every_mode(friend_id):
    """时间不像搜网那样会出戏 —— 三种 mode 的清单里都该有它。"""
    for mode in ("normal", "roleplay", "tool"):
        assert "get_time" in "\n".join(tools.prompt_lines(mode))


# ---------- v3.8（D169）：原生 function calling 的 schema ----------


def test_native_tools_cover_every_tool_and_are_well_formed():
    """每个工具都要有原生 schema（有它模型才能原生调；缺了就只能手写 <tool> 块）。"""
    native = tools.native_tools(game_active=True, cheat_active=True)
    assert {item["function"]["name"] for item in native} == tools.tool_names()
    assert len(native) == len(tools.TOOLS)
    # 默认（不在游戏里）不含 game 工具
    default_names = {item["function"]["name"] for item in tools.native_tools()}
    assert "game_do" not in default_names

    for item in native:
        assert item["type"] == "function"
        fn = item["function"]
        assert fn["description"].strip(), f"{fn['name']} 没写说明"
        schema = fn["parameters"]
        assert schema["type"] == "object"
        props = schema.get("properties", {})
        for required in schema.get("required", []):
            assert required in props, f"{fn['name']} 的 required 里有不存在的参数：{required}"
        # 参数名要跟 handler 真读的对得上（抽查最常见的几个）
        if fn["name"] == "save_memory":
            assert set(props) >= {"content", "tag", "layer", "importance", "context"}
        if fn["name"] == "add_schedule":
            assert set(props) >= {"title", "date", "time", "nature", "owner"}


def test_native_tools_respect_mode_grouping():
    """原生清单跟提示词清单**同一套分组**（入戏不给搜网/文件工具）。"""
    roleplay = {item["function"]["name"] for item in tools.native_tools("roleplay")}
    normal = {item["function"]["name"] for item in tools.native_tools("normal")}
    tool_mode = {
        item["function"]["name"]
        for item in tools.native_tools("tool", game_active=True, cheat_active=True)
    }

    assert "web_search" not in roleplay and "read_file" not in roleplay
    assert "save_memory" in roleplay and "get_time" in roleplay
    assert "web_search" in normal
    assert tool_mode == tools.tool_names()
    no_cheat = {
        item["function"]["name"]
        for item in tools.native_tools("tool", game_active=True)
    }
    assert "game_cheat" not in no_cheat
    assert "game_skill" in no_cheat
    assert "game_build" in no_cheat
    assert "game_knowledge" in no_cheat
    assert "game_plugin" in no_cheat
    # 没有游戏时，哪一档都不该出现 game_*
    for names in (roleplay, normal, {item["function"]["name"] for item in tools.native_tools("tool")}):
        assert "game_do" not in names


def test_add_schedule_accepts_relative_date(friend_id):
    """让模型算日期容易错 —— 所以 10m / 2h 这种相对写法必须收（算成本地 date+time）。"""
    with db.session_scope() as s:
        record = tools.execute(s, friend_id, call("add_schedule", title="喝水", date="10m"))
        assert record["ok"] is True, record
        assert record["result"]["title"] == "喝水"
        from datetime import datetime, timedelta

        when = datetime.fromisoformat(f"{record['result']['date']}T{record['result']['time']}")
        delta = when - datetime.now()
        assert timedelta(minutes=9) < delta < timedelta(minutes=11)


def test_add_schedule_accepts_absolute_and_rejects_garbage(friend_id):
    with db.session_scope() as s:
        ok = tools.execute(
            s, friend_id, call("add_schedule", title="a", date="2026-09-20", time="09:00")
        )
        assert ok["ok"] and ok["result"]["date"] == "2026-09-20"
        assert ok["result"]["time"] == "09:00"
        bad = tools.execute(s, friend_id, call("add_schedule", title="c", date="下周三"))
        assert bad["ok"] is False and "看不懂这个日期" in bad["error"]
        # 没 title 也不行
        assert not tools.execute(s, friend_id, call("add_schedule", date="2026-09-24"))["ok"]


def test_schedule_remove_and_settle_by_title(friend_id):
    """模型平时拿不到 id —— 所以按标题也能改/能删，不然这俩工具没法用。"""
    with db.session_scope() as s:
        tools.execute(s, friend_id, call("add_schedule", title="交房租", date="2026-09-30"))
        tools.execute(s, friend_id, call("add_schedule", title="喝水", date="2026-09-24", time="15:00"))

        settled = tools.execute(
            s, friend_id, call("update_schedule", title="喝水", state="settled", reason="done")
        )
        assert settled["ok"] is True and settled["result"]["state"] == "settled"

        removed = tools.execute(s, friend_id, call("remove_schedule", title="房租"))
        assert removed["ok"] is True and removed["result"]["removed"] is True

        assert tools.execute(s, friend_id, call("remove_schedule", title="不存在的事"))["ok"] is False
        rows = schedule_service.list_for(s, friend_id=friend_id)
        assert [r.title for r in rows] == ["喝水"] and rows[0].state == "settled"


def test_ambiguous_schedule_is_refused(friend_id):
    with db.session_scope() as s:
        tools.execute(s, friend_id, call("add_schedule", title="喝水", date="2026-09-24"))
        tools.execute(s, friend_id, call("add_schedule", title="喝水吃药", date="2026-09-25"))
        record = tools.execute(s, friend_id, call("update_schedule", title="喝水", state="settled"))
        assert record["ok"] is False and "说不清是哪条" in record["error"]


def test_update_schedule_title_is_lookup_not_rename(friend_id):
    """v4.1（D246）：`title` 只用来找那条，**不改标题** —— 改名走 `new_title`。

    （实测她 `update_schedule(title="喝水", time=…)` 把「提醒梦汐喝水」改成了「喝水」。）
    """
    with db.session_scope() as s:
        tools.execute(s, friend_id, call("add_schedule", title="提醒梦汐喝水", date="2026-10-09", time="11:20"))
        moved = tools.execute(s, friend_id, call("update_schedule", title="喝水", time="11:22"))
        assert moved["ok"] is True
        assert moved["result"]["title"] == "提醒梦汐喝水"  # 没被改名
        assert moved["result"]["time"] == "11:22"

        renamed = tools.execute(
            s, friend_id, call("update_schedule", title="喝水", new_title="提醒汐音喝水")
        )
        assert renamed["ok"] is True and renamed["result"]["title"] == "提醒汐音喝水"


# ---------- M4：入戏 / 换角色 / 调演法 ----------


def test_switch_mode_writes_back_to_friend(friend_id):
    """模型自主入戏 → **后端状态真的变了**（不然下一轮 prompt 还是旧的）。"""
    with db.session_scope() as s:
        record = tools.execute(s, friend_id, call("switch_mode", mode="roleplay"))
        assert record["ok"] is True, record
        assert record["result"] == {"mode": "roleplay", "role": None}
    with db.session_scope() as s:
        assert s.get(Friend, friend_id).current_mode == "roleplay"


def test_switch_mode_accepts_chinese_and_rejects_junk(friend_id):
    with db.session_scope() as s:
        assert tools.execute(s, friend_id, call("switch_mode", mode="普通"))["result"]["mode"] == "normal"
        assert tools.execute(s, friend_id, call("switch_mode", mode="入戏"))["result"]["mode"] == "roleplay"
        bad = tools.execute(s, friend_id, call("switch_mode", mode="随便"))
        assert bad["ok"] is False and "不认识的模式" in bad["error"]


def test_switch_role_implies_roleplay(friend_id):
    with db.session_scope() as s:
        record = tools.execute(s, friend_id, call("switch_role", role="女朋友"))
        assert record["result"] == {"mode": "roleplay", "role": "女朋友"}
        assert tools.execute(s, friend_id, call("switch_role", role=""))["ok"] is False


def test_leaving_roleplay_clears_the_role(friend_id):
    with db.session_scope() as s:
        tools.execute(s, friend_id, call("switch_role", role="女朋友"))
        assert tools.execute(s, friend_id, call("switch_mode", mode="normal"))["result"]["role"] is None


def test_adjust_rp_playstyle_creates_then_updates(friend_id):
    """同角色**覆盖更新**（DB 上有唯一索引，直接插第二条会撞）。"""
    with db.session_scope() as s:
        assert tools.execute(s, friend_id, call("adjust_rp_playstyle", text="温柔一点"))["ok"] is False
        tools.execute(s, friend_id, call("switch_role", role="女朋友"))
        first = tools.execute(s, friend_id, call("adjust_rp_playstyle", text="温柔一点，会撒娇"))
        assert first["ok"] is True and first["result"]["updated"] is False
        second = tools.execute(s, friend_id, call("adjust_rp_playstyle", text="再冷淡一点，话更少"))
        assert second["result"]["updated"] is True
        assert first["result"]["play_style_id"] == second["result"]["play_style_id"]

    with db.session_scope() as s:
        styles = [m for m in memory_service.list_alive(s, friend_id) if m.tag == "rp_play_style"]
        assert len(styles) == 1
        assert styles[0].content == "再冷淡一点，话更少"
        assert styles[0].layer == "long_term" and styles[0].context == "女朋友"


def test_each_role_keeps_its_own_play_style(friend_id):
    with db.session_scope() as s:
        tools.execute(s, friend_id, call("switch_role", role="女朋友"))
        tools.execute(s, friend_id, call("adjust_rp_playstyle", text="甜"))
        tools.execute(s, friend_id, call("switch_role", role="仇人"))
        tools.execute(s, friend_id, call("adjust_rp_playstyle", text="冷"))
        styles = {m.context: m.content for m in memory_service.list_alive(s, friend_id) if m.tag == "rp_play_style"}
        assert styles == {"女朋友": "甜", "仇人": "冷"}


# ---------- 正常路径 ----------


def test_save_memory_survives_messy_model_output(friend_id):
    """实测模型会把 importance 写成 high、tag 写成"生日"这种自由词 —— 不许因此失败。"""
    with db.session_scope() as s:
        record = tools.execute(
            s,
            friend_id,
            call(
                "save_memory",
                content="用户的生日是5月20号",
                tag="生日",  # 非法 tag
                layer="permanent",
                importance="high",  # 不是数字
            ),
        )
        assert record["ok"] is True, record
        assert record["result"]["layer"] == "permanent"
        assert record["result"]["merged"] is False
        assert record["duration_ms"] >= 0

    with db.session_scope() as s:
        stored = memory_service.list_alive(s, friend_id)
        assert stored[0].tag == "normal", "非法 tag 应被兜成 normal"
        assert stored[0].importance == 0.9, "high 应被折算成 0.9"


def test_save_memory_default_layer_is_long_term(friend_id):
    with db.session_scope() as s:
        record = tools.execute(s, friend_id, call("save_memory", content="顺手记的"))
        assert record["result"]["layer"] == "long_term"


def test_second_save_merges(friend_id):
    with db.session_scope() as s:
        first = tools.execute(s, friend_id, call("save_memory", content="用户不吃香菜"))
        second = tools.execute(s, friend_id, call("save_memory", content="用户不吃香菜"))
        assert first["result"]["merged"] is False
        assert second["result"]["merged"] is True
        assert first["result"]["id"] == second["result"]["id"]


def test_recall_returns_ids_so_update_and_delete_work(friend_id):
    with db.session_scope() as s:
        saved = tools.execute(
            s, friend_id, call("save_memory", content="用户的猫叫团子", layer="pinned")
        )
        memory_id = saved["result"]["id"]

        recalled = tools.execute(s, friend_id, call("recall_memory", query="团子"))
        assert recalled["ok"] is True
        assert recalled["result"]["memories"][0]["id"] == memory_id

        updated = tools.execute(
            s, friend_id, call("update_memory", id=memory_id, content="用户的猫叫团子（橘猫）")
        )
        assert updated["ok"] is True
        assert updated["result"]["content"] == "用户的猫叫团子（橘猫）"

        deleted = tools.execute(s, friend_id, call("delete_memory", id=memory_id))
        assert deleted["ok"] is True


# ---------- 失败路径：一律返回结果，不抛异常 ----------


def test_unknown_tool_is_reported_not_raised(friend_id):
    with db.session_scope() as s:
        # 用一个真不存在的名字
        record = tools.execute(s, friend_id, call("no_such_tool_xyz", path="x"))
        assert record["ok"] is False
        assert "没有这个工具" in record["error"]


def test_missing_required_arguments(friend_id):
    with db.session_scope() as s:
        assert tools.execute(s, friend_id, call("save_memory", content="   "))["ok"] is False
        assert tools.execute(s, friend_id, call("update_memory"))["ok"] is False
        assert tools.execute(s, friend_id, call("delete_memory"))["ok"] is False
        assert tools.execute(s, friend_id, call("recall_memory"))["ok"] is False


def test_agent_can_change_layer_and_delete_via_tools(friend_id):
    """D230：朋友用工具能改层（升 / 降级）、能删任意 layer（软删）。"""
    with db.session_scope() as s:
        memory, _ = memory_service.save(
            s, Actor.USER, friend_id, content="这条其实只是这阵子的事", layer="pinned"
        )
        memory_id = memory.id

    with db.session_scope() as s:
        record = tools.execute(
            s, friend_id, call("update_memory", id=memory_id, layer="long_term")
        )
        assert record["ok"] is True and record["result"]["layer"] == "long_term"
    # 给错层必须报错（不能静默兜底成 long_term，否则等于偷偷换了用户的记忆）
    with db.session_scope() as s:
        record = tools.execute(s, friend_id, call("update_memory", id=memory_id, layer="瞎写"))
        assert record["ok"] is False
    with db.session_scope() as s:
        record = tools.execute(s, friend_id, call("delete_memory", id=memory_id))
        assert record["ok"] is True
        assert memory_service.list_alive(s, friend_id) == []


def test_recall_of_missing_memory_returns_ok_but_empty(friend_id):
    with db.session_scope() as s:
        record = tools.execute(s, friend_id, call("recall_memory", query="不存在的词"))
        assert record["ok"] is True
        assert record["result"]["count"] == 0


# ---------- 回灌文本（LLM协议§4.4）----------


def test_format_results_is_model_readable():
    text = tools.format_results(
        [
            {"id": "t1", "name": "save_memory", "ok": True, "result": {"id": "m1", "merged": False}},
            {"id": "t2", "name": "delete_memory", "ok": False, "error": "只能删 pinned"},
        ]
    )
    assert text.startswith("[工具执行结果]")
    assert "- save_memory: " in text
    assert "- delete_memory: " in text
    assert json.loads(text.splitlines()[1].split(": ", 1)[1])["ok"] is True
    assert json.loads(text.splitlines()[2].split(": ", 1)[1])["error"] == "只能删 pinned"

def test_imagegen_build_request_two_roads():
    """D129：不带 ref 走老的 generations；带 ref 走 edits + denoising_strength（默认 0.7）。"""
    from backend.services import imagegen_service as S

    conf = {"model": "nsfw-gen-illustrious", "size": "1024x1024", "strength": None}

    plain_suffix, plain_mode, plain_fields = S.build_request(conf=conf, prompt="1girl")
    assert plain_suffix == "/images/generations"
    assert plain_mode == "json"
    assert plain_fields["n"] == 1
    assert "denoising_strength" not in plain_fields
    assert "masterpiece" in plain_fields["prompt"]  # 官方质量标签照旧带上

    edit_suffix, edit_mode, edit_fields = S.build_request(
        conf=conf, prompt="1girl", ref_png=b"png-bytes"
    )
    assert edit_suffix == "/images/edits"
    assert edit_mode == "multipart"
    assert edit_fields["denoising_strength"] == "0.7"  # 默认 0.7（用户拍的）
    assert edit_fields["n"] == "1"  # 表单字段是字符串


def test_imagegen_strength_is_clamped_and_configurable():
    from backend.services import imagegen_service as S

    assert S._strength_of({"strength": 0.55}) == 0.55  # 用户能自己调
    assert S._strength_of({"strength": 5}) == 1.0
    assert S._strength_of({"strength": 0}) == 0.1
    assert S._strength_of({}) == S.DEFAULT_STRENGTH
    assert S._strength_of({"strength": "abc"}) == S.DEFAULT_STRENGTH


def test_huge_tool_result_is_truncated_before_reaching_the_model():
    """D332：最后一道工具结果体积安全网。"""
    record = {
        "ok": True,
        "result": {"blocks": [{"x": i, "y": 64, "z": 0, "name": "stone"} for i in range(5000)]},
    }
    capped = tools._cap_tool_result(record)
    assert capped["result"]["truncated"] is True
    assert capped["result"]["chars"] > tools.MAX_TOOL_RESULT_CHARS
    assert len(capped["result"]["preview"]) <= 6000
