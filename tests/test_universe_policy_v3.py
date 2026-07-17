import numpy as np
import pandas as pd

from config.settings import Config
from config.universe import INITIAL_ETF_UNIVERSE, UniversePolicy
from data.calendar import NyseCalendar
from data.features import FeatureEngineer


def test_initial_universe_contains_25_symbols_and_bil_is_not_ranked_risk_asset():
    assert len(INITIAL_ETF_UNIVERSE) == 25
    assert INITIAL_ETF_UNIVERSE[-1] == "BIL"


def test_point_in_time_eligibility_uses_listing_date_not_prelisting_sessions():
    sessions = NyseCalendar().sessions("2018-01-01", "2024-12-31")
    listed_sessions = sessions[-800:]
    frame = pd.DataFrame(
        {
            "Close": np.full(len(listed_sessions), 100.0),
            "Volume": np.full(len(listed_sessions), 1_000_000.0),
        },
        index=listed_sessions,
    )

    result = UniversePolicy().assess(
        "SPY", frame, as_of=listed_sessions[-1], sessions=sessions
    )

    assert result.eligible is True
    assert result.data_completeness == 1.0


def test_universe_rejects_insufficient_history_low_liquidity_and_leverage():
    sessions = NyseCalendar().sessions("2020-01-01", "2024-12-31")
    frame = pd.DataFrame(
        {"Close": 10.0, "Volume": 1_000.0}, index=sessions[-100:]
    )

    result = UniversePolicy().assess(
        "TEST",
        frame,
        as_of=sessions[-1],
        sessions=sessions,
        metadata={"leveraged_or_inverse": True},
    )

    assert result.eligible is False
    assert {"insufficient_history", "insufficient_dollar_volume", "leveraged_or_inverse"}.issubset(result.reasons)


def test_quarterly_change_only_becomes_effective_next_quarter():
    assert str(UniversePolicy.next_quarter_effective_date(pd.Timestamp("2026-07-17"))) == "2026-10-01"


def test_feature_eligibility_is_frozen_by_quarter_without_history_backfill():
    sessions = NyseCalendar().sessions("2020-01-02", "2024-12-31")[:900]
    frame = pd.DataFrame(
        {
            "Close": np.full(len(sessions), 100.0),
            "Volume": np.full(len(sessions), 500_000.0),
        },
        index=sessions,
    )
    engineer = FeatureEngineer({"SPY": frame}, Config(universe=["SPY"]))
    prices = engineer.make_price_frame()
    eligibility = engineer._universe_eligibility(prices)["SPY"]

    assert eligibility.iloc[:756].eq(False).all()
    first_eligible = eligibility[eligibility].index[0]
    quarter_sessions = sessions[sessions.to_period("Q") == first_eligible.to_period("Q")]
    assert first_eligible == quarter_sessions[0]
    assert sessions.get_loc(first_eligible) >= 756
