"""月次勤務表を組む。OR-Tools CP-SAT を使う。

絶対に守るルール(ハード制約)は model.Add で直接表し、
できるだけ守りたいこと(ソフト制約)は目的関数のペナルティにする。
組めない場合は INFEASIBLE を返すので、「この条件では組めない」を機械的に検知できる。

勤務表の記号:
  日①②③⑤⑦⑨   毎日ちょうど1人ずつ
  日             人数の決まりなし。残りの勤務者が入る枠
  ○ → △ → 公     夜勤の3日セット(毎日○が2人)
  ◉ → 公         深夜の2日セット(毎日◉が1人)
  公             公休
  せ             責任者(師長が休みの日だけ、日責ができる人を1人)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

from .calendar_utils import Day, month_days
from .models import StaffProfile
from .request_sheet import StaffRequests
from .shifts import (
    ABSENCE_MARKS,
    MAX_CONSECUTIVE_WORK_DAYS,
    DAILY_REQUIREMENT,
    DAY_SHIFT_CODES,
    EARLY_SHIFTS,
    LATE_NIGHT_IN,
    NIGHT_AFTER,
    NIGHT_IN,
    OFF,
    REQUIRED_DAY_SHIFTS,
)

# 割り当てうる記号(時間直書きの人を除く)
ALL_MARKS: Tuple[str, ...] = (
    tuple(DAY_SHIFT_CODES) + (NIGHT_IN, NIGHT_AFTER, LATE_NIGHT_IN, OFF) + ABSENCE_MARKS
)

# ソフト制約の重み。大きいほど優先して守る。
WEIGHT_WISH_WORK = 40  # 希望出勤に応える
WEIGHT_NIGHT_COUNT = 20  # 夜勤・深夜の希望回数の範囲
WEIGHT_WORK_DAYS = 8  # 週の勤務日数の目安
WEIGHT_WEEKEND_OFF = 5  # 土日どちらかは休みたい
WEIGHT_AVOID_EARLY = 5  # 早出には入れていない
# 「他の人で埋まらないときだけ使う」枠。他のどのソフト制約より重くして、
# 本当に最後の手段にする(これを破るくらいなら他の希望を諦める、という強さ)。
# 夜勤明けの翌日にまた夜勤に入る形。実物に1件あり許容されるが、
# 体への負担が大きいので、他に手が無いときだけになるよう強めに抑える。
WEIGHT_NIGHT_AFTER_NIGHT = 200

WEIGHT_LAST_RESORT = 300

# 深夜(◉)は介護職が8〜9割。看護職で入るのは決まった人だけ。
# 実物の2026年7月では、看護職の深夜3回はすべて齋藤さんだった。
NURSES_ALLOWED_ON_LATE_NIGHT = ("齋藤",)


@dataclass
class ScheduleResult:
    """組んだ結果。"""

    status: str  # "最適" / "実行可能" / "組めません" / "時間切れ"
    year: int
    month: int
    days: List[Day] = field(default_factory=list)
    # staff_id -> {日にち: 記号}。時間直書きの人は記号の代わりに "9:00-15:00" など。
    assignments: Dict[str, Dict[int, str]] = field(default_factory=dict)
    # 責任者「せ」。{日にち: staff_id}。師長が出勤している日は入らない。
    responsible: Dict[int, str] = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("最適", "実行可能")


def build_schedule(
    profiles: Sequence[StaffProfile],
    requests: Sequence[StaffRequests],
    year: int,
    month: int,
    time_limit_seconds: float = 60.0,
) -> ScheduleResult:
    days = month_days(year, month)
    result = ScheduleResult(status="組めません", year=year, month=month, days=days)

    by_staff_request = {request.staff_id: request for request in requests}

    # 番号のシフトに入らない人(曜日ごとに勤務時間が決まっているパート)は
    # 解く前に決めてしまう。ソルバーの対象からは外す。
    solver_profiles: List[StaffProfile] = []
    for profile in profiles:
        if profile.is_on_leave(year, month):
            result.messages.append(
                f"{profile.name}: {profile.leave_from} から休職のため、勤務表に入れていません"
            )
            continue
        if profile.is_support_staff or profile.fixed_time_slots:
            # 介護補助は環境整備などが仕事で、日勤帯の人数にも夜勤の輪番にも入らない。
            # 曜日ごとに勤務時間が決まっている人も同じく、時間をそのまま書く。
            result.assignments[profile.staff_id] = _fixed_hours_assignment(
                profile, days, by_staff_request.get(profile.staff_id)
            )
            reason = (
                "介護補助のため、決まった時間をそのまま入れました(人数計算には数えません)"
                if profile.is_support_staff
                else "曜日ごとの勤務時間が決まっているため、時間をそのまま入れました"
            )
            result.messages.append(f"{profile.name}: {reason}")
        else:
            solver_profiles.append(profile)

    if not solver_profiles:
        result.status = "実行可能"
        return result

    model = cp_model.CpModel()
    x = _create_variables(model, solver_profiles, days)

    _add_one_mark_per_day(model, x, solver_profiles, days)
    _add_availability(model, x, solver_profiles, days)
    _add_night_sequences(model, x, solver_profiles, days)
    _add_coverage(model, x, solver_profiles, days)
    _add_night_composition(model, x, solver_profiles, days)
    _add_fixed_days_off(model, x, solver_profiles, days, by_staff_request)
    _add_monthly_off_quota(model, x, solver_profiles, days)
    _add_weekly_days_off(model, x, solver_profiles, days, by_staff_request)
    _add_max_consecutive_work(model, x, solver_profiles, days)
    pair_terms = _add_pair_constraints(model, x, solver_profiles, days)
    se = _add_responsible(model, x, solver_profiles, days, result)

    penalties = _soft_constraints(model, x, solver_profiles, days, by_staff_request)
    penalties += pair_terms
    model.Minimize(sum(penalties) if penalties else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status == cp_model.OPTIMAL:
        result.status = "最適"
    elif status == cp_model.FEASIBLE:
        result.status = "実行可能"
        result.messages.append(
            "時間内に最良とは限らない解を返しました。時間を延ばすと改善する場合があります。"
        )
    elif status == cp_model.INFEASIBLE:
        result.status = "組めません"
        result.messages.append(
            "条件を全部同時に満たす組み方が存在しません。"
            "毎日の必要人数に対して、入れる人が足りていない可能性があります。"
        )
        return result
    else:
        result.status = "時間切れ"
        result.messages.append("制限時間内に解が見つかりませんでした。時間を延ばしてください。")
        return result

    for profile in solver_profiles:
        assignment: Dict[int, str] = {}
        for day in days:
            for mark in ALL_MARKS:
                if solver.Value(x[profile.staff_id, day.day, mark]):
                    assignment[day.day] = mark
                    break
        result.assignments[profile.staff_id] = assignment

    for (staff_id, day_number), variable in se.items():
        if solver.Value(variable):
            result.responsible[day_number] = staff_id
    return result


# --- 変数 ---------------------------------------------------------------------


def _create_variables(model, profiles, days):
    return {
        (profile.staff_id, day.day, mark): model.NewBoolVar(
            f"{profile.staff_id}_{day.day}_{mark}"
        )
        for profile in profiles
        for day in days
        for mark in ALL_MARKS
    }


# --- ハード制約 ----------------------------------------------------------------


def _add_one_mark_per_day(model, x, profiles, days) -> None:
    """1人1日につき、必ず1つの記号。"""
    for profile in profiles:
        for day in days:
            model.AddExactlyOne(x[profile.staff_id, day.day, mark] for mark in ALL_MARKS)


def _add_availability(model, x, profiles, days) -> None:
    """入れない勤務を禁止する。"""
    for profile in profiles:
        for day in days:
            # 曜日限定の制限も反映する
            allowed = set(profile.shifts_allowed_on(day.weekday))
            for code in DAY_SHIFT_CODES:
                if code not in allowed:
                    model.Add(x[profile.staff_id, day.day, code] == 0)

            if not profile.can_night:
                model.Add(x[profile.staff_id, day.day, NIGHT_IN] == 0)
            if not profile.can_late_night:
                model.Add(x[profile.staff_id, day.day, LATE_NIGHT_IN] == 0)

            # 夜勤に入る曜日が決まっている人
            if profile.night_weekdays and day.weekday not in profile.night_weekdays:
                model.Add(x[profile.staff_id, day.day, NIGHT_IN] == 0)

        # 勤務形態の限定
        if profile.night_shift_exclusive:
            for day in days:
                for code in DAY_SHIFT_CODES:
                    model.Add(x[profile.staff_id, day.day, code] == 0)
                model.Add(x[profile.staff_id, day.day, LATE_NIGHT_IN] == 0)
        if profile.late_night_only:
            for day in days:
                for code in DAY_SHIFT_CODES:
                    model.Add(x[profile.staff_id, day.day, code] == 0)
                model.Add(x[profile.staff_id, day.day, NIGHT_IN] == 0)
        if profile.day_shift_only:
            for day in days:
                model.Add(x[profile.staff_id, day.day, NIGHT_IN] == 0)
                model.Add(x[profile.staff_id, day.day, LATE_NIGHT_IN] == 0)


def _add_night_sequences(model, x, profiles, days) -> None:
    """夜勤は ○→△→公、深夜は ◉→公 の並びを守る。"""
    day_numbers = [day.day for day in days]
    last = day_numbers[-1]

    for profile in profiles:
        staff = profile.staff_id
        for day in day_numbers:
            night = x[staff, day, NIGHT_IN]
            after = x[staff, day, NIGHT_AFTER]

            # 明け(△)は、前日が夜勤(○)のときだけ
            if day == day_numbers[0]:
                # 月初の明けは前月の夜勤による。ここでは付けない
                model.Add(after == 0)
            else:
                model.Add(after == x[staff, day - 1, NIGHT_IN])

            # 夜勤の翌々日は原則として公休。
            # ただし実物には「明けの翌日に次の夜勤」が1件あり、許容されるとのこと。
            # そこで「公休 または 次の夜勤」を認め、公休から外れた分はペナルティで抑える。
            if day + 2 <= last:
                model.AddBoolOr(
                    [
                        night.Not(),
                        x[staff, day + 2, OFF],
                        x[staff, day + 2, NIGHT_IN],
                    ]
                )

            # 深夜(◉)の翌日は公休
            if day + 1 <= last:
                model.AddImplication(x[staff, day, LATE_NIGHT_IN], x[staff, day + 1, OFF])


def _add_coverage(model, x, profiles, days) -> None:
    """毎日の必要人数を満たす。人数は shifts.DAILY_REQUIREMENT に従う。

    番号付きの日勤帯は「ちょうど1人」。実物の勤務表に6種がそろっているかを
    判定する行があり、毎日1人ずつで組まれているため。
    「日」は人数の決まりが無いので、ここでは縛らない。
    """
    for day in days:
        for code in REQUIRED_DAY_SHIFTS:
            model.Add(
                sum(x[profile.staff_id, day.day, code] for profile in profiles)
                == DAILY_REQUIREMENT[code]
            )
        for mark in (NIGHT_IN, LATE_NIGHT_IN):
            model.Add(
                sum(x[profile.staff_id, day.day, mark] for profile in profiles)
                == DAILY_REQUIREMENT[mark]
            )


def _add_night_composition(model, x, profiles, days) -> None:
    """夜間の顔ぶれを決める。

      ○(夜勤) 2人 … 看護職1人 + 介護職1人
      ◉(深夜) 1人 … 原則は介護職。看護職で入れるのは決まった人だけ

    どちらも実物の2026年7月で確かめた(夜勤は31日中30日がこの組み合わせ、
    深夜は介護職90%・看護職10%でその全部が同じ人)。
    """
    nurses = [p for p in profiles if p.is_nurse]
    caregivers = [p for p in profiles if p.is_caregiver]

    for day in days:
        if nurses:
            model.Add(
                sum(x[p.staff_id, day.day, NIGHT_IN] for p in nurses) == 1
            )
        if caregivers:
            model.Add(
                sum(x[p.staff_id, day.day, NIGHT_IN] for p in caregivers) == 1
            )

    # 深夜に入れない看護職を止める
    for profile in profiles:
        if not profile.is_nurse or profile.name in NURSES_ALLOWED_ON_LATE_NIGHT:
            continue
        for day in days:
            model.Add(x[profile.staff_id, day.day, LATE_NIGHT_IN] == 0)


def _add_fixed_days_off(model, x, profiles, days, by_staff_request) -> None:
    """毎週の固定休み・希望休・有給などを固定する。

    有給・夏休・研修・健診は、希望で指定された日にだけ入れる。
    こちらで勝手に割り当てるものではないので、指定が無い日は使わない。
    """
    for profile in profiles:
        request = by_staff_request.get(profile.staff_id)
        wish_off = set(request.wish_off_days()) if request else set()
        absences = request.absence_days() if request else {}

        for day in days:
            if day.day in absences:
                # 有給などが指定された日は、その記号で確定
                model.Add(x[profile.staff_id, day.day, absences[day.day]] == 1)
                continue
            if day.weekday in profile.fixed_off_weekdays or day.day in wish_off:
                model.Add(x[profile.staff_id, day.day, OFF] == 1)

        # 指定の無い日に有給などを勝手に入れない
        for mark in ABSENCE_MARKS:
            for day in days:
                if absences.get(day.day) != mark:
                    model.Add(x[profile.staff_id, day.day, mark] == 0)


def _add_weekly_days_off(model, x, profiles, days, by_staff_request) -> None:
    """週休2日にする。月に何日と数えるのではなく、週ごとに休みを確保する。

    夜勤明けの次の「公」も、この休みに含める(実物がそうなっている)。
    月をまたぐ週は日数が足りないので、その週にある日数に応じて緩める。

    「週◯日勤務」と決まっているパート職員は、休みの数え方が違うので対象にしない。
    """
    for profile in profiles:
        if _has_own_schedule(profile):
            continue
        # 週5日勤務なら休みは2日。それ以外は 7 - 週の勤務日数。
        weekly_work = profile.weekly_work_days or 5
        needed = max(0, 7 - weekly_work)
        if needed == 0:
            continue

        for week in _weeks(days):
            # 月初・月末の半端な週は、その週にある日数に応じて減らす
            quota = needed if len(week) == 7 else (needed * len(week)) // 7
            if quota == 0:
                continue
            model.Add(
                sum(x[profile.staff_id, day.day, OFF] for day in week) >= quota
            )


def _weeks(days):
    """月曜始まりで週に区切る。月初・月末は半端な週になる。"""
    weeks, current = [], []
    for day in days:
        if day.weekday == 0 and current:
            weeks.append(current)
            current = []
        current.append(day)
    if current:
        weeks.append(current)
    return weeks


def _has_own_schedule(profile) -> bool:
    """勤務の形が個別に決まっていて、週休2日や連続勤務の上限を当てはめない人か。

    対象は「曜日ごとに勤務時間が決まっているパート」と「介護補助」。
    work_hours(全日共通の時間枠)だけの人は、番号シフトで普通に働く常勤なので
    含めない。ここを writes_own_hours で判定すると、条件文に
    「6時から17時迄の枠」と書かれた常勤まで外れてしまう。
    """
    return bool(profile.fixed_time_slots or profile.is_support_staff)


def _add_max_consecutive_work(model, x, profiles, days) -> None:
    """連続勤務を5日までにする。

    6日ぶんのどの並びを見ても、必ず1日は休みが入るようにする。
    休みとは「公」と、有給・夏休・研修・健診のこと。
    夜勤明け(△)は勤務が続いているものとして数える(実物がそうなっている)。
    """
    rest_marks = (OFF,) + ABSENCE_MARKS
    window = MAX_CONSECUTIVE_WORK_DAYS + 1

    for profile in profiles:
        if _has_own_schedule(profile):
            continue
        for start in range(len(days) - window + 1):
            chunk = days[start : start + window]
            model.Add(
                sum(
                    x[profile.staff_id, day.day, mark]
                    for day in chunk
                    for mark in rest_marks
                )
                >= 1
            )


def _add_monthly_off_quota(model, x, profiles, days) -> None:
    """「毎月2回は金曜日休み」のような、月内の回数指定。"""
    for profile in profiles:
        for weekday, count in profile.monthly_off_quota.items():
            matching = [day for day in days if day.weekday == weekday]
            if not matching:
                continue
            model.Add(
                sum(x[profile.staff_id, day.day, OFF] for day in matching)
                >= min(count, len(matching))
            )


def _add_pair_constraints(model, x, profiles, days) -> List:
    """同席(同じ夜の ○ と ◉)の制約。絶対厳守。

    ついでに「同じ相手と何度も組まない」ためのペナルティ項も作って返す。
    """
    by_name = {profile.name: profile for profile in profiles}
    penalties: List = []

    for profile in profiles:
        for constraint in profile.pair_constraints:
            other = by_name.get(constraint.other_staff)
            if other is None:
                continue  # 名簿にいない相手。確認事項として別途出している

            together = []
            for day in days:
                both = model.NewBoolVar(f"pair_{profile.staff_id}_{other.staff_id}_{day.day}")
                # どちらが夜勤でどちらが深夜でも「同席」とみなす
                a_night = x[profile.staff_id, day.day, NIGHT_IN]
                a_late = x[profile.staff_id, day.day, LATE_NIGHT_IN]
                b_night = x[other.staff_id, day.day, NIGHT_IN]
                b_late = x[other.staff_id, day.day, LATE_NIGHT_IN]

                model.Add(both >= a_night + b_late - 1)
                model.Add(both >= a_late + b_night - 1)
                model.Add(both <= a_night + a_late)
                model.Add(both <= b_night + b_late)
                together.append(both)

            if constraint.kind == "no_pair_night":
                for both in together:
                    model.Add(both == 0)
            elif constraint.max_count is not None:
                model.Add(sum(together) <= constraint.max_count)

    return penalties


def _add_responsible(model, x, profiles, days, result) -> Dict[Tuple[str, int], object]:
    """責任者「せ」を決める。

    実物の勤務表で確かめたルール:
      - 師長が出勤している日は「せ」を付けない(師長が担うため)。31日中、例外なし
      - 師長が休みの日は、日責ができる人を1人「せ」にする
      - 「せ」になれるのは、その日に出勤している人だけ
    """
    se: Dict[Tuple[str, int], object] = {}
    head = next((p for p in profiles if p.is_head_nurse), None)
    if head is None:
        result.messages.append(
            "師長が設定されていないため、責任者「せ」は決めていません。"
            "確認用Excelの「師長」列で指定してください。"
        )
        return se

    # 日責ができる人。「可」を優先し、いなければ「条件付き可」も使う。
    able = [p for p in profiles if p.day_responsible in ("可", "条件付き可")]
    if not able:
        result.messages.append("日責ができる人がいないため、責任者「せ」は決めていません。")
        return se

    for profile in able:
        for day in days:
            se[profile.staff_id, day.day] = model.NewBoolVar(
                f"se_{profile.staff_id}_{day.day}"
            )

    for day in days:
        # 師長が出勤しているか(公休でなければ出勤とみなす)
        head_working = model.NewBoolVar(f"head_working_{day.day}")
        model.Add(head_working == 1 - x[head.staff_id, day.day, OFF])

        todays = [se[p.staff_id, day.day] for p in able]
        # 師長が休みの日はちょうど1人、出勤の日は0人
        model.Add(sum(todays) == 1 - head_working)

        for profile in able:
            # 休みの人は責任者になれない
            model.Add(se[profile.staff_id, day.day] <= 1 - x[profile.staff_id, day.day, OFF])
    return se


# --- ソフト制約 ----------------------------------------------------------------


def _soft_constraints(model, x, profiles, days, by_staff_request) -> List:
    penalties: List = []
    for profile in profiles:
        staff = profile.staff_id
        request = by_staff_request.get(staff)

        # 希望出勤に応える
        if request:
            for day_number, mark in request.wish_work_days().items():
                if mark not in ALL_MARKS:
                    continue
                if (staff, day_number, mark) not in x:
                    continue
                missed = model.NewBoolVar(f"missed_wish_{staff}_{day_number}")
                model.Add(missed == 1 - x[staff, day_number, mark])
                penalties.append(WEIGHT_WISH_WORK * missed)

        # 夜勤・深夜の希望回数
        for count_range, mark in (
            (profile.night_shift_count, NIGHT_IN),
            (profile.late_night_shift_count, LATE_NIGHT_IN),
        ):
            if not count_range:
                continue
            low, high = count_range
            total = sum(x[staff, day.day, mark] for day in days)
            penalties.extend(
                _range_penalty(model, total, low, high, WEIGHT_NIGHT_COUNT, f"{staff}_{mark}")
            )

        # 週の勤務日数の目安(月あたりに引き伸ばす)
        if profile.weekly_work_days:
            target = round(profile.weekly_work_days * len(days) / 7)
            worked = sum(
                x[staff, day.day, mark]
                for day in days
                for mark in ALL_MARKS
                if mark not in (OFF, NIGHT_AFTER) and mark not in ABSENCE_MARKS
            )
            penalties.extend(
                _range_penalty(model, worked, target, target, WEIGHT_WORK_DAYS, f"{staff}_days")
            )

        # 土日どちらかは休みたい
        if profile.weekend_off_either:
            penalties.extend(_weekend_penalty(model, x, staff, days))

        # 早出には入れていない
        if profile.avoid_early:
            for day in days:
                for code in EARLY_SHIFTS:
                    penalties.append(WEIGHT_AVOID_EARLY * x[staff, day.day, code])

        # 夜勤明けの翌日にまた夜勤、という形をできるだけ避ける
        day_numbers = [day.day for day in days]
        for day in day_numbers:
            if day + 2 > day_numbers[-1]:
                continue
            back_to_back = model.NewBoolVar(f"night_chain_{staff}_{day}")
            model.Add(
                back_to_back
                >= x[staff, day, NIGHT_IN] + x[staff, day + 2, NIGHT_IN] - 1
            )
            penalties.append(WEIGHT_NIGHT_AFTER_NIGHT * back_to_back)

        # 他の人で埋まらないときだけ使う枠(師長の早出・遅出など)
        for code in profile.last_resort_day_shifts:
            if code not in profile.available_day_shifts:
                continue
            for day in days:
                penalties.append(WEIGHT_LAST_RESORT * x[staff, day.day, code])

    return penalties


def _range_penalty(model, total, low: int, high: int, weight: int, name: str) -> List:
    """total が [low, high] から外れた分だけペナルティを付ける。"""
    under = model.NewIntVar(0, 1000, f"under_{name}")
    over = model.NewIntVar(0, 1000, f"over_{name}")
    model.Add(under >= low - total)
    model.Add(under >= 0)
    model.Add(over >= total - high)
    model.Add(over >= 0)
    return [weight * under, weight * over]


def _weekend_penalty(model, x, staff: str, days) -> List:
    """土日が両方とも勤務になっている週にペナルティ。"""
    penalties = []
    saturdays = [day for day in days if day.weekday == 5]
    for saturday in saturdays:
        sunday = next((d for d in days if d.day == saturday.day + 1 and d.weekday == 6), None)
        if sunday is None:
            continue
        both_worked = model.NewBoolVar(f"weekend_{staff}_{saturday.day}")
        model.Add(
            both_worked
            >= 1 - x[staff, saturday.day, OFF] + 1 - x[staff, sunday.day, OFF] - 1
        )
        penalties.append(WEIGHT_WEEKEND_OFF * both_worked)
    return penalties


# --- 時間直書きの人 --------------------------------------------------------------


def _fixed_hours_assignment(
    profile: StaffProfile, days: Sequence[Day], request: Optional[StaffRequests]
) -> Dict[int, str]:
    """曜日ごとに勤務時間が決まっている人の割り当てを、そのまま作る。"""
    by_weekday: Dict[int, List[str]] = {}
    for slot in profile.fixed_time_slots:
        by_weekday.setdefault(slot.weekday, []).append(f"{slot.start}-{slot.end}")

    wish_off = set(request.wish_off_days()) if request else set()
    assignment: Dict[int, str] = {}
    for day in days:
        if day.day in wish_off or day.weekday in profile.fixed_off_weekdays:
            assignment[day.day] = OFF
        elif day.weekday in by_weekday:
            assignment[day.day] = "・".join(by_weekday[day.weekday])
        else:
            assignment[day.day] = OFF
    return assignment
