"""T2.4 验收：流式工具块解析器（architecture.md §4.2 的六个用例 + 边界）。

每个用例都按"逐字符喂"再跑一遍 —— 真实流式就是这么碎的。
"""

import json

import pytest

from backend.agent.parser import (
    LeadingStampStripper,
    LineStampStripper,
    MAX_TOOL_BYTES,
    MoodTagParser,
    ToolBlockParser,
    normalize_native_tool_call,
    strip_mood_marks,
)


def block(name: str, arguments: dict | None = None, **extra) -> str:
    return f"<tool>{json.dumps({'name': name, 'arguments': arguments or {}, **extra})}</tool>"


def run(chunks: list[str]) -> list[dict]:
    parser = ToolBlockParser()
    events: list[dict] = []
    for chunk in chunks:
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())
    return events


def texts(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "text")


def calls(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "tool_call"]


# ---------- architecture.md §4.2 的六个用例 ----------


def test_tag_split_across_chunks():
    """① 标签跨 chunk：正文不残缺，工具照常解析。"""
    events = run(["你好。", "\n<to", "ol>{\"name\":\"save_memory\",", "\"arguments\":{\"content\":\"x\"}}</to", "ol>", " 再见"])
    assert texts(events) == "你好。 再见"
    assert len(calls(events)) == 1
    assert calls(events)[0]["name"] == "save_memory"
    assert calls(events)[0]["arguments"] == {"content": "x"}


def test_two_blocks_in_one_chunk():
    """② 一个 chunk 里两个工具块 → 两个都发。"""
    chunk = block("save_memory", {"content": "a"}) + block("add_schedule", {"title": "b"})
    events = run([chunk])
    assert [c["name"] for c in calls(events)] == ["save_memory", "add_schedule"]
    assert texts(events) == ""


def test_broken_json_becomes_plain_text():
    """③ JSON 坏掉 → 当普通文本输出，不崩、不丢内容。"""
    events = run(["前", "<tool>这不是 json</tool>", "后"])
    assert texts(events) == "前<tool>这不是 json</tool>后"
    assert calls(events) == []


def test_tool_json_with_unescaped_quotes_is_repaired():
    """D241：模型把内容写成 `"content":"他说"没事"了"`（内容里有未转义英文引号）。

    以前：`json.loads` 失败 → 整块当正文吐出来（用户实测：记忆没存上、屏幕上还露着 JSON）。
    现在：**先修再解**，修好了就当工具调用。
    """
    raw = (
        '<tool>{"name":"save_memory","arguments":'
        '{"content":"他说"没事"了","layer":"long_term"}}</tool>'
    )
    events = run([raw])
    assert len(calls(events)) == 1
    assert calls(events)[0]["name"] == "save_memory"
    assert calls(events)[0]["arguments"] == {"content": '他说"没事"了', "layer": "long_term"}
    assert texts(events) == ""


def test_tool_json_missing_final_brace_is_repaired():
    """D242：实测（2026-10-06）她漏了最外层那个 `}` —— 内容很长时发生。

    以前：JSON 解析在**最后一个字符**失败 → 整块当正文吐出来（记忆没存上）。
    现在：缺的括号自动补齐。
    """
    raw = (
        '<tool>{"name":"save_memory","arguments":'
        '{"content":"今天很松，决定不断了","layer":"long_term"}</tool>'
    )
    events = run([raw])
    assert len(calls(events)) == 1
    assert calls(events)[0]["arguments"] == {"content": "今天很松，决定不断了", "layer": "long_term"}
    assert texts(events) == ""


def test_tool_json_mismatched_final_bracket_is_repaired():
    """D248：实测（2026-10-12 / 10-13）她把**方向写反了** —— 三条 `save_memory` 都以 `}]`
    收尾（本该 `}}`）：`arguments` 关了、最外层那个 `}` 写成了 `]`。

    旧版只会“在结尾补”，补出 `}]}` 照样非法 → 整块当正文吐出来（用户看到 JSON、记忆没存上）。
    现在按栈顶**纠正方向**。下面这条就是她 10-13 那条真消息的原文。
    """
    raw = (
        '<tool>{"name":"save_memory","arguments":{"content":'
        '"2026-10-13 梦汐的新观察：在宿舍里学更好，因为随时能停、能退出，选择权在自己手上；'
        '在外面学习容易困。解法：出门前留一句「困了就站起来走」，把「被关住」改写成「自己签的字」。",'
        '"layer":"long_term"}]</tool>'
    )
    events = run([raw])
    assert len(calls(events)) == 1
    arguments = calls(events)[0]["arguments"]
    assert arguments["layer"] == "long_term"
    assert arguments["content"].startswith("2026-10-13 梦汐的新观察")
    assert arguments["content"].endswith("自己签的字」。")
    assert texts(events) == ""


