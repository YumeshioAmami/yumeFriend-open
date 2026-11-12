from backend.api.messages import _align_moods, _paragraph_segments


def test_paragraph_segments_keeps_many_natural_paragraphs():
    text = "\n\n".join(f"第{i}段" for i in range(1, 9))
    parts = _paragraph_segments(text)
    assert len(parts) == 8
    assert parts[0] == "第1段"
    assert parts[-1] == "第8段"


def test_align_moods_keeps_each_paragraph_when_counts_match():
    moods = ["调皮", "无奈", "担心", "认真", "高兴", "自信", "害羞", "平静"]
    segments = [f"第{i + 1}段" for i in range(8)]
    marks = [{"mood": mood, "at": i} for i, mood in enumerate(moods)]
    assert _align_moods(segments, marks, fallback="高兴") == moods


def test_align_moods_does_not_collapse_many_paragraphs_to_one():
    segments = [f"第{i + 1}段" for i in range(8)]
    marks = [{"mood": "调皮", "at": 0}]
    aligned = _align_moods(segments, marks, fallback="高兴")
    # 只有一条标记时只能保守沿用；但绝不能把 8 段压成长度 1 的 moods。
    assert len(aligned) == 8
