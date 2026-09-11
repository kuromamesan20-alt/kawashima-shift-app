"""希望休・希望出勤の入力画面(Streamlit)。

施設の担当者がブラウザで開き、31名分の希望を入力する。
入力内容は Googleスプレッドシートに保存され、勤務表を組むときに読み込む。

  起動: ./venv/bin/python -m streamlit run app.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from kawashima_schedule.calendar_utils import month_days, month_label  # noqa: E402
from kawashima_schedule.profile_store import load_profiles  # noqa: E402
from kawashima_schedule.request_sheet import WISH_CHOICES, StaffRequests  # noqa: E402
from kawashima_schedule.request_storage import get_storage  # noqa: E402
from kawashima_schedule.shifts import (  # noqa: E402
    ABSENCE_MARKS,
    DAY_SHIFTS,
    LATE_NIGHT_IN,
    NIGHT_IN,
    OFF,
)

PROFILES_PATH = Path(__file__).parent / "data/staff_profiles/case001.yaml"
# 空欄はStreamlitが薄い「None」と表示してしまうので、控えめな記号を置く。
# 画面を埋め尽くさないよう短くし、保存時には空欄に戻す。
BLANK = "・"

MEANING = {
    OFF: "休みたい",
    NIGHT_IN: "夜勤に入りたい",
    LATE_NIGHT_IN: "深夜に入りたい",
    "有": "有給休暇",
    "夏": "夏季休暇",
    "研": "研修",
    "健": "健康診断",
}


def main() -> None:
    st.set_page_config(page_title="希望休・希望出勤の入力", layout="wide")
    st.title("希望休・希望出勤の入力")

    profiles = _load_profiles()
    if not profiles:
        st.error(f"スタッフ情報が見つかりません: {PROFILES_PATH}")
        return

    year, month = _pick_month()
    active = [p for p in profiles if not p.is_on_leave(year, month)]

    storage = get_storage(_secrets(), PROFILES_PATH.parent)
    status = storage.status()
    st.caption(f"保存先: {status.provider} — {status.message}")

    saved = {r.staff_id: r for r in storage.load(year, month)}
    days = month_days(year, month)

    st.subheader(f"{month_label(year, month)}（スタッフ {len(active)} 名）")
    _show_legend()

    edited = st.data_editor(
        _to_frame(active, days, saved),
        use_container_width=True,
        height=min(760, 80 + 36 * len(active)),
        column_config=_column_config(days),
        column_order=_column_order(days),
        disabled=["スタッフ"],
        hide_index=True,
        key=f"editor-{year}-{month}-v4",
    )

    left, right = st.columns([1, 4])
    if left.button("保存する", type="primary"):
        requests = _from_frame(edited, active, days)
        storage.save(year, month, requests)
        filled = sum(len(r.entries) for r in requests)
        right.success(f"保存しました（{filled} 件の希望）")

    _show_summary(edited, days)


# --- 部品 ---------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def _load_profiles():
    if not PROFILES_PATH.exists():
        return []
    return load_profiles(PROFILES_PATH)


def _secrets():
    """secrets.toml が無い環境では None を返す(手元で動かすとき)。

    st.secrets は存在しない場合、触れた時点で例外を投げる。
    ここで実際にキーを読んで確かめておく。
    """
    try:
        st.secrets.get("requests_sheet_id")
        return st.secrets
    except Exception:
        return None


def _pick_month():
    today = date.today()
    default_year, default_month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    col1, col2 = st.columns(2)
    year = col1.number_input("年", min_value=2020, max_value=2100, value=default_year, step=1)
    month = col2.number_input("月", min_value=1, max_value=12, value=default_month, step=1)
    return int(year), int(month)


def _show_legend() -> None:
    with st.expander("記号の意味", expanded=False):
        lines = [f"**{code}** {start}-{end} に入りたい" for code, (start, end) in DAY_SHIFTS.items()]
        lines += [f"**{mark}** {text}" for mark, text in MEANING.items()]
        lines.append("**(空欄)** 希望なし。こちらで組みます")
        for chunk in [lines[i::3] for i in range(3)]:
            st.markdown("　/　".join(chunk))
        st.caption("夜勤は ○ → △ → 公 の3日、深夜は ◉ → 公 の2日が続きます。")


def _to_frame(profiles, days, saved) -> pd.DataFrame:
    rows = []
    for profile in profiles:
        entries = saved[profile.staff_id].entries if profile.staff_id in saved else {}
        # スタッフIDは同姓同名の職員を取り違えないための突き合わせ用。画面には出さない。
        row = {"スタッフID": profile.staff_id, "スタッフ": profile.name}
        for day in days:
            row[_column(day)] = entries.get(day.day) or BLANK
        rows.append(row)
    return pd.DataFrame(rows)


def _column(day) -> str:
    return f"{day.day}\n{day.weekday_name}"


def _column_config(days):
    config = {
        "スタッフID": st.column_config.TextColumn("スタッフID"),
        "スタッフ": st.column_config.TextColumn("スタッフ", width="small", pinned=True),
    }
    for day in days:
        label = _column(day)
        config[label] = st.column_config.SelectboxColumn(
            f"{day.day}({day.weekday_name})",
            options=[BLANK] + list(WISH_CHOICES),
            width="small",
            required=False,
        )
    return config


def _column_order(days):
    """画面に表示する列の順番。スタッフID列は突き合わせ専用なので表示しない。"""
    return ["スタッフ"] + [_column(day) for day in days]


def _from_frame(frame, profiles, days):
    # 同姓同名の職員がいても取り違えないよう、名前ではなく staff_id で突き合わせる。
    by_id = {p.staff_id: p for p in profiles}
    requests = []
    for _, row in frame.iterrows():
        profile = by_id.get(row.get("スタッフID"))
        if profile is None:
            continue
        entries = {}
        for day in days:
            mark = str(row.get(_column(day), "") or "").strip()
            if mark and mark != BLANK:
                entries[day.day] = mark
        requests.append(
            StaffRequests(staff_id=profile.staff_id, name=profile.name, entries=entries)
        )
    return requests


def _show_summary(frame, days) -> None:
    columns = [_column(day) for day in days]
    values = frame[columns].values.ravel()
    counts = {}
    for value in values:
        mark = str(value or "").strip()
        if mark and mark != BLANK:
            counts[mark] = counts.get(mark, 0) + 1
    if not counts:
        return
    st.caption(
        "入力済み: "
        + "　".join(f"{mark} {count}件" for mark, count in sorted(counts.items()))
    )


if __name__ == "__main__":
    main()