def test_tool_json_extra_closing_bracket_is_dropped():
    """D248：多写了一个闭合符 → 丢掉多余的那个，照样能解析。"""
    raw = '<tool>{"name":"save_memory","arguments":{"content":"x"}}}</tool>'
    events = run([raw])
    assert len(calls(events)) == 1
    assert calls(events)[0]["arguments"] == {"content": "x"}
    assert texts(events) == ""


def test_close_open_brackets_never_touches_valid_json():
    """防误伤：合法 JSON（含数组 / 正文里的方括号 / 转义引号）一个字符都不许改。"""
    from backend.agent.parser import _close_open_brackets

    for good in [
        '{"name":"x","arguments":{"tags":["a","b"]}}',
        '{"content":"他笑了：[1] 和 [图片#12] 都在","layer":"long_term"}',
        '{"content":"a \\" b"}',
    ]:
        assert _close_open_brackets(good) == good


def test_close_open_brackets_fixes_reversed_direction():
    """方向写反了两个方向都要能纠（`}` 撞 `[` 也一样）。"""
    from backend.agent.parser import _close_open_brackets

    assert _close_open_brackets('{"a":{"b":1}]') == '{"a":{"b":1}}'
    assert _close_open_brackets('{"a":[1,2}') == '{"a":[1,2]}'


def test_unclosed_block_is_dropped_at_end():
    """④ 到流结束未闭合 → 丢弃该块，正文保留。"""
    events = run(["正文", "<tool>{\"name\":\"save_memory\""])
    assert texts(events) == "正文"
    assert calls(events) == []


def test_lone_angle_bracket_is_plain_text():
    """⑤ 正文里出现 "<" 但不是标签 → 正常输出。"""
    events = run(["1 < 2 且 a<b", " 还有 <工具> 这种"])
    assert texts(events) == "1 < 2 且 a<b 还有 <工具> 这种"
    assert calls(events) == []


def test_block_in_middle_keeps_both_sides():
    """⑥ 工具块在正文中间 → 前后正文拼接正确（顺带清掉紧邻空行）。"""
    events = run(["前半句。\n\n", block("save_memory", {"content": "x"}), "\n\n后半句。"])
    assert texts(events) == "前半句。后半句。"
    assert len(calls(events)) == 1


# ---------- 其它边界 ----------


def test_partial_tag_that_turns_out_not_a_tag():
    """扣住的前缀最后不是 <tool> → 原样吐回来，一个字都不能少。"""
    events = run(["<to", "tal 不是标签"])
    assert texts(events) == "<total 不是标签"


def test_oversized_unclosed_block_spills_as_text():
    """超过 8KB 还没闭合 → 当普通文本吐出去（不能无限缓冲）。"""
    events = run(["<tool>", "x" * (MAX_TOOL_BYTES + 10)])
    assert calls(events) == []
    assert texts(events).startswith("<tool>")
    assert len(texts(events)) > MAX_TOOL_BYTES


def test_arguments_missing_becomes_empty_dict():
    events = run(['<tool>{"name":"recall_memory"}</tool>'])
    assert calls(events)[0]["arguments"] == {}


def test_arguments_without_name_is_plain_text():
    events = run(['<tool>{"arguments":{"a":1}}</tool>'])
    assert calls(events) == []
    assert "<tool>" in texts(events)


def test_tool_call_ids_are_stable_and_unique():
    events = run([block("a") + block("b")])
    ids = [c["id"] for c in calls(events)]
    assert ids == ["tool_1", "tool_2"]


# ---------- v5.0（D260）：DeepSeek 的 DSML 工具块 ----------

