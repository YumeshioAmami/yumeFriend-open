"""T2.2 验收：上下文组装（LLM协议§2 / prompts.md §1~§6）。

不碰数据库、不发网络请求——纯字符串组装。
"""

import json
from datetime import datetime, timedelta

from backend.agent import context as ctx
from backend.models.friend import Friend
from backend.models.memory import Memory

NOW = datetime(2026, 9, 18, 21, 30)


def _friend(**over) -> Friend:
    data = {
        "id": "f1",
        "name": "阿月",
        "personality": "话不多，喜欢用括号写动作。",
        "custom_instructions": "别用颜文字。",
        "current_mode": "normal",
        "current_role": None,
        "created_at": "2026-09-01T00:00:00",
        "updated_at": "2026-09-01T00:00:00",
    }
    data.update(over)
    return Friend(**data)


def _mem(mid: str, content: str, *, layer="permanent", tag="normal", context=None,
         importance=0.5, created_at="2026-09-01T00:00:00", refreshed_at=None,
         deleted_at=None) -> Memory:
    return Memory(
        id=mid,
        friend_id="f1",
        content=content,
        tag=tag,
        layer=layer,
        context=context,
        importance=importance,
        created_at=created_at,
        refreshed_at=refreshed_at or created_at,
        deleted_at=deleted_at,
    )


# ---------- system prompt：结构 ----------


def test_system_prompt_contains_persona_and_mode():
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert "你是「阿月」。" in prompt
    assert "话不多，喜欢用括号写动作。" in prompt
    assert "别用颜文字。" in prompt
    assert ctx.NORMAL_TEXT in prompt
    assert "当前时间：2026-09-18 21:30（周五）" in prompt


def test_system_prompt_section_order_puts_volatile_part_last():
    """D42：时间/mode 放最后，前面全是稳定前缀（吃输入缓存半价，D25）。

    v3.6（D154）：工具块挪到**记忆之后**（原来紧贴人格，全是"必须/格式"，
    会把语气拽成助手腔）；顺序仍是"稳定的在前、易变的最后"。
    """
    prompt = ctx.build_system_prompt(_friend(), [_mem("m1", "用户是后端开发")], now=NOW)
    i_persona = prompt.index("# 你是谁")
    i_memory = prompt.index(ctx.MEMORY_HEADER)
    i_tools = prompt.index("# 后台能力")
    i_now = prompt.index("# 现在")
    assert i_persona < i_memory < i_tools < i_now
    assert prompt.rstrip().endswith(ctx.NORMAL_TEXT)


def test_schedule_block_is_injected_when_present():
    """v4.1（D246）：她名下的日程要摆进 system prompt（不然她想不起来去用工具）。"""
    prompt = ctx.build_system_prompt(
        _friend(),
        [],
        now=NOW,
        schedule_note="- id=x ｜ 2026-10-09 11:22 ｜ 提醒他喝水 ｜ 他的事 ｜ 待响",
    )
    assert ctx.SCHEDULE_HEADER in prompt
    assert "id=x" in prompt
    assert "别只在嘴上答应" in prompt


def test_schedule_block_is_absent_without_schedules():
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert ctx.SCHEDULE_HEADER not in prompt


def test_system_prompt_has_no_safety_guidance():
    """D1：系统层零内容约束——不许注入任何红线 / 安全提示。"""
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    for banned in ("安全", "红线", "违反", "法律法规", "safety", "policy", "拒绝"):
        assert banned not in prompt, f"system prompt 里不该出现「{banned}」"


def test_style_block_offers_human_moves_without_checklist():
    """D313：给可能的人类反应，不规定每轮必须凑观点/下文。"""
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert "这些只是**可能发生的情况**" in prompt
    assert "不是每轮都要做的检查表" in prompt
    assert "只接一句" in prompt and "想问就问" in prompt
    assert "每轮至少" not in prompt
    assert "多数闲聊留一个自然的下文" not in prompt
    assert "情绪价值 / 底层逻辑 / 边界 / 承接 / 锚点 / 闭环" in prompt


