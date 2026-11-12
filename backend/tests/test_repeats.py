"""重复护栏（D104 / T2.2-7）。

实测样本（真机数据库里那两条）：模型在**工具轮**里把上一轮说过的话又从头说一遍。
这里的用例就是照那两条的样子写的。
"""

from __future__ import annotations

from backend.agent.repeats import strip_repeats

ROUND1 = """（我跪伏在地，目光死死盯着屏幕上那个正在运行的界面）

主人……

（我挺起腰身，主动将臀部抬高，像是在展示自己最不堪的一面）

您正在看着奴……"""


def test_no_previous_text_keeps_everything():
    assert strip_repeats("你好呀。\n\n今天过得怎么样？", "") == "你好呀。\n\n今天过得怎么样？"


def test_identical_round_is_dropped_entirely():
    """整轮又从头说一遍 → 长内容一个字都不留（只剩"主人……"这种短叹词）。"""
    kept = strip_repeats(ROUND1, ROUND1)
    assert "跪伏在地" not in kept and "您正在看着奴" not in kept
    assert len(kept) < 12


def test_slightly_rewritten_round_is_dropped():
    """改几个字再说一遍也算重复（实测那两条正是这种）。"""
    long_round = (
        "（我跪伏在地，额头贴着冰冷的地板，身体因为极度的顺从而微微颤抖，"
        "听到您的命令我没有丝毫犹豫，声音低哑且带着哭腔般的讨好）\n\n"
        "主人……奴会乖乖听话的，您说什么就是什么，奴不敢有半点违抗。"
    )
    rewritten = long_round.replace("地板", "地面").replace("主人", "主上")
    assert strip_repeats(rewritten, long_round).strip() == ""


def test_new_content_survives():
    """真正的新内容要留住，一个字都不能误杀。"""
    new = "（我跪伏在地，目光死死盯着屏幕上那个正在运行的界面）\n\n主人……\n\n我记住了。"
    kept = strip_repeats(new, ROUND1)
    assert "我记住了。" in kept
    assert "跪伏在地" not in kept  # 这段上一轮说过


def test_short_lines_are_never_treated_as_repeats():
    """太短的重复（"嗯。""汪……"）不判 —— 误杀比漏判更糟。"""
    assert strip_repeats("嗯。\n\n汪……\n\n嗯。", "嗯。\n\n汪……") == "嗯。\n\n汪……\n\n嗯。"


def test_repeat_inside_the_same_round_is_dropped_too():
    """同一轮里自己把一段写两遍，也拦。"""
    piece = "这一句挺长的，应该被判为重复才对——它出现了两次。"
    once = f"{piece}\n\n我是新的一句。"
    twice = f"{once}\n\n{piece}"
    kept = strip_repeats(twice, once)
    # 新的那句留着，重复的那段（连同"我是新的一句"之外的重复）没了
    assert piece not in kept
    assert "我是新的一句。" in kept


def test_mid_paragraph_restart_is_dropped():
    """实测样本的另一种形态：第二段接在上一轮的半句话后面又重来一遍。"""
    previous = (
        "（我跪伏在地，目光死死盯着屏幕上那个正在运行的界面）\n\n"
        "求主人……别只看着屏幕……\n\n而是主人您真实的……惩罚……"
    )
    new = "而是主人您真实的……惩罚……（我跪伏在地，目光死死盯着屏幕上那个正在运行的界面）\n\n求主人……别只看着屏幕……"
    assert strip_repeats(new, previous).strip() == ""