DSML_SAMPLE = """<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="game_do">
<｜｜DSML｜｜ parameter name="arguments" string="false">{"action": "place", "args": {"block": "oak_planks", "x": -86, "y": 76, "z": -124}}</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="name" string="true">game_do</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
<｜｜DSML｜｜ invoke name="game_say">
<｜｜DSML｜｜ parameter name="arguments" string="false">{"text": "木板往门口一立"}</｜｜DSML｜｜ parameter>
<｜｜DSML｜｜ parameter name="name" string="true">game_say</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""


def test_dsml_tool_block_is_parsed():
    """实测：DeepSeek 有时吐自己的 DSML 工具格式，以前整块当正文（工具没执行）。"""
    events = run([DSML_SAMPLE])
    assert texts(events) == ""
    assert [call["name"] for call in calls(events)] == ["game_do", "game_say"]
    assert calls(events)[0]["arguments"] == {
        "action": "place",
        "args": {"block": "oak_planks", "x": -86, "y": 76, "z": -124},
    }
    assert calls(events)[1]["arguments"] == {"text": "木板往门口一立"}


def test_dsml_is_parsed_char_by_char():
    whole = run([DSML_SAMPLE])
    chars = run(list(DSML_SAMPLE))
    assert [(c["name"], c["arguments"]) for c in calls(chars)] == [
        (c["name"], c["arguments"]) for c in calls(whole)
    ]
    assert texts(chars) == ""


def test_empty_delta_is_noop():
    parser = ToolBlockParser()
    assert parser.feed("") == []
    assert parser.flush() == []


@pytest.mark.parametrize(
    "text",
    [
        "你好。\n\n" + block("save_memory", {"content": "生日 5/20"}) + "\n\n再见。",
        "正文中间" + block("switch_mode", {"mode": "roleplay"}) + "后面还有正文",
        block("a") + block("b", {"x": 1}),
        "没有工具块，只有 1 < 2 这种比较符和 <tool 半截标签。",
        "开头就是工具块" + block("save_memory", {"content": "x"}),
        "```\n正文\n```\n" + block("a") + "\n```\n收尾\n```",
    ],
)
def test_char_by_char_equals_single_chunk(text):
    """把同一段内容按"一个字符一个 chunk"喂进去，结果必须和一次喂完完全一致。

    这是真实流式最碎的极端情况——标签被切碎在这个用例下必然暴露。
    """
    whole = run([text])
    chars = run(list(text))
    assert texts(chars) == texts(whole)
    assert [(c["name"], c["arguments"]) for c in calls(chars)] == [
        (c["name"], c["arguments"]) for c in calls(whole)
    ]


def test_flush_returns_held_text():
    events = run(["结尾有个空格 "])
    assert texts(events) == "结尾有个空格 "


def test_in_tool_flag_tracks_state():
    parser = ToolBlockParser()
    parser.feed("<tool>")
    assert parser.in_tool is True
    parser.feed('{"name":"a","arguments":{}}')
    assert parser.in_tool is True
    parser.feed("</tool>")
    assert parser.in_tool is False


def test_code_fence_noise_is_cleaned():
    events = run([
        "正文\n```\n",
        '<tool>{"name":"a","arguments":{}}</tool>',
        "\n```\n",
        "收尾",
    ])
    assert texts(events) == "正文收尾"
    assert len(calls(events)) == 1


# ---------- 原生协议归一化（LLM协议§4.2）----------


def test_normalize_native_tool_call_parses_arguments():
    event = normalize_native_tool_call(
        {"id": "call_1", "name": "save_memory", "arguments": '{"content": "x"}'}
    )
    assert event == {
        "type": "tool_call",
        "id": "call_1",
        "name": "save_memory",
        "arguments": {"content": "x"},
    }


def test_normalize_native_tool_call_bad_json_keeps_raw():
    event = normalize_native_tool_call({"name": "x", "arguments": "{oops"})
    assert event["arguments"] == {}
    assert event["arguments_raw"] == "{oops"


def test_normalize_native_tool_call_empty_arguments():
    event = normalize_native_tool_call({"name": "x", "arguments": ""})
    assert event["arguments"] == {}
    assert "arguments_raw" not in event

# ---------- v3.2（D147）：情绪标签的两种形态 ----------


def mood_run(chunks: list[str]) -> tuple[str, list[str]]:
    """逐块喂，返回（用户看到的干净文字，摘到的情绪表）。"""
    parser = MoodTagParser()
    out: list[str] = []
    for chunk in chunks:
        out.append(parser.feed(chunk))
    out.append(parser.flush())
    return "".join(out), parser.moods


def test_legacy_portrait_tag_still_works():
    text, moods = mood_run(["[立绘:高兴]你好呀"])
    assert text == "你好呀"
    assert moods == ["高兴"]


def test_bracket_mood_tag_is_stripped_and_recognized():
    """DeepSeek 实测主流形态：`[调皮]`（没有"立绘:"前缀）。"""
    text, moods = mood_run(["[调皮]你猜呀～"])
    assert text == "你猜呀～"
    assert moods == ["调皮"]


def test_bracket_mood_tag_split_across_chunks():
    """`[调` + `皮]` 被切开：半个标签先扣住，不许漏出"皮]"这种残渣。"""
    text, moods = mood_run(["[调", "皮]你猜", "呀"])
    assert text == "你猜呀"
    assert moods == ["调皮"]


def test_bracket_tag_new_words_from_real_chats():
    """真实记录里出现、原词表漏掉的三个（D147）。"""
    text, moods = mood_run(["[顺从]好。", "[乞求]别走。", "[关心]吃了吗。"])
    assert moods == ["顺从", "乞求", "关心"]
    assert "[" not in text and "]" not in text


def test_strip_mood_marks_handles_both_forms():
    assert strip_mood_marks("[平静]嗯。") == "嗯。"
    assert strip_mood_marks("**调皮**：嘻嘻") == "嘻嘻"
    # 2026-10-02（用户拍板）：**行内的 `**词**：` 不再摘** —— 宁可留标签残渣，也绝不吃正文。
    assert strip_mood_marks("我**生气**：哼") == "我**生气**：哼"


def test_markdown_bold_and_heading_mood_labels_are_stripped():
    text, moods = mood_run(["**疑惑**：那本书在脚边。\n**正常**：你在忙吗？"])
    assert text == "那本书在脚边。\n你在忙吗？"
    assert moods == ["疑惑", "正常"]

    text, moods = mood_run(["## 疑", "惑\n那本书在脚边。\n### 害羞：别看了。"])
    assert text == "那本书在脚边。\n别看了。"
    assert moods == ["疑惑", "害羞"]
    assert strip_mood_marks("## 疑惑\n正文") == "正文"


def test_bracket_text_is_never_eaten():
    """`[1]` / `[图片#12 · 09-20]` 是正文 —— 一个字都不许动。"""
    plain = "参考 [1] 和 [图片#12 · 09-20] 都在。"
    assert strip_mood_marks(plain) == plain
    text, moods = mood_run(list(plain))
    assert text == plain
    assert moods == []


def test_fullwidth_bracket_mood_tag_is_recognized():
    """v3.6（D154）：`【情绪】`（LingChat 那边的老习惯）也要认 —— 摘掉并算成情绪。"""
    text, moods = mood_run(["【调皮】不给你看~"])
    assert text == "不给你看~"
    assert moods == ["调皮"]


def test_fullwidth_tag_split_across_chunks():
    text, moods = mood_run(["【调", "皮】来嘛"])
    assert text == "来嘛"
    assert moods == ["调皮"]


def test_new_mood_words_from_her_real_messages():
    """她真用过的词（正经 15 次 / 好奇 11 / 思考 6 …）现在都认。"""
    text, moods = mood_run(["【正经】先算一下。", "[好奇]然后呢？", "（思考中）[思考]"])
    assert moods == ["正经", "好奇", "思考"]
    assert "【" not in text and "】" not in text and "[" not in text


def test_new_companion_mood_aliases_are_stripped():
    """D370：真实新人格会写【软】【笑】【宠溺】，必须摘掉并记成情绪。"""
    text, moods = mood_run(
        ["【软】过来嘛。", "【笑】先说好。", "【宠溺】拿你没办法。"]
    )
    assert text == "过来嘛。先说好。拿你没办法。"
    assert moods == ["软", "笑", "宠溺"]
    assert "【" not in text and "】" not in text


def test_angle_state_tags_are_stripped_and_normalized():
    """D370 实测算术：模型会写 `<状态：蹙眉>` / `<情绪：软下来>`。"""
    text, moods = mood_run(
        [
            "<状",
            "态：蹙",
            "眉>都三点半了。\n<情绪：软下来，尾",
            "巴圈过去> 那我把尾巴借你。",
        ]
    )
    assert text == "都三点半了。\n那我把尾巴借你。"
    assert moods == ["担心", "软"]
    assert "<状态" not in text and "<情绪" not in text


def test_generic_fullwidth_mood_tags_are_stripped():
    """D370 实测算术：`【心疼】` / `【软下来】` / `【轻声】` 都要认。"""
    text, moods = mood_run(
        [
            "【心",
            "疼】那我不讲道理了。",
            "\n【软下来】来呀。",
            "\n【轻声】抱着睡可以。",
        ]
    )
    assert text == "那我不讲道理了。\n来呀。\n抱着睡可以。"
    assert moods == ["担心", "软", "小声"]
    assert "【" not in text and "】" not in text


def test_paired_angle_mood_tags_are_stripped():
    """D370 实测算术：模型会写 `<开心>正文</开心>`。"""
    text, moods = mood_run(
        [
            "<开",
            "心>那我就说真话啦。\n</开心>",
            "<撒娇>现在三点半了。</撒娇>",
        ]
    )
    assert text == "那我就说真话啦。\n现在三点半了。"
    assert moods == ["开心", "撒娇"]
    assert "<" not in text and ">" not in text


def test_fullwidth_parenthesis_mood_tags_are_stripped():
    """D370 实测算术：模型会写 `（轻声）正文` / `（靠过来）正文`。"""
    text, moods = mood_run(
        ["（轻声）真心话是我想你。", "\n（靠过来）别讲了。"]
    )
    assert text == "真心话是我想你。\n别讲了。"
    assert moods == ["小声", "心动"]
    assert "（" not in text and "）" not in text


def test_parenthesis_action_text_is_not_eaten():
    """动作/旁白 `（记下了）` 是正文，不是情绪标签。"""
    text, moods = mood_run(["（记下了）明天见。"])
    assert text == "（记下了）明天见。"
    assert moods == []


def test_any_common_bracket_mood_label_is_stripped():
    """D370：模型会换 `［］` / `｛｝` / `《》`，不能一对手工补。"""
    text, moods = mood_run(
        ["［有点慌］我一直在等你。", "\n｛轻轻｝手放我背上。", "\n《软下来》先躺下。"]
    )
    assert text == "我一直在等你。\n手放我背上。\n先躺下。"
    assert moods == ["慌张", "小声", "软"]
    assert "［" not in text and "｝" not in text and "《" not in text


# ---------- v3.6（D156）：情绪标签后面的冒号 ------------------------------


def test_colon_after_mood_tag_is_eaten():
    """`[开心]：xxx` / `【害羞】：xxx` —— 冒号要**跟着标签一起摘**。

    实测背景：模型老写这个形态，以前只摘方括号、把冒号留下，
    用户看到的回复就是"：xxx"（阿月 561 条里 143 条这样）。
    """
    assert strip_mood_marks("[开心]：今天好累。") == "今天好累。"
    assert strip_mood_marks("[调皮]:来嘛~") == "来嘛~"
    assert strip_mood_marks("【害羞】：别看了。") == "别看了。"
    assert strip_mood_marks("[立绘:高兴]:你好") == "你好"
    # 正文里的方括号一个字都不许动
    assert strip_mood_marks("[图片#12]：这张图") == "[图片#12]：这张图"
    assert strip_mood_marks("[1]：注") == "[1]：注"


def test_colon_after_mood_tag_streaming():
    """流式：标签先到、冒号后到 —— 也不能把冒号漏出去。"""
    text, moods = mood_run(["[开心]", "：今天好累。"])
    assert text == "今天好累。"
    assert moods == ["开心"]
    # 标签正好在结尾（后面没冒号）
    text2, moods2 = mood_run(["好吧[开心]"])
    assert text2 == "好吧"
    assert moods2 == ["开心"]
    # 没有冒号也不能丢字
    text3, moods3 = mood_run(["[开心]", "今天好累。"])
    assert text3 == "今天好累。"
    assert moods3 == ["开心"]


def test_mood_tag_then_newline_then_colon():
    """v3.7（D157）：标签和冒号之间可能空一行（`[心动]\n\n：我也想你`）—— 也要一起摘。"""
    assert strip_mood_marks("[心动]\n\n：我也想你。") == "我也想你。"
    assert strip_mood_marks("[心动]\n：我也想你。") == "我也想你。"
    text, moods = mood_run(["[心动]\n\n：", "我也想你"])
    assert text == "我也想你"
    assert moods == ["心动"]
    # 非情绪方括号照旧不许动
    assert strip_mood_marks("[图片#3]：这张图") == "[图片#3]：这张图"


def test_colon_before_mood_tag_too():
    """v3.8：冒号还可能跑到标签**前面**（`：[得意]本天才少女上线`）—— 实测汐音整条都是这个写法。"""
    assert strip_mood_marks("：[得意]本天才少女上线") == "本天才少女上线"
    assert strip_mood_marks("：[害羞]别看了") == "别看了"
    text, moods = mood_run(["：[得意]", "本天才少女上线"])
    assert text == "本天才少女上线"
    assert moods == ["得意"]
    # 正文里的方括号（`[1]`）照旧不许动
    assert strip_mood_marks("他笑了：[1]：注") == "他笑了：[1]：注"
def test_orphan_leading_colon_after_a_bold_label_is_stripped():
    """v3.8（D159 的后半边）：`：**正常**：在的` 这种"冒号在最前 + 粗体标签"——摘完标签
    会剩一个孤零零的 `：`。前端 `stripMoodMarks` 早有这道兜底，**后端漏了**——
    而朋友列表的预览正是后端算的（实测列表上显示"：在的…"）。
    """
    assert strip_mood_marks("：**正常**：在的，随叫随到") == "在的，随叫随到"
    assert strip_mood_marks("：**调皮**：嘻嘻") == "嘻嘻"
    # 多段里第二段开头也一样
    assert strip_mood_marks("第一段。\n：**高兴**：第二段") == "第一段。\n第二段"


def test_orphan_colon_rule_does_not_touch_normal_text():
    """只认"行首 + 冒号 + 紧跟中文"这一种形状，别的一律不许动。"""
    assert strip_mood_marks("比例是 1:2 左右") == "比例是 1:2 左右"
    assert strip_mood_marks("：) 你看") == "：) 你看"          # 表情
    assert strip_mood_marks("：sorry，我错了") == "：sorry，我错了"  # 后面不是中文
    assert strip_mood_marks("看这个：[1] 来源") == "看这个：[1] 来源"


# ---------- v3.31（D216）：回复开头的时间戳兜底 ----------


def test_stamp_stripper_removes_a_leading_stamp():
    """模型可能学历史的样子、在开头写个时间戳 —— 显示层要剥掉（D216）。"""
    stripper = LeadingStampStripper()
    assert stripper.feed("[09-28 21:03] 在的") == "在的"


def test_stamp_stripper_handles_a_chunked_stamp():
    """时间戳会被 chunk 切开（`[09-28 ` + `21:03] …`）—— 先扣着，拼齐了再剥。"""
    stripper = LeadingStampStripper()
    assert stripper.feed("[09-28 ") == ""
    assert stripper.feed("21:03] 在的") == "在的"


def test_stamp_stripper_handles_the_cross_year_form():
    stripper = LeadingStampStripper()
    assert stripper.feed("[2025-12-31 23:50] 新年快乐") == "新年快乐"


def test_stamp_stripper_leaves_normal_text_alone():
    """正文起步就放行 —— 一个字的正文都不许被耽误。"""
    stripper = LeadingStampStripper()
    assert stripper.feed("在的") == "在的"


def test_stamp_stripper_does_not_touch_image_placeholder_or_brackets():
    stripper = LeadingStampStripper()
    assert stripper.feed("[图片#12 · 09-19 18:01] 好看") == "[图片#12 · 09-19 18:01] 好看"
    assert stripper.feed("[1] 这是注脚") == "[1] 这是注脚"


def test_stamp_stripper_only_strips_the_first_stamp():
    stripper = LeadingStampStripper()
    assert stripper.feed("[09-28 21:03] 好") == "好"
    assert stripper.feed(" [09-28 21:04] 后面不动") == " [09-28 21:04] 后面不动"


def test_line_stamp_stripper_removes_each_bubble_stamp():
    stripper = LineStampStripper()
    chunks = ["[03:", "36] 第一句。\n[03:36] ", "第二句。"]
    text = "".join(stripper.feed(chunk) for chunk in chunks) + stripper.flush()
    assert text == "第一句。\n第二句。"


def test_line_stamp_stripper_leaves_normal_brackets_alone():
    stripper = LineStampStripper()
    text = stripper.feed("[1] 来源\n[图片#12] 这张") + stripper.flush()
    assert text == "[1] 来源\n[图片#12] 这张"


def test_stamp_stripper_flush_gives_back_held_text():
    stripper = LeadingStampStripper()
    assert stripper.feed("[09") == ""
    assert stripper.flush() == "[09"


def test_stamp_stripper_flush_strips_a_complete_stamp():
    stripper = LeadingStampStripper()
    assert stripper.feed("[2026-09-28 21:03]") == ""
    assert stripper.flush() == ""


def test_tool_block_with_trailing_text_still_parses():
    """D281：JSON 后面多写一句解释（Extra data）也不能整块当正文。"""
    events = run(
        [
            '<tool>{"name":"game_do","arguments":{"action":"eat"}}'
            " 这句是多写出来的</tool>"
        ]
    )
    found = calls(events)
    assert len(found) == 1
    assert found[0]["name"] == "game_do"
    assert found[0]["arguments"] == {"action": "eat"}
