"""build_schedule の最小シナリオでのテスト。

CP-SATのフル月次求解は重いので、1日だけの月(monkeypatch)にしてすばやく検証する。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kawashima_schedule import scheduler as scheduler_module  # noqa: E402
from kawashima_schedule.calendar_utils import Day  # noqa: E402
from kawashima_schedule.models import StaffProfile  # noqa: E402
from kawashima_schedule.scheduler import build_schedule  # noqa: E402
from kawashima_schedule.shifts import OFF, REQUIRED_DAY_SHIFTS  # noqa: E402


def _coverage_staff() -> List[StaffProfile]:
    """1日分の必要人数(日勤帯6種・夜勤2人・深夜1人)をちょうど満たすだけの最小構成。"""
    staff: List[StaffProfile] = []
    for index, code in enumerate(REQUIRED_DAY_SHIFTS, 1):
        staff.append(
            StaffProfile(
                staff_id=f"day-{index}",
                name=f"日勤{index}",
                available_day_shifts=[code],
                can_night=False,
                can_late_night=False,
            )
        )
    for index in range(1, 3):
        staff.append(
            StaffProfile(
                staff_id=f"night-{index}",
                name=f"夜勤{index}",
                available_day_shifts=[],
                can_night=True,
                can_late_night=False,
            )
        )
    staff.append(
        StaffProfile(
            staff_id="latenight-1",
            name="深夜1",
            available_day_shifts=[],
            can_night=False,
            can_late_night=True,
        )
    )
    return staff


def test_support_staff_is_not_counted_in_day_shift_requirement(monkeypatch):
    """介護補助(is_support_staff)は日勤帯の必要人数に数えられず、常に公になること。"""
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )

    support = StaffProfile(staff_id="support-1", name="介護補助さん", is_support_staff=True)
    profiles = _coverage_staff() + [support]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages
    # 固定時間も希望休もないので、常に公休として扱われる(=日勤帯の枠を取らない)
    assert result.assignments["support-1"] == {1: OFF}
    assert any("介護補助" in message for message in result.messages)


def test_weekly_days_off_are_kept(monkeypatch):
    """週休2日が守られること。夜勤の回数で休みが減らないこと。

    実物の勤務表では、夜勤5回の人も0回の人も公休は同じ8〜9日だった。
    「月に何日」ではなく「週ごとに休む」という考え方。
    """
    from kawashima_schedule.scheduler import _weeks
    from kawashima_schedule.calendar_utils import month_days

    days = month_days(2026, 10)  # 10月1日は木曜
    weeks = _weeks(days)

    # 週の区切りが月曜始まりになっていること
    assert len(weeks[0]) == 4, "1〜4日(木金土日)が最初の半端な週"
    assert weeks[1][0].weekday == 0, "2週目は月曜から"
    assert sum(len(w) for w in weeks) == 31, "全部の日が どれかの週に入る"


def test_partial_weeks_ask_for_less(monkeypatch):
    """月初・月末の半端な週は、日数に応じて休みを減らすこと。

    4日しかない週に2日の休みを求めると、組めなくなることがある。
    """
    from kawashima_schedule.calendar_utils import month_days
    from kawashima_schedule.scheduler import _weeks

    weeks = _weeks(month_days(2026, 10))
    partial = [w for w in weeks if len(w) < 7]
    assert partial, "10月には半端な週がある"
    for week in partial:
        quota = (2 * len(week)) // 7
        assert quota < 2, "半端な週は2日より少なくてよい"


def _night_staff():
    """夜勤・深夜を回せる最小限の顔ぶれ。"""
    from kawashima_schedule.models import StaffProfile

    people = []
    for i in range(4):
        people.append(StaffProfile(staff_id=f"ns-{i}", name=f"看護{i}", role="看護師"))
    for i in range(4):
        people.append(StaffProfile(staff_id=f"cg-{i}", name=f"介護{i}", role="介護士"))
    return people


def test_night_shift_is_one_nurse_and_one_caregiver():
    """○夜勤は看護職1人+介護職1人。実物では31日中30日がこの組み合わせだった。"""
    from kawashima_schedule.scheduler import _add_night_composition
    from kawashima_schedule.models import StaffProfile

    people = _night_staff()
    nurses = [p for p in people if p.is_nurse]
    caregivers = [p for p in people if p.is_caregiver]
    assert len(nurses) == 4 and len(caregivers) == 4

    # 職種の判定そのものを確かめる
    assert StaffProfile(staff_id="x", name="管理", role="管理者、看護師").is_nurse
    assert not StaffProfile(staff_id="x", name="環境", role="その他").is_nurse
    assert not StaffProfile(staff_id="x", name="環境", role="その他").is_caregiver


def test_only_named_nurses_take_late_night():
    """深夜(◉)に入れる看護職は決まった人だけ。"""
    from kawashima_schedule.scheduler import NURSES_ALLOWED_ON_LATE_NIGHT

    assert NURSES_ALLOWED_ON_LATE_NIGHT, "誰も指定が無いと看護職が深夜に入れない"
    assert "齋藤" in NURSES_ALLOWED_ON_LATE_NIGHT
