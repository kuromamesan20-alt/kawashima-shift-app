"""勤務表Excel書き出し(excel_export.py)の集計・並び替えロジックのテスト。

SUMMARY の「残業」列は実物の勤務表にある手書き欄なので、
自動では埋めない(常に空欄)仕様。ここではテストしない。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kawashima_schedule.excel_export import (  # noqa: E402
    SUMMARY,
    _count,
    _order_staff,
    _split_mark,
)
from kawashima_schedule.models import StaffProfile  # noqa: E402
from kawashima_schedule.scheduler import ScheduleResult  # noqa: E402


def _empty_counts():
    return {label: 0 for label in SUMMARY}


# --- _count -------------------------------------------------------------------


def test_count_day_shift_marks():
    counts = _empty_counts()
    for mark in ("日①", "日②", "日③", "日", "日⑤", "日⑦", "日⑨"):
        _count(counts, mark)
    assert counts["日勤"] == 7


def test_count_night_in_mark():
    counts = _empty_counts()
    _count(counts, "○")
    assert counts["夜勤"] == 1


def test_count_late_night_in_mark():
    counts = _empty_counts()
    _count(counts, "◉")
    assert counts["深夜"] == 1


def test_count_off_mark():
    counts = _empty_counts()
    _count(counts, "公")
    assert counts["公休"] == 1


def test_count_paid_leave_mark():
    counts = _empty_counts()
    _count(counts, "有")
    assert counts["有休"] == 1


def test_count_summer_leave_mark():
    counts = _empty_counts()
    _count(counts, "夏")
    assert counts["夏正"] == 1


def test_count_training_and_health_check_marks():
    counts = _empty_counts()
    _count(counts, "研")
    _count(counts, "健")
    assert counts["出研"] == 2


def test_count_written_hours_are_counted_as_day_shift():
    """パート職員などの時間直書き(「09:00-15:00」)は日勤帯として数える。"""
    counts = _empty_counts()
    _count(counts, "09:00-15:00")
    assert counts["日勤"] == 1


def test_count_ignores_unknown_or_blank_marks():
    counts = _empty_counts()
    _count(counts, "")
    _count(counts, "せ")
    assert sum(counts.values()) == 0


def test_overtime_column_is_never_auto_filled():
    """残業は実物の手書き欄なので、_count はどんなマークでも増やさない。"""
    counts = _empty_counts()
    for mark in ("日①", "○", "◉", "公", "有", "夏", "研", "健", "09:00-15:00"):
        _count(counts, mark)
    assert counts["残業"] == 0


# --- _split_mark ----------------------------------------------------------------


def test_split_mark_splits_on_naka_nuke():
    """中抜け「09:00-12:00・19:00-20:00」は2つに分かれること。"""
    main, second = _split_mark("09:00-12:00・19:00-20:00")
    assert main == "09:00-12:00"
    assert second == "19:00-20:00"


def test_split_mark_leaves_ordinary_marks_untouched():
    for mark in ("日①", "○", "◉", "公", ""):
        main, second = _split_mark(mark)
        assert main == mark
        assert second == ""


# --- _order_staff ----------------------------------------------------------------


def _profile(**kwargs):
    defaults = dict(staff_id="", name="", sheet_label="")
    defaults.update(kwargs)
    return StaffProfile(**defaults)


def _result_with(profiles):
    return ScheduleResult(
        status="最適",
        year=2026,
        month=10,
        assignments={p.staff_id: {} for p in profiles},
    )


def test_order_staff_puts_head_nurse_first_then_units_then_support_staff():
    head = _profile(staff_id="head", name="師長", is_head_nurse=True)
    bara = _profile(staff_id="bara", name="A", unit="ばら", sheet_label="1")
    sakura = _profile(staff_id="sakura", name="B", unit="さくら", sheet_label="1")
    yuri = _profile(staff_id="yuri", name="C", unit="ゆり", sheet_label="1")
    sumire = _profile(staff_id="sumire", name="D", unit="すみれ", sheet_label="1")
    support = _profile(staff_id="support", name="E", is_support_staff=True, sheet_label="①")

    # わざと並びを崩して渡す
    profiles = [support, sumire, yuri, sakura, bara, head]
    result = _result_with(profiles)

    ordered = [p.staff_id for p in _order_staff(profiles, result)]
    assert ordered == ["head", "bara", "sakura", "yuri", "sumire", "support"]


def test_order_staff_mixes_plain_numbers_and_circled_numbers_correctly():
    """看護(1,2,3…)を先に、介護(①②③…)を後に並べる。丸数字と半角数字が混在してもよい。"""
    profiles = [
        _profile(staff_id="c2", name="F", unit="ばら", sheet_label="②"),
        _profile(staff_id="n2", name="G", unit="ばら", sheet_label="2"),
        _profile(staff_id="c1", name="H", unit="ばら", sheet_label="①"),
        _profile(staff_id="n1", name="I", unit="ばら", sheet_label="1"),
    ]
    result = _result_with(profiles)

    ordered = [p.staff_id for p in _order_staff(profiles, result)]
    assert ordered == ["n1", "n2", "c1", "c2"]


def test_order_staff_excludes_people_not_in_the_result():
    """勤務表に載らない人(休職中など)は除く。"""
    listed = _profile(staff_id="a", name="A", unit="ばら", sheet_label="1")
    not_listed = _profile(staff_id="b", name="B", unit="ばら", sheet_label="2")
    result = _result_with([listed])

    ordered = [p.staff_id for p in _order_staff([listed, not_listed], result)]
    assert ordered == ["a"]
