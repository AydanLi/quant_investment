from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal


NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class SessionFreshness:
    expected_session: pd.Timestamp
    latest_data_session: pd.Timestamp
    stale_sessions: int


class NyseCalendar:
    """Authoritative NYSE sessions, including exchange holidays and closures."""

    def __init__(self) -> None:
        self._calendar = mcal.get_calendar("NYSE")

    def schedule(self, start: object, end: object) -> pd.DataFrame:
        schedule = self._calendar.schedule(
            start_date=pd.Timestamp(start).date(),
            end_date=pd.Timestamp(end).date(),
        )
        schedule.index = pd.DatetimeIndex(schedule.index).tz_localize(None).normalize()
        return schedule

    def sessions(self, start: object, end: object) -> pd.DatetimeIndex:
        return self.schedule(start, end).index

    def latest_completed_session(self, as_of: pd.Timestamp | None = None) -> pd.Timestamp:
        now = pd.Timestamp.now(tz=NEW_YORK) if as_of is None else pd.Timestamp(as_of)
        if now.tzinfo is None:
            now = now.tz_localize(NEW_YORK)
        else:
            now = now.tz_convert(NEW_YORK)
        schedule = self.schedule(now - pd.Timedelta(days=14), now + pd.Timedelta(days=1))
        close_utc = pd.to_datetime(schedule["market_close"], utc=True)
        completed = schedule.index[close_utc <= now.tz_convert(UTC)]
        if completed.empty:
            raise ValueError("No completed NYSE session is available for the requested time.")
        return pd.Timestamp(completed[-1]).normalize()

    def next_session(self, session: object) -> pd.Timestamp:
        session = pd.Timestamp(session).normalize()
        candidates = self.sessions(session + pd.Timedelta(days=1), session + pd.Timedelta(days=14))
        if candidates.empty:
            raise ValueError(f"No NYSE session found after {session.date()}.")
        return pd.Timestamp(candidates[0])

    def previous_session(self, session: object) -> pd.Timestamp:
        session = pd.Timestamp(session).normalize()
        candidates = self.sessions(
            session - pd.Timedelta(days=14), session - pd.Timedelta(days=1)
        )
        if candidates.empty:
            raise ValueError(f"No NYSE session found before {session.date()}.")
        return pd.Timestamp(candidates[-1])

    def is_session(self, value: object) -> bool:
        value = pd.Timestamp(value).normalize()
        return value in self.sessions(value, value)

    def is_month_end_session(self, session: object) -> bool:
        session = pd.Timestamp(session).normalize()
        if not self.is_session(session):
            return False
        return self.next_session(session).month != session.month

    def next_month_end_session(self, after: object) -> pd.Timestamp:
        after = pd.Timestamp(after).normalize()
        sessions = self.sessions(after, after + pd.DateOffset(months=3))
        for session in sessions:
            if session > after and self.is_month_end_session(session):
                return pd.Timestamp(session)
        raise ValueError(f"No month-end NYSE session found after {after.date()}.")

    def freshness(
        self,
        latest_data_session: object,
        *,
        as_of: pd.Timestamp | None = None,
    ) -> SessionFreshness:
        expected = self.latest_completed_session(as_of)
        latest = pd.Timestamp(latest_data_session).normalize()
        if latest >= expected:
            stale = 0
        else:
            sessions = self.sessions(latest + pd.Timedelta(days=1), expected)
            stale = len(sessions)
        return SessionFreshness(expected, latest, stale)

    @staticmethod
    def after_cutoff(
        as_of: pd.Timestamp | None,
        cutoff: str = "20:30",
    ) -> bool:
        now = pd.Timestamp.now(tz=NEW_YORK) if as_of is None else pd.Timestamp(as_of)
        if now.tzinfo is None:
            now = now.tz_localize(NEW_YORK)
        else:
            now = now.tz_convert(NEW_YORK)
        hour, minute = (int(piece) for piece in cutoff.split(":"))
        return now.timetz().replace(tzinfo=None) >= time(hour, minute)
