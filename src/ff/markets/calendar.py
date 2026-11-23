"""NYSE trading calendar for live use: future sessions and the market clock.

Historical replay takes its sessions from the data itself. The paper-trading loop
also needs to know the *next* session (for month-end rebalancing) and whether the
market is open right now, before any bar exists. Holiday rules follow the NYSE
schedule; early closes (1 pm) are treated as full days, which only matters for
intraday work in Track B.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
    sunday_to_monday,
)

ET = ZoneInfo("America/New_York")


class NYSEHolidays(AbstractHolidayCalendar):
    rules = [  # noqa: RUF012 - pandas API
        Holiday("New Year's Day", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-06-19", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


_CAL = NYSEHolidays()


def holidays(start: pd.Timestamp, end: pd.Timestamp) -> set[pd.Timestamp]:
    return set(_CAL.holidays(start, end))


def is_session(d: pd.Timestamp) -> bool:
    d = pd.Timestamp(d).normalize()
    return d.weekday() < 5 and d not in holidays(d, d)


def next_session(d: pd.Timestamp) -> pd.Timestamp:
    d = pd.Timestamp(d).normalize() + pd.Timedelta(days=1)
    while not is_session(d):
        d += pd.Timedelta(days=1)
    return d


def is_month_end(d: pd.Timestamp) -> bool:
    """True when ``d`` is the last session of its month."""
    return next_session(d).month != pd.Timestamp(d).month


def now_et() -> datetime:
    return datetime.now(ET)


def market_status(now: datetime | None = None) -> dict[str, str]:
    """Same shape the dashboard expects: state, label, reason, et_time."""
    now = (now or now_et()).astimezone(ET)
    today = pd.Timestamp(now.date())
    mins = now.hour * 60 + now.minute
    weekend = today.weekday() >= 5
    holiday = not weekend and not is_session(today)
    state, label = "closed", "Closed"
    if not weekend and not holiday:
        if 570 <= mins < 960:
            state, label = "open", "Open"
        elif 240 <= mins < 570:
            state, label = "pre", "Pre-market"
        elif 960 <= mins < 1200:
            state, label = "post", "After hours"
    reason = "Weekend" if weekend else "Holiday" if holiday else ""
    return {"state": state, "label": label, "reason": reason, "et_time": f"{now:%H:%M} ET"}


def last_final_session(now: datetime | None = None, settle_minutes: int = 30) -> pd.Timestamp:
    """Latest session whose daily bar is final: today only once the close is
    ``settle_minutes`` old, otherwise the previous session."""
    now = (now or now_et()).astimezone(ET)
    d = pd.Timestamp(now.date())
    if is_session(d) and now.hour * 60 + now.minute >= 960 + settle_minutes:
        return d
    d -= pd.Timedelta(days=1)
    while not is_session(d):
        d -= pd.Timedelta(days=1)
    return d