def test_tool_block_is_hard_rule_and_lists_tools():
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert "【硬性规则】" in prompt  # prompts.md §6.1 实测教训：软语气会被 RP 人格压住
    assert "<tool>" in prompt
    # 清单由 tools manifest 生成（T3.1）——prompt 里的工具必须和真能跑的一致
    from backend.agent import tools as tools_module

    for line in tools_module.prompt_lines():
        assert line in prompt
    assert "save_memory" in prompt
    assert "recall_memory" in prompt
    # manifest 里有的，prompt 必须跟着有（M4 三个 mode 工具 + M5 三个提醒工具）
    assert "switch_mode" in prompt and "adjust_rp_playstyle" in prompt
    assert "add_schedule" in prompt
    # T2.0-6b/7 已经落地的两个工具
    assert "look_image" in prompt and "web_search" in prompt
    # T2.1：知识库 + 文件工具
    assert "search_knowledge" in prompt
    assert "read_file" in prompt and "write_file" in prompt
    # 反过来：prompt 里**只许出现 manifest 里真有的工具**（说了做不到最误导）。
    # （以前这里写死某个"还没做的名字"，每落地一个新工具就得改一次 —— 改成通用检查）
    import re

    named = set(re.findall(r"^- ([a-z_]+)\(", prompt, re.MULTILINE))
    assert named <= tools_module.tool_names(), f"prompt 里出现了没实现的工具：{named - tools_module.tool_names()}"


def test_layer_rules_are_in_the_prompt_and_are_decidable():
    """选层规则必须写清楚：四层都有明确条件 + 例子（用户担心模型乱分）。"""
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert "怎么选 layer" in prompt
    # 四层都要出现，而且各自有条件
    for layer in ("pinned", "permanent", "long_term", "short_term"):
        assert f"`{layer}`" in prompt
    # 关键的是非题：过一年还成立吗 → permanent（这是 permanent 唯一的入口）
    assert "过一年也依然成立" in prompt
    assert "只是这阵子的状态" in prompt
    # 带例子，模型才不会自由发挥
    assert "用户不吃香菜" in prompt and "用户最近在学 Rust" in prompt


def test_extraction_prompt_uses_the_same_layer_rules():
    """两条写记忆的路径（聊天 / 补漏）必须用同一份规则，不然会漂移。"""
    from backend.agent.layer_rules import LAYER_DECISION_RULES
    from backend.services import catchup_service

    assert LAYER_DECISION_RULES in ctx.HARD_RULE_BLOCK
    assert LAYER_DECISION_RULES in catchup_service.EXTRACTION_SYSTEM


def test_tool_lines_can_be_overridden():
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW, tool_lines=["- 只有这一个"])
    assert "- 只有这一个" in prompt
    assert "- recall_memory" not in prompt, "覆盖后不该再出现 manifest 里的清单"


def test_no_tools_renders_placeholder():
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW, tool_lines=[])
    assert ctx.NO_TOOLS_LINE in prompt


def test_final_identity_does_not_train_blanket_refusal():
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert "不是通用助手" in prompt
    assert "不是客服" in prompt
    assert "什么都能配合" not in prompt
    assert "自己的性格、判断和选择" in prompt


# ---------- mode ----------


def test_roleplay_mode_injects_role_and_play_style():
    friend = _friend(current_mode="roleplay", current_role="女朋友")
    memories = [
        _mem("p1", "讲话要软一点，会撒娇", layer="pinned", tag="rp_play_style", context="女朋友"),
        _mem("p2", "另一个角色的演法", layer="pinned", tag="rp_play_style", context="仇人"),
    ]
    prompt = ctx.build_system_prompt(friend, memories, now=NOW)
    assert "你在扮演「女朋友」" in prompt
    assert "讲话要软一点，会撒娇" in prompt
    assert "另一个角色的演法" not in prompt  # 别的角色的演法不能串进来


def test_roleplay_without_play_style_has_fallback():
    friend = _friend(current_mode="roleplay", current_role="老板娘")
    prompt = ctx.build_system_prompt(friend, [], now=NOW)
    assert "老板娘" in prompt
    assert ctx.PLAY_STYLE_MISSING in prompt


def test_unknown_mode_falls_back_to_normal():
    friend = _friend(current_mode="???")
    assert ctx.render_mode_text(friend) == ctx.NORMAL_TEXT


