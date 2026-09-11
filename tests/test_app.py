"""希望入力画面(app.py)のテスト。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import app  # noqa: E402
from kawashima_schedule.calendar_utils import month_days  # noqa: E402
from kawashima_schedule.models import StaffProfile  # noqa: E402


def test_from_frame_distinguishes_same_name_staff_by_id():
    """同姓同名の職員がいても、名前ではなく staff_id で突き合わせること。

    名前だけで突き合わせると辞書構築時に片方が消え、
    一方の希望がもう一方のものとして保存されてしまう。
    """
    profiles = [
        StaffProfile(staff_id="id-1", name="佐藤"),
        StaffProfile(staff_id="id-2", name="佐藤"),
    ]
    days = month_days(2026, 10)

    frame = app._to_frame(profiles, days, saved={})
    assert "スタッフID" in frame.columns

    frame.loc[frame["スタッフID"] == "id-1", app._column(days[0])] = "公"
    frame.loc[frame["スタッフID"] == "id-2", app._column(days[0])] = "○"

    requests = app._from_frame(frame, profiles, days)
    by_id = {r.staff_id: r for r in requests}

    assert len(requests) == 2
    assert by_id["id-1"].entries == {1: "公"}
    assert by_id["id-2"].entries == {1: "○"}


def test_column_order_hides_staff_id_from_the_editor():
    """スタッフIDは突き合わせ用の内部データで、画面には出さないこと。"""
    days = month_days(2026, 10)
    order = app._column_order(days)

    assert "スタッフID" not in order
    assert "スタッフ" in order
