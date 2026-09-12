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
from kawashima_schedule.models import (  # noqa: E402
    FixedTimeSlot,
    PairConstraint,
    StaffProfile,
)
from kawashima_schedule.scheduler import (  # noqa: E402
    ALERT_MARK,
    allowed_entry_marks,
    build_schedule,
)
from kawashima_schedule.request_sheet import StaffRequests  # noqa: E402
from kawashima_schedule.shifts import (  # noqa: E402
    LATE_NIGHT_IN,
    NIGHT_IN,
    OFF,
    REQUIRED_DAY_SHIFTS,
)


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


def test_staff_with_a_time_window_still_follows_the_limits():
    """「6時から17時の枠」のような時間指定があっても、常勤は上限の対象。

    ここを writes_own_hours で判定すると、番号シフトで普通に働く常勤まで
    連続勤務や週休2日の対象から外れてしまう(実際に13日連続が出た)。
    """
    from kawashima_schedule.models import FixedTimeSlot, StaffProfile
    from kawashima_schedule.scheduler import _has_own_schedule

    # 条件文に時間の枠が書かれた常勤 → 対象に含める
    nurse = StaffProfile(
        staff_id="n1", name="常勤看護", role="看護師", work_hours=("06:00", "17:00")
    )
    assert nurse.writes_own_hours, "時間枠は持っている"
    assert not _has_own_schedule(nurse), "それでも上限の対象に含める"

    # 曜日ごとに勤務時間が決まっているパート → 対象外
    part = StaffProfile(
        staff_id="p1",
        name="パート",
        fixed_time_slots=[FixedTimeSlot(weekday=1, start="09:00", end="15:00")],
    )
    assert _has_own_schedule(part)

    # 介護補助 → 対象外
    support = StaffProfile(staff_id="s1", name="補助", is_support_staff=True)
    assert _has_own_schedule(support)


def test_max_consecutive_days_is_five():
    from kawashima_schedule.shifts import MAX_CONSECUTIVE_WORK_DAYS

    assert MAX_CONSECUTIVE_WORK_DAYS == 5


# --- 入れる勤務が1つも無い人(実データで見つかった不具合) --------------------------


def _staff_with_night_roles() -> List[StaffProfile]:
    """夜勤の顔ぶれ(看護1+介護1)を満たせる最小構成。深夜の枠は空けてある。

    介護職を1人でも足すと「○に介護職ちょうど1人」の条件が効くので、
    役割を設定しないままだと組めなくなる。
    """
    staff = [p for p in _coverage_staff() if p.staff_id != "latenight-1"]
    by_id = {p.staff_id: p for p in staff}
    by_id["night-1"].role = "看護師"
    by_id["night-2"].role = "介護士"
    return staff


def _late_night_filler() -> StaffProfile:
    """深夜(◉)の枠を埋めるだけの介護職。"""
    return StaffProfile(
        staff_id="latenight-fill",
        name="深夜要員",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=True,
    )


def test_深夜のみと夜勤専従が両方立っていても深夜に入れる(monkeypatch):
    """原文の言い回しが重なって両方立つことがある。より具体的な「深夜のみ」を採る。

    以前はこの組み合わせで日勤も夜勤も深夜も全部禁止され、
    何の警告も出ないまま1か月まるごと公休になっていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    both = StaffProfile(
        staff_id="both-1",
        name="深夜専従さん",
        role="介護士",
        night_shift_exclusive=True,
        late_night_only=True,
    )
    # 深夜の枠を this 人に取らせるため、既定の深夜要員は外す
    profiles = _staff_with_night_roles() + [both]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["both-1"] == {1: LATE_NIGHT_IN}


def test_入れる勤務が無い人は黙って全公休にせず報告する(monkeypatch):
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    nobody = StaffProfile(
        staff_id="none-1",
        name="入れない人",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
    )
    profiles = _staff_with_night_roles() + [_late_night_filler(), nobody]
    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["none-1"] == {1: OFF}
    assert any("★要確認" in m and "入れない人" in m for m in result.messages)


def test_番号のシフトに合わない勤務時間は時間をそのまま書く(monkeypatch):
    """8:00-15:00 のような、番号のシフトに当てはまらないパートの勤務時間。

    以前は入れる日勤帯が空になり、警告も無いまま1か月すべて公休になっていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    part = StaffProfile(
        staff_id="part-1",
        name="時間パートさん",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        work_hours=("08:00", "15:00"),
    )
    wish = StaffRequests(staff_id="part-1", name="時間パートさん", entries={1: "日"})

    profiles = _staff_with_night_roles() + [_late_night_filler(), part]
    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["part-1"] == {1: "08:00-15:00"}


