from pathlib import Path

import numpy as np
import pandas as pd

from scripts import optimize_mirrored_portfolio as optimizer
from services.mirror_optimization import calculate_optimizer_source_fingerprint


def test_candidate_grid_has_96_unique_predeclared_variants():
    candidates = optimizer._candidate_parameters()

    assert len(candidates) == 96
    assert len(set(candidates)) == 96
    assert all(parameters["top_n"] in {3, 5, 8} for parameters in candidates.values())


def test_dashboard_and_optimizer_source_fingerprints_match():
    project_root = Path(__file__).resolve().parents[1]

    assert calculate_optimizer_source_fingerprint(
        project_root
    ) == optimizer._source_fingerprint(project_root)


def test_universe_filter_uses_only_information_before_validation(monkeypatch):
    index = pd.bdate_range("2021-01-04", "2024-12-31")
    cutoff = pd.Timestamp("2022-12-31")
    pre_validation = index <= cutoff
    columns = {}
    columns[("SPY", "Close")] = np.linspace(100.0, 150.0, len(index))
    columns[("^VIX", "Close")] = np.full(len(index), 20.0)
    columns[("BIL", "Close")] = np.full(len(index), 91.0)
    columns[("EARLY", "Close")] = np.linspace(10.0, 20.0, len(index))

    future = np.full(len(index), np.nan)
    future[~pre_validation] = 50.0
    columns[("FUTURE", "Close")] = future

    low_then_high = np.full(len(index), 4.0)
    low_then_high[~pre_validation] = 100.0
    columns[("LOW_AT_CUTOFF", "Close")] = low_then_high
    raw = pd.DataFrame(columns, index=index)
    monkeypatch.setattr(optimizer.yf, "download", lambda **_kwargs: raw)

    prices, eligible = optimizer._download(
        ["EARLY", "FUTURE", "LOW_AT_CUTOFF"],
        "2021-01-01",
        5.0,
        cutoff,
    )

    assert eligible == ["BIL", "EARLY"]
    assert list(prices.columns) == ["BIL", "EARLY", "SPY", "^VIX"]


def test_local_cache_rejects_incomplete_private_universe():
    class IncompleteRepository:
        def get_close_frame(self, _tickers, start=None):
            index = pd.bdate_range(start, periods=300)
            return pd.DataFrame(
                {
                    "SPY": 100.0,
                    "^VIX": 20.0,
                    "BIL": 91.0,
                },
                index=index,
            )

    with np.testing.assert_raises_regex(
        ValueError,
        "External symbol disclosure remains disabled",
    ):
        optimizer._load_cached(
            IncompleteRepository(),
            ["PRIVATE_HOLDING"],
            "2021-01-01",
            5.0,
            pd.Timestamp("2022-12-31"),
        )
