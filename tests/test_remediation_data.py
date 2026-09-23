import pandas as pd
import pytest

from backtest.engine import Backtester
from config.settings import Config
from data.calendar import NyseCalendar
from data.features import FeatureEngineer
from data.models import DataQualityReport, ProviderPayload
from data.quality import assess_market_data_quality


def bars(index):
    return pd.DataFrame({"Open": 100., "High": 101., "Low": 99., "Close": 100., "Volume": 1e6}, index=index)


def quality(primary, secondary):
    return assess_market_data_quality(
        ProviderPayload({"SPY": primary}, (), {}, "primary"),
        ProviderPayload({"SPY": secondary}, (), {}, "secondary"),
        required_tickers=["SPY"], config=Config(universe=["SPY"]),
        as_of=pd.Timestamp("2024-01-04 21:00", tz="America/New_York"),
    )


@pytest.mark.parametrize("column,value", [("Open", 1.), ("Open", float("inf")), ("Volume", -1.)])
def test_invalid_trading_fields_cannot_be_trusted(column, value):
    good = bars(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    bad = good.copy()
    bad.loc[bad.index[-1], column] = value
    assert not quality(bad, good).actionable


def test_shared_missing_session_cannot_be_trusted():
    data = bars(pd.to_datetime(["2024-01-02", "2024-01-04"]))
    assert not quality(data, data).actionable


def test_legacy_quality_report_requires_reassessment_without_automatic_upgrade():
    data = bars(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    current = quality(data, data)
    assert current.actionable
    legacy_payload = current.to_dict()
    legacy_payload.pop("quality_model_version", None)
    legacy = DataQualityReport.from_dict(legacy_payload)
    assert not legacy.actionable
    assert legacy.quality_model_version is None
    assert legacy.content_hash == current.content_hash


def test_duplicate_and_nonfinite_close_are_blocked_without_losing_diagnostics():
    data = bars(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    duplicate = pd.concat([data, data.iloc[[-1]]])
    duplicate.iloc[-1, duplicate.columns.get_loc("Close")] = float("inf")
    report = quality(duplicate, data)
    assert not report.actionable
    assert {"DUPLICATE_BAR_DATE", "INVALID_BAR_FIELD"}.issubset({item.code for item in report.issues})


def test_plausible_but_cross_source_wrong_open_is_blocked():
    data = bars(pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    different = data.copy()
    different.iloc[-1, different.columns.get_loc("Open")] = 100.5
    report = quality(different, data)
    assert not report.actionable
    assert "CROSS_SOURCE_OPEN_MISMATCH" in {item.code for item in report.issues}


def test_signal_calendar_preserves_missing_benchmark_day_as_missing():
    data = bars(pd.to_datetime(["2024-01-02", "2024-01-04"]))
    data.attrs["corporate_actions"] = ()
    prices = FeatureEngineer({"SPY": data}, Config(universe=["SPY"])).make_price_frame()
    assert pd.Timestamp("2024-01-03") in prices.index
    assert pd.isna(prices.at[pd.Timestamp("2024-01-03"), "SPY"])


def test_raw_execution_frames_never_select_adjusted_prices():
    data = bars(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    data["Adjusted Open"] = 50.
    data["Adjusted Close"] = 50.
    engineer = FeatureEngineer({"SPY": data}, Config(universe=["SPY"]))
    assert engineer.make_open_frame()["SPY"].eq(100.).all()
    assert engineer.make_raw_close_frame()["SPY"].eq(100.).all()
    assert engineer.make_price_frame()["SPY"].eq(50.).all()


def test_unknown_price_basis_cannot_be_used_as_signal_history():
    data = bars(pd.to_datetime(["2024-01-02", "2024-01-03"]))
    with pytest.raises(ValueError, match="explicit corporate-action"):
        FeatureEngineer({"SPY": data}, Config(universe=["SPY"])).make_price_frame()
    data.attrs["corporate_actions"] = ()
    assert FeatureEngineer({"SPY": data}, Config(universe=["SPY"])).make_price_frame()["SPY"].eq(100.).all()


class Neutral:
    def classify(self, *args):
        return "neutral"


class HoldCash:
    def target_weights(self, *args):
        return {"CASH_USD": 1.}


class PassRisk:
    def scale_to_target_vol(self, date, weights, returns):
        return weights

    def enforce_weight_limits(self, weights):
        return weights

    def pre_trade_check(self, weights):
        return True, "OK"


def test_backtest_rejects_missing_exchange_session():
    index = NyseCalendar().sessions("2023-01-01", "2024-02-05").difference(pd.to_datetime(["2024-02-01"]))
    prices = pd.DataFrame({"SPY": 100., "BIL": 100.}, index=index)
    with pytest.raises(ValueError, match="missing NYSE"):
        Backtester(Config(universe=["SPY", "BIL"]), prices, prices.pct_change(fill_method=None), {}, Neutral(), HoldCash(), PassRisk(), execution_prices=prices).run()
