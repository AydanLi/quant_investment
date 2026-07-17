import pandas as pd

from backtest.engine import Backtester
from config.settings import Config
from data.calendar import NyseCalendar


class _Neutral:
    def classify(self, date, prices, features):
        assert prices.index.max() == date
        return "neutral"


class _ProbeStrategy:
    def __init__(self):
        self.observed = []

    def target_weights(self, date, regime, prices, features):
        self.observed.append((date, prices.index.max()))
        return {"BIL": 1.0}


class _Risk:
    def scale_to_target_vol(self, date, weights, returns):
        assert returns.index.max() == date
        return weights

    def enforce_weight_limits(self, weights):
        return weights

    def pre_trade_check(self, weights):
        return True, "OK"


def test_backtester_slices_all_inputs_at_signal_date():
    index = NyseCalendar().sessions("2023-01-01", "2024-04-30")[:260]
    prices = pd.DataFrame({"SPY": 100.0, "BIL": 91.0}, index=index)
    strategy = _ProbeStrategy()
    Backtester(
        config=Config(
            universe=["SPY", "BIL"],
            rebalance_frequency="D",
            max_asset_weight=1.0,
            min_asset_weight=0.0,
        ),
        prices=prices,
        execution_prices=prices,
        returns=prices.pct_change(fill_method=None),
        features={"future_probe": prices.copy()},
        regime_detector=_Neutral(),
        strategy=strategy,
        risk_engine=_Risk(),
    ).run()

    assert strategy.observed
    assert all(date == maximum for date, maximum in strategy.observed)


def test_non_nyse_session_is_rejected_instead_of_becoming_rebalance_date():
    index = NyseCalendar().sessions("2023-01-01", "2024-04-30")[:260]
    index = index.append(pd.DatetimeIndex([pd.Timestamp("2024-07-04")])).sort_values()
    prices = pd.DataFrame({"SPY": 100.0, "BIL": 91.0}, index=index)

    try:
        Backtester(
            config=Config(universe=["SPY", "BIL"]),
            prices=prices,
            returns=prices.pct_change(fill_method=None),
            features={},
            regime_detector=_Neutral(),
            strategy=_ProbeStrategy(),
            risk_engine=_Risk(),
        ).run()
    except ValueError as exc:
        assert "non-NYSE" in str(exc)
    else:
        raise AssertionError("Expected a VIX-only/non-NYSE date to be rejected.")