def test_mode_texts_carry_chinese_labels():
    """模式名必须中英都写：不然问"你什么模式"，它会回英文或把模式名和角色名混起来。"""
    assert "NORMAL（普通）" in ctx.NORMAL_TEXT
    assert "ROLEPLAY（入戏）" in ctx.ROLEPLAY_TEXT
    assert "TOOL（工具）" in ctx.TOOL_TEXT
    # NORMAL / TOOL 要能如实回答"现在是什么模式"
    assert '直接答"普通"' in ctx.NORMAL_TEXT
    assert '直接答"工具"' in ctx.TOOL_TEXT
    # ROLEPLAY 不写"直接答'入戏'"这种硬口径，而是留了一个**唯一的例外**（D51）：
    # 用户直接问模式才如实答一句，然后立刻回到角色里
    assert "唯一的例外" in ctx.ROLEPLAY_TEXT
    assert "立刻回到角色里" in ctx.ROLEPLAY_TEXT
    assert '直接答"入戏"' not in ctx.ROLEPLAY_TEXT


# ---------- 切 mode 立刻生效（D49）----------


def test_mode_switch_note_is_injected_only_when_mode_changed():
    friend = _friend(current_mode="normal")
    switched = ctx.build_system_prompt(friend, [], now=NOW, previous_mode="roleplay")
    assert "用户刚刚把你切到了 普通 模式（上一轮是 入戏）" in switched
    assert "现在就出戏" in switched

    same = ctx.build_system_prompt(friend, [], now=NOW, previous_mode="normal")
    assert "刚刚把你切到" not in same, "没切模式就不要乱说"

    first_turn = ctx.build_system_prompt(friend, [], now=NOW, previous_mode=None)
    assert "刚刚把你切到" not in first_turn, "第一轮没有上一轮，不该有切换提示"


def test_mode_switch_note_for_roleplay_says_no_confirmation():
    friend = _friend(current_mode="roleplay", current_role="女朋友")
    prompt = ctx.build_system_prompt(friend, [], now=NOW, previous_mode="normal")
    assert "不用问、不用确认" in prompt
    # 切换提示排在 mode 文案前面，让模型先看到这条
    assert prompt.index("刚刚把你切到") < prompt.index(ctx.ROLEPLAY_TEXT[:20])


def test_build_messages_passes_previous_mode():
    messages = ctx.build_messages(
        _friend(), user_input="在吗", now=NOW, previous_mode="roleplay"
    )
    assert "刚刚把你切到" in messages[0]["content"]


def test_mode_state_note_sits_right_before_user_input():
    """D50：状态提示必须是**倒数第二条**（贴着本轮输入），角色为 user。"""
    messages = ctx.build_messages(
        _friend(current_mode="tool"),
        history=[{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "普通"}],
        user_input="现在呢？",
        now=NOW,
        previous_mode="normal",
    )
    assert messages[-1] == {"role": "user", "content": "现在呢？"}
    note = messages[-2]
    assert note["role"] == "user"
    assert "你现在的模式是 工具（TOOL）" in note["content"]
    assert "刚刚把你从 普通 切过来了" in note["content"]


def test_mode_state_note_is_present_every_turn_even_without_switch():
    """同一模式连问也必须每轮都贴（D50 的 F 组实验：只在切换时贴会失效）。"""
    messages = ctx.build_messages(
        _friend(current_mode="tool"),
        history=[{"role": "assistant", "content": "工具"}],
        user_input="现在呢？",
        now=NOW,
        previous_mode="tool",
    )
    note = messages[-2]
    assert note["role"] == "user"
    assert "你现在的模式是 工具（TOOL）" in note["content"]
    assert "刚刚把你从" not in note["content"], "没切模式就别提切换"


def test_first_turn_also_gets_the_note():
    messages = ctx.build_messages(_friend(), user_input="在吗", now=NOW, previous_mode=None)
    assert len(messages) == 3
    assert "你现在的模式是 普通（NORMAL）" in messages[-2]["content"]


# ---------- v3.7（D168）：情绪标签要在"历史之后"再提醒一次 ----------


