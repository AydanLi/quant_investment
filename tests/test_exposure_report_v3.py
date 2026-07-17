import numpy as np
import pandas as pd

from risk.exposure import analyze_exposure


def test_exposure_report_shows_asset_class_and_correlation_concentration():
    index = pd.bdate_range("2024-01-01", periods=60)
    returns = pd.DataFrame(
        {
            "SPY": np.linspace(-0.01, 0.01, len(index)),
            "QQQ": np.linspace(-0.009, 0.011, len(index)),
        },
        index=index,
    )

    report = analyze_exposure(
        {"SPY": 0.30, "QQQ": 0.20, "BIL": 0.50}, returns
    )

    assert report.risky_exposure == 0.5
    assert report.cash_exposure == 0.5
    assert report.largest_risky_position == 0.3
    assert report.asset_class_exposures["US_EQUITY"] == 0.5
    assert report.correlation_concentration > 0.9
    assert report.effective_independent_bets < 1.2
