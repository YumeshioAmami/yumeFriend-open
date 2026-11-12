from backend.agent.parser import MoodTagParser


def _parse(text: str, allowed: list[str]) -> tuple[str, list[str]]:
    parser = MoodTagParser(allowed)
    visible = parser.feed(text) + parser.flush()
    return visible, parser.moods


def test_custom_label_in_bold_tag_is_parsed_and_stripped():
    visible, moods = _parse("**大笑**：你好呀", ["大笑"])
    assert visible == "你好呀"
    assert moods == ["大笑"]


def test_custom_label_in_bracket_and_angle_tags_is_parsed():
    assert _parse("[大笑]：你好", ["大笑"]) == ("你好", ["大笑"])
    assert _parse("<状态：大笑>你好", ["大笑"]) == ("你好", ["大笑"])


def test_custom_label_is_split_across_chunks():
    parser = MoodTagParser(["大笑"])
    first = parser.feed("**大")
    second = parser.feed("笑**：你好")
    visible = first + second + parser.flush()
    assert visible == "你好"
    assert parser.moods == ["大笑"]


def test_unknown_label_is_not_treated_as_a_configured_tag():
    visible, moods = _parse("**乱写**：你好", ["大笑"])
    assert visible == "**乱写**：你好"
    assert moods == []