def test_時間直書きの人の有給が反映される(monkeypatch):
    """有給は公休と別に数えるので、勤務や公で塗りつぶしてはいけない。

    以前は fixed_time_slots のある人に「有」を出しても勤務時間が入っていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    part = StaffProfile(
        staff_id="slot-1",
        name="曜日パートさん",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        fixed_time_slots=[FixedTimeSlot(weekday=1, start="09:00", end="13:00")],
    )
    wish = StaffRequests(staff_id="slot-1", name="曜日パートさん", entries={1: "有"})

    profiles = _staff_with_night_roles() + [_late_night_filler(), part]
    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["slot-1"] == {1: "有"}


def test_希望出勤が無く有給だけの人は勤務0日として要確認になる(monkeypatch):
    """有給・夏休などは「勤務した日」に数えてはいけない。

    以前は worked を `mark != OFF` だけで数えていたため、希望出勤が1件も無く
    有給が入っているだけの月でも worked>0 になり、要確認(★)の警告が出ないまま
    「勤務時間をそのまま入れました」という誤った安心メッセージになっていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    part = StaffProfile(
        staff_id="paid-1",
        name="有給だけさん",
        role="介護士",
        available_day_shifts=[],
        can_night=False,
        can_late_night=False,
        work_hours=("08:00", "15:00"),
    )
    # 希望出勤は無く、有給だけが入っている
    wish = StaffRequests(staff_id="paid-1", name="有給だけさん", entries={1: "有"})

    profiles = _staff_with_night_roles() + [_late_night_filler(), part]
    result = build_schedule(profiles, requests=[wish], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["paid-1"] == {1: "有"}
    # 有給は「勤務した日」ではないので、勤務0日として要確認に回る
    assert any(ALERT_MARK in m and "有給だけさん" in m for m in result.messages)
    assert not any("そのまま入れました" in m and "有給だけさん" in m for m in result.messages)


def test_夜勤専従は深夜にも入れる(monkeypatch):
    """「夜勤専従」は夜間業務の専従という意味で、○だけでなく◉にも入る。

    以前は○だけに絞っていた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    exclusive = StaffProfile(
        staff_id="ex-1",
        name="夜間専従さん",
        role="介護士",
        night_shift_exclusive=True,
    )
    assert allowed_entry_marks(exclusive, Day(date(2026, 9, 1))) == {
        NIGHT_IN,
        LATE_NIGHT_IN,
    }

    # 深夜の枠しか空いていなくても組めること
    profiles = _staff_with_night_roles() + [exclusive]
    result = build_schedule(profiles, requests=[], year=2026, month=9)

    assert result.ok, result.messages
    assert result.assignments["ex-1"] == {1: LATE_NIGHT_IN}


# --- 同席制約(実データで効いていなかった) ----------------------------------------


def _night_pair_staff() -> List[StaffProfile]:
    """○2人(看護1+介護1)と◉1人をちょうど満たす、日勤帯なしの最小構成。"""
    staff = [p for p in _coverage_staff() if p.staff_id != "latenight-1"]
    by_id = {p.staff_id: p for p in staff}
    by_id["night-1"].role = "看護師"
    by_id["night-2"].role = "介護士"
    return staff + [_late_night_filler()]


def test_同じ夜に2人とも夜勤なら同席として数える(monkeypatch):
    """○は毎晩2人いるので、○と○の組み合わせも同席になる。

    以前は○と◉の組み合わせしか数えておらず、看護師と介護士の
    「同席不可」が素通りしていた。実データでは実際に同席が発生していた。
    """
    monkeypatch.setattr(
        scheduler_module, "month_days", lambda year, month: [Day(date(2026, 9, 1))]
    )
    profiles = _night_pair_staff()
    by_id = {p.staff_id: p for p in profiles}
    by_id["night-1"].pair_constraints = [
        PairConstraint(other_staff=by_id["night-2"].name, kind="no_pair_night")
    ]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    # ○は看護1+介護1で2人必要なのに、その2人が同席不可 → 組めないのが正しい
    assert not result.ok, (
        "○と○の同席を数えていれば、この条件では組めないはず。"
        f"組めてしまった: {result.assignments}"
    )


def test_同席の回数上限が守られる(monkeypatch):
    """「月1回まで」なら、同じ夜に入るのは1回まで。"""
    days = [Day(date(2026, 9, day)) for day in (1, 2, 3)]
    monkeypatch.setattr(scheduler_module, "month_days", lambda year, month: days)

    profiles = _night_pair_staff()
    by_id = {p.staff_id: p for p in profiles}
    by_id["night-1"].pair_constraints = [
        PairConstraint(
            other_staff=by_id["night-2"].name, kind="max_shared_night", max_count=1
        )
    ]

    result = build_schedule(profiles, requests=[], year=2026, month=9)

    # ○は毎日この2人しかいないので、3日とも同席になり上限1回を超える
    assert not result.ok, "同席の上限を数えていれば組めないはず"
