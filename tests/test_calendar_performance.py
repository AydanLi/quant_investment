from time import perf_counter

import pandas as pd

from data.calendar import NyseCalendar


def _expected_period_ends(
    exchange_sessions: pd.DatetimeIndex,
    requested: pd.DatetimeIndex,
    period: str,
) -> pd.DatetimeIndex:
    last_by_period = (
        pd.Series(exchange_sessions, index=exchange_sessions.to_period(period))
        .groupby(level=0)
        .max()
    )
    return pd.DatetimeIndex(last_by_period).intersection(requested)


def test_vectorized_rebalance_sessions_match_nyse_and_finish_under_one_second():
    calendar = NyseCalendar()
    sessions = calendar.sessions("2006-01-01", "2027-01-31")[:5185]
    exchange = calendar.sessions(sessions[0], sessions[-1] + pd.Timedelta(days=14))

    started = perf_counter()
    weekly = calendar.rebalance_sessions(sessions, "W")
    monthly = calendar.rebalance_sessions(sessions, "M")
    elapsed = perf_counter() - started

    pd.testing.assert_index_equal(
        weekly,
        _expected_period_ends(exchange, sessions, "W-SUN"),
    )
    pd.testing.assert_index_equal(
        monthly,
        _expected_period_ends(exchange, sessions, "M"),
    )
    assert elapsed < 1.0


def test_rebalance_generation_uses_one_schedule_lookup(monkeypatch):
    calendar = NyseCalendar()
    sessions = calendar.sessions("2018-01-01", "2026-12-31")
    original = calendar.schedule
    calls = 0

    def counted(start, end):
        nonlocal calls
        calls += 1
        return original(start, end)

    monkeypatch.setattr(calendar, "schedule", counted)

    calendar.rebalance_sessions(sessions, "M")

    assert calls == 1
