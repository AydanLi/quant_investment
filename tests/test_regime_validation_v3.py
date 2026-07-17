import numpy as np
import pandas as pd
import pytest

from config.settings import Config
from strategy.regime import RegimeDetector


def test_missing_vix_or_benchmark_blocks_regime_classification():
    date = pd.Timestamp("2026-07-16")
    prices = pd.DataFrame({"SPY": [600.0]}, index=[date])

    with pytest.raises(ValueError, match="benchmark and VIX"):
        RegimeDetector(Config()).classify(date, prices, {})


def test_nan_regime_input_is_not_silently_relabelled_neutral():
    date = pd.Timestamp("2026-07-16")
    prices = pd.DataFrame({"SPY": [600.0], "^VIX": [np.nan]}, index=[date])
    features = {
        "ma_200": pd.DataFrame({"SPY": [590.0]}, index=[date]),
        "drawdown_200": pd.DataFrame({"SPY": [0.01]}, index=[date]),
    }

    with pytest.raises(ValueError, match="missing values"):
        RegimeDetector(Config()).classify(date, prices, features)
