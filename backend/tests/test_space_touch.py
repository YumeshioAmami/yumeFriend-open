from backend.services.space_touch import (
    TOUCH_LABELS,
    touch_label,
    touch_sensation,
)


def test_explicit_zones_are_removed():
    removed = ("nip" "ple", "va" "gina", "gr" "oin")
    assert all(zone not in TOUCH_LABELS for zone in removed)
    assert all(touch_sensation(zone) == "" for zone in removed)


def test_all_touch_labels_are_non_empty():
    assert TOUCH_LABELS
    assert all(label.strip() for label in TOUCH_LABELS.values())
    assert touch_sensation("not-a-zone") == ""