def test_mood_reminder_sits_after_history_and_before_this_turn():
    """D168：有立绘的朋友 → 历史之后、本轮输入之前补一条情绪标签的格式提醒。

    为什么在这个位置：标签入库前就被摘掉 → 历史里从来没有标签样本 → 模型照着自己
    "从来不写标签"的旧回复学。实测：不带历史 2/2 有标签、带 40 条真历史 0/3；
    把规矩往 system 末尾挪/加说明/加例子全 0/3 —— 只有贴着生成点再提醒一次才管用。
    """
    messages = ctx.build_messages(
        _friend(),
        history=[{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在的。"}],
        user_input="今天好累",
        now=NOW,
        portrait_emotions=["正常", "高兴"],
    )
    assert messages[-1] == {"role": "user", "content": "今天好累"}
    assert messages[-2]["role"] == "user", "D50 的状态提示仍然紧贴本轮输入"
    reminder = messages[-3]
    assert reminder["role"] == "system"
    assert ctx.MOOD_REMINDER in reminder["content"]
    # 提醒在历史**之后**（历史那两条原样排在它前面）
    assert messages[1] == {"role": "user", "content": "在吗"}
    assert messages[2] == {"role": "assistant", "content": "在的。"}


def test_reply_shape_note_sits_after_history_and_before_this_turn():
    """D350：回复形态提醒必须贴着生成点，不能被埋在 system prompt 远处。"""
    messages = ctx.build_messages(
        _friend(),
        history=[{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在的。"}],
        user_input="今天好累",
        now=NOW,
        reply_shape_note="【这一轮先按这个形态回】只回 1 个气泡。",
    )
    assert messages[1] == {"role": "user", "content": "在吗"}
    assert messages[2] == {"role": "assistant", "content": "在的。"}
    assert messages[-1] == {"role": "user", "content": "今天好累"}
    assert messages[-2]["role"] == "user"
    assert messages[-3] == {
        "role": "system",
        "content": "【这一轮先按这个形态回】只回 1 个气泡。",
    }


def test_mood_reminder_is_skipped_without_portraits():
    """没立绘的朋友不该被打扰（表情块压根不注入 → 不写标签是正常的）。"""
    messages = ctx.build_messages(_friend(), user_input="在吗", now=NOW)
    assert all(ctx.MOOD_REMINDER not in (m.get("content") or "") for m in messages)

    blank = ctx.build_messages(_friend(), user_input="在吗", now=NOW, portrait_emotions=["", "  "])
    assert all(ctx.MOOD_REMINDER not in (m.get("content") or "") for m in blank), "空白不算立绘"


def test_mood_reminder_keeps_the_existing_tag_formats():
    """提醒里点明的必须是**现成那两种**形态 —— 摘标签的还是原来那套解析器（显示口径不变）。"""
    assert "**高兴**：" in ctx.MOOD_REMINDER
    assert "[立绘:" in ctx.MOOD_REMINDER
    assert "别跟着学" in ctx.MOOD_REMINDER, "要说清历史里为什么没有标签"


def test_roleplay_note_mentions_the_role():
    messages = ctx.build_messages(
        _friend(current_mode="roleplay", current_role="女朋友"),
        user_input="在吗",
        now=NOW,
        previous_mode="normal",
    )
    note = messages[-2]["content"]
    assert "你现在的模式是 入戏（ROLEPLAY）" in note
    assert "你正在扮演「女朋友」" in note
    assert "如实答一句" in note, "D51：用户直接问模式时要能给个准话"


# ---------- 记忆注入 ----------


def test_memory_line_prefix_marks_layer_and_role():
    assert ctx.format_memory_line(_mem("m", "用户生日 5 月 20 日", layer="pinned")) == (
        "- [pinned] 用户生日 5 月 20 日"
    )
    rp = _mem("m", "想一起去看海", tag="rp", context="女朋友", layer="long_term")
    assert ctx.format_memory_line(rp) == "- [rp·女朋友] 想一起去看海"


def test_select_memories_quota_and_ordering():
    memories = (
        [_mem(f"pin{i}", f"p{i}", layer="pinned") for i in range(3)]
        + [_mem(f"per{i}", f"permanent {i}", layer="permanent", importance=i / 100) for i in range(20)]
        + [_mem(f"lt{i}", f"long {i}", layer="long_term", importance=i / 100) for i in range(20)]
        + [_mem("st1", "临时的", layer="short_term")]
    )
    picked = ctx.select_memories(memories, now=NOW)
    layers = [m.layer for m in picked]
    assert layers[:3] == ["pinned"] * 3
    assert layers.count("permanent") == ctx.PERMANENT_TOP_N
    assert layers.count("long_term") == ctx.LONG_TERM_TOP_N
    assert "short_term" not in layers  # D25 没给 short_term 配额（见 Q24）
    # pinned → permanent → long_term 的顺序
    assert layers == sorted(layers, key=["pinned", "permanent", "long_term"].index)
    # 重要度高的排在同层前面
    perms = [m for m in picked if m.layer == "permanent"]
    assert perms[0].content == "permanent 19"


def test_freshness_outranks_stale_with_same_importance():
    old = _mem("old", "很久没提过", importance=0.5, refreshed_at="2026-03-01T00:00:00")
    fresh = _mem("fresh", "刚提过", importance=0.5, refreshed_at="2026-09-18T00:00:00")
    picked = ctx.select_memories([old, fresh], now=NOW)
    assert [m.id for m in picked] == ["fresh", "old"]
    assert ctx.memory_score(fresh, NOW) > ctx.memory_score(old, NOW)


def test_deleted_memories_are_not_injected():
    alive = _mem("a", "活着", layer="pinned")
    dead = _mem("d", "删掉了", layer="pinned", deleted_at="2026-09-10T00:00:00")
    picked = ctx.select_memories([alive, dead], now=NOW)
    assert [m.id for m in picked] == ["a"]


def test_pinned_is_capped_and_budget_is_enforced():
    many = [_mem(f"p{i}", "很长的记忆内容" * 5, layer="pinned") for i in range(60)]
    picked = ctx.select_memories(many, now=NOW)
    assert len(picked) <= ctx.PINNED_LIMIT  # 40 条上限还在（兜底）

    # v3.5（D153）：pinned **不再被 token 预算裁剪**（预算只裁 permanent / long_term）——
    # 所以再小的预算，pinned 那部分也原样全在。
    small = ctx.select_memories(many, now=NOW, budget_tokens=60)
    assert len([m for m in small if m.layer == "pinned"]) == len(
        [m for m in picked if m.layer == "pinned"]
    )

    # 预算对 permanent / long_term 依然有效
    mixed = many[:5] + [_mem("o0", "很长的普通记忆" * 100, layer="long_term")]
    got = ctx.select_memories(mixed, now=NOW, budget_tokens=60)
    assert len([m for m in got if m.layer == "pinned"]) == 5
    assert not any(m.layer == "long_term" for m in got)

    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert ctx.MEMORY_EMPTY in prompt  # 一条记忆都没有时给个占位，不留空段


# ---------- messages 拼装 ----------


def test_build_messages_order_and_history_truncation():
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"历史 {i}"}
        for i in range(30)
    ]
    messages = ctx.build_messages(
        _friend(), history=history, user_input="在吗？", now=NOW, history_limit=5
    )
    assert messages[0]["role"] == "system"
    # system + 5 条历史 + 1 条状态提示 + 本轮输入
    assert len(messages) == 1 + 5 + 1 + 1
    assert [m["content"] for m in messages[1:6]] == [f"历史 {i}" for i in range(25, 30)]
    assert messages[-1] == {"role": "user", "content": "在吗？"}


def test_build_messages_accepts_orm_like_history():
    class Row:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    messages = ctx.build_messages(
        _friend(), history=[Row("user", "早"), Row("assistant", "早呀")],
        user_input="今天好累", now=NOW,
    )
    assert messages[1] == {"role": "user", "content": "早"}
    assert messages[2] == {"role": "assistant", "content": "早呀"}


def test_history_default_limit_is_20():
    # v3.5：20 → 40（用户："上下文窗口提到 2~3 倍"）
    assert ctx.HISTORY_LIMIT_DEFAULT == 40


def test_pinned_memories_are_exempt_from_the_token_budget():
    """D153：pinned 是"最不能忘、每轮都在场"的东西 —— **不受 token 预算裁剪**
    （只受 40 条上限）；预算只裁 permanent / long_term。

    起因：汐音把 LingChat 的 29 条小本本搬成 pinned 后，pinned 合计 11893 字 > 预算 3000，
    结果大部分小本本**根本没进上下文**。
    """
    pinned = [_mem(f"p{i}", "钉住的旧事" * 30, layer="pinned") for i in range(6)]
    ordinary = [_mem("o0", "一条很长的普通长期记忆" * 200, layer="long_term")]
    picked = ctx.select_memories(pinned + ordinary, now=NOW, budget_tokens=100)
    assert sum(1 for m in picked if m.layer == "pinned") == 6, "pinned 必须全在"
    assert not any(m.layer == "long_term" for m in picked), "普通记忆该被预算挡住"


def test_prompt_tells_her_to_answer_instead_of_punting():
    """D235：他正经问事时**先给答案**；不许把问题原样丢回去（姿态归人格，系统只管"必须答"）。"""
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert "先给答案" in prompt
    # 被禁的招牌话术要**点名**写出来（只禁不配替代 = 逼回中立，D235 第 3 条）
    assert "答案在你心里" in prompt
    assert "你自己悟" in prompt


def test_voice_samples_are_injected_and_style_negatives_are_gone():
    """V6.0-2（D298）：学声音样本；不再用成堆“不要”把语气压成模板。"""
    friend = _friend(
        voice_examples=json.dumps(
            ["行，那不排了。你这种人就别上闹钟。", "我没死。你别把话收得那么快。"],
            ensure_ascii=False,
        )
    )
    prompt = ctx.build_system_prompt(friend, [], now=NOW)
    assert ctx.VOICE_SAMPLES_HEADER in prompt
    assert "别上闹钟" in prompt
    assert "别把话收得那么快" in prompt
    assert "绝对不要输出你的思考过程" not in prompt
    assert "先给答案" in prompt
    assert "我是不是在演" in prompt
    assert "关心的话说过一次就够" in prompt
    assert "<<<MSG>>>" in prompt
    assert "样本不是长度上限" in prompt
    assert "对话不是问答反射" in prompt
    assert "最多 3 条" in prompt


def test_more_than_eight_voice_samples_are_kept():
    """V6.0：声音样本要覆盖正面 + 负面状态，上限 16 段。"""
    samples = [f"样本{i}" for i in range(12)]
    friend = _friend(voice_examples=json.dumps(samples, ensure_ascii=False))
    prompt = ctx.build_system_prompt(friend, [], now=NOW)
    for sample in samples:
        assert sample in prompt


def test_prompt_forbids_faking_actions_in_system_layer():
    """D236：**不编事实**收进系统层（所有朋友都吃到，不用每人抄一遍）。"""
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert "不编事实" in prompt
    assert "没做过的不说做过" in prompt
    assert "不假装翻" in prompt


def test_estimate_tokens_is_conservative():
    assert ctx.estimate_tokens("") >= 1
    assert ctx.estimate_tokens("三个字") == 3


def test_memory_score_handles_missing_timestamps():
    m = _mem("x", "没有时间", created_at="not-a-date", refreshed_at="not-a-date")
    assert ctx.memory_score(m, NOW) == m.importance
    assert ctx.memory_score(_mem("y", "y", refreshed_at="2026-09-18T00:00:00"), NOW) <= 1.0


def test_format_now_weekday():
    assert ctx.format_now(datetime(2026, 9, 18, 9, 5)) == "当前时间：2026-09-18 09:05（周五）"
    assert ctx.format_now(datetime(2026, 9, 20, 0, 0)) == "当前时间：2026-09-20 00:00（周日）"


# ---------- v3.44（D229）：pinned 安全阀 + 本轮关键词命中自动注入 ----------


def test_focus_keywords_extracts_latin_and_cjk_ngrams():
    words = ctx.focus_keywords("我前天问你 408 的事，王道教材买哪本？")
    assert "408" in words  # 拉丁/数字串整串保留（治"408 这种短词从缝里漏"）
    assert "王道教" in words  # 中文 3-gram
    assert "王道" not in words  # 2-gram 噪声太大，已停用
    assert not any(c in words for c in ("，", "？", " "))
    # 超长输入封顶，别让一轮消息拖慢组装
    assert len(ctx.focus_keywords("长" * 500)) <= ctx.KEYWORD_MAX_CANDIDATES


def test_pinned_limit_is_a_safety_valve_not_a_quota():
    # 汐音实测 42 条 pinned 被旧上限 40 静默砍了 2 条（D228）→ 现在必须全在
    pinned = [_mem(f"p{i}", f"钉住的事 {i}", layer="pinned") for i in range(42)]
    picked = ctx.select_memories(pinned, now=NOW)
    assert len(picked) == 42
    assert ctx.PINNED_LIMIT >= 200


def test_keyword_hit_surfaces_memory_that_top_n_would_miss():
    # 一条 importance 极低的 long_term：固定配额（top 8）永远轮不到它
    buried = _mem("buried", "梦汐在学 408，参考书是王道", layer="long_term", importance=0.05)
    noisy = [
        _mem(f"n{i}", f"无关的琐事 {i}", layer="long_term", importance=0.9) for i in range(30)
    ]
    memories = [buried, *noisy]
    without = ctx.select_memories(memories, now=NOW)
    assert "buried" not in [m.id for m in without]

    with_focus = ctx.select_memories(
        memories, now=NOW, focus_text="我前天问你 408 的事还记得吗"
    )
    assert "buried" in [m.id for m in with_focus]


def test_keyword_hit_needs_a_specific_word_not_just_a_common_one():
    # 30 条都含"问题"（占全部 > 三成 → 不算具体词）：只说"问题"不许把它们当命中灌进来
    common = [
        _mem(f"c{i}", "用户在纠结一个问题", layer="long_term", importance=0.5) for i in range(30)
    ]
    without = ctx.select_memories(common, now=NOW)
    with_focus = ctx.select_memories(common, now=NOW, focus_text="我有个问题想问你")
    assert {m.id for m in without} == {m.id for m in with_focus}  # 关键词没加进任何东西


def test_keyword_hit_skips_short_term_and_play_style():
    st = _mem("st", "临时提到 408", layer="short_term")
    ps = _mem("ps", "演法里有 408", layer="pinned", tag="rp_play_style", context="女友")
    picked = ctx.select_memories([st, ps], now=NOW, focus_text="408 怎么样了")
    assert [m.id for m in picked] == []


def test_keyword_hit_not_duplicated_with_base_selection():
    pinned = _mem("pin1", "钉住的 408 记忆", layer="pinned")
    picked = ctx.select_memories([pinned], now=NOW, focus_text="408 怎么样了")
    assert [m.id for m in picked] == ["pin1"]  # 只出现一次


def test_build_messages_passes_focus_text_for_keyword_recall():
    buried = _mem("buried", "梦汐在学 408", layer="long_term", importance=0.01)
    noisy = [_mem(f"n{i}", f"别的 {i}", layer="long_term", importance=0.9) for i in range(30)]
    messages = ctx.build_messages(
        _friend(),
        history=[],
        user_input="408 学得怎么样了",
        memories=[buried, *noisy],
        now=NOW,
        focus_text="408 学得怎么样了",
    )
    assert "梦汐在学 408" in messages[0]["content"]
    # 不传 focus_text 时保持老行为（不靠关键词）
    plain = ctx.build_messages(
        _friend(), history=[], user_input="408 学得怎么样了", memories=[buried, *noisy], now=NOW
    )
    assert "梦汐在学 408" not in plain[0]["content"]
    assert ctx.format_now(datetime(2026, 9, 18) + timedelta(days=1)).endswith("（周六）")

def test_assistant_images_are_never_inlined(monkeypatch):
    """D149：DeepSeek 对 assistant 带图直接 400（"Image in assistant message is not supported"）
    —— 她那条消息上的图（她画过的）一律降级成 `[图片#N]` 占位，想看就调 look_image（D82 第三层）。
    """
    from backend.services import image_service

    monkeypatch.setattr(image_service, "data_url", lambda path: "data:image/png;base64,AAAA")
    history = [
        {
            "role": "user",
            "content": "看这张",
            "image_path": "images/a.png",
            "image_no": 1,
            "created_at": "2026-09-19T10:00:00",
        },
        {
            "role": "assistant",
            "content": "好看",
            "image_path": "images/b.png",
            "image_no": 2,
            "created_at": "2026-09-19T10:01:00",
        },
    ]
    messages = ctx.build_messages(_friend(), history=history, user_input="在吗？", now=NOW)

    # 「我」发的图照旧真附上（user 角色允许）
    user_image_message = next(
        m for m in messages if m["role"] == "user" and isinstance(m["content"], list)
    )
    assert any(part.get("type") == "image_url" for part in user_image_message["content"])

    # 她那条：**必须**是纯文本（带编号占位），绝不能有 image_url 部件
    assistant_message = messages[2]
    assert assistant_message["role"] == "assistant"
    assert isinstance(assistant_message["content"], str), "assistant 里不许出现 image_url（DeepSeek 400）"
    assert "[图片#2" in assistant_message["content"]
    assert "好看" in assistant_message["content"]


def test_assistant_images_stay_plain_even_without_vision(monkeypatch):
    """看不了图的模型也一样：她那条只留占位，连"看不到图"的转述都不用加。"""
    from backend.services import image_service

    monkeypatch.setattr(image_service, "data_url", lambda path: "data:image/png;base64,AAAA")
    history = [
        {
            "role": "assistant",
            "content": "画好了",
            "image_path": "images/b.png",
            "image_no": 7,
            "created_at": "2026-09-19T10:01:00",
        }
    ]
    messages = ctx.build_messages(
        _friend(), history=history, user_input="在吗？", now=NOW, inline_images=False
    )
    body = messages[2]["content"] if messages[2]["role"] == "assistant" else messages[1]["content"]
    assert isinstance(body, str)
    assert "[图片#7" in body and "画好了" in body


# ---------- v3.31（D216）：历史带时间戳 + 每轮硬注入"现在" ----------


def test_history_messages_get_a_local_timestamp():
    """每条历史（她和用户都算）正文前拼本地时间戳 —— 朋友才算得清"多久以前说的"。"""
    from backend.timefmt import local_stamp_full

    history = [
        {"role": "user", "content": "早", "created_at": "2026-09-18T13:00:00+00:00"},
        {"role": "assistant", "content": "早呀", "created_at": "2026-09-18T13:01:00+00:00"},
    ]
    messages = ctx.build_messages(_friend(), history=history, user_input="在吗", now=NOW)
    assert messages[1]["content"] == (
        f"[{local_stamp_full('2026-09-18T13:00:00+00:00', NOW)}] 早"
    )
    assert messages[2]["content"] == (
        f"[{local_stamp_full('2026-09-18T13:01:00+00:00', NOW)}] 早呀"
    )


def test_history_timestamp_shows_the_year_only_across_years():
    """当年只写 `MM-DD`（省 token）；跨年必须带年份（否则 `09-28` 有歧义）。"""
    from backend.timefmt import local_stamp_full

    same_year = local_stamp_full("2026-07-01T12:00:00+00:00", NOW)
    other_year = local_stamp_full("2025-07-01T12:00:00+00:00", NOW)
    assert not same_year.startswith("2026-"), same_year
    assert other_year.startswith("2025-"), other_year


def test_history_without_a_timestamp_is_not_stamped():
    """没有 `created_at` 的裸历史（测试里的 dict）不拼 —— 免得拼出 `[?]`。"""
    messages = ctx.build_messages(
        _friend(), history=[{"role": "user", "content": "早"}], user_input="在吗", now=NOW
    )
    assert messages[1] == {"role": "user", "content": "早"}


def test_image_history_is_not_double_stamped(monkeypatch):
    """带图的消息：占位符 `[图片#N · 时间]` 里已经有时间了，不能再拼一个（否则两个方括号）。"""
    from backend.services import image_service

    monkeypatch.setattr(image_service, "data_url", lambda path: None)
    history = [
        {
            "role": "assistant",
            "content": "画好了",
            "image_path": "images/b.png",
            "image_no": 7,
            "created_at": "2026-09-19T10:01:00+00:00",
        }
    ]
    messages = ctx.build_messages(_friend(), history=history, user_input="在吗", now=NOW)
    body = messages[1]["content"]
    assert body.startswith("[图片#7")
    assert body.count("[") == 1  # 只该有图片占位那一个方括号


def test_now_rule_is_injected_and_forbids_timestamps():
    """system 的 `# 现在` 里写明"时间戳是真实发出时间"+"别在回复里写"。"""
    prompt = ctx.build_system_prompt(_friend(), [], now=NOW)
    assert ctx.NOW_RULE in prompt
    assert "时间戳" in ctx.NOW_RULE and "不要" in ctx.NOW_RULE


def test_turn_note_carries_the_current_time_and_the_mandate():
    """D216：每轮贴着输入再硬提醒一次 —— "现在是几点" + "必须用"（不是"参考"）。"""
    messages = ctx.build_messages(_friend(), user_input="在吗", now=NOW)
    note = messages[-2]
    assert note["role"] == "user"
    assert "现在是 2026-09-18 21:30（周五）" in note["content"]
    assert "必须用" in note["content"]
