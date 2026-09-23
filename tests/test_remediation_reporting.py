"""Regression cases for sample coverage and atomic research persistence."""
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select, func
from dataclasses import replace

from config.settings import Config
from report.reporter import ReportGenerator
from storage.db import create_db_engine, create_all
from storage.schema import experiment_runs, portfolio_daily
from storage.store import ResearchStore


def portfolio():
    dates = pd.bdate_range("2024-01-02", "2025-02-03")
    returns = pd.Series(0.0001, index=dates)
    returns.iloc[-2:] = [-0.1, 0.1]
    return pd.DataFrame({"equity": 10000 * (1 + returns).cumprod(),
                         "daily_return": returns, "turnover": 0.0})


def test_partial_rf_cannot_label_full_period_final():
    frame = portfolio()
    summary = ReportGenerator(Config()).summarize(
        frame, risk_free_returns=pd.Series(0.0002, index=frame.index[-2:]))
    assert summary["Metric Status"] == "RF_INCOMPLETE"
    assert np.isnan(summary["Sharpe"])
    assert summary["Risk-free Coverage"] == pytest.approx(2 / len(frame))


def test_unconfirmed_dividend_cash_flows_cannot_enter_final_metrics():
    frame = portfolio()
    frame["unconfirmed_dividend_payments"] = False
    frame.loc[frame.index[10], "unconfirmed_dividend_payments"] = True
    summary = ReportGenerator(Config()).summarize(
        frame, risk_free_returns=pd.Series(0., index=frame.index))
    assert summary["Metric Status"] == "PROVISIONAL_CASH_FLOWS"
    assert summary["Cash-flow Evidence"] == "UNCONFIRMED_PAYMENT"


def test_incomplete_benchmark_is_not_shortened_silently():
    frame = portfolio()
    summary = ReportGenerator(Config()).summarize(
        frame, benchmark_returns={"SPY": pd.Series(0.1, index=frame.index[-2:])})
    assert np.isnan(summary["Benchmark SPY Total Return"])
    assert summary["Benchmark SPY Status"] == "INCOMPLETE"


def test_incomplete_benchmark_prevents_complete_metric_label_even_with_full_rf():
    frame = portfolio()
    summary = ReportGenerator(Config()).summarize(frame,
        risk_free_returns=pd.Series(0., index=frame.index),
        benchmark_returns={"SPY": pd.Series(.001, index=frame.index[-2:])})
    assert summary["Metric Status"] == "BENCHMARK_INCOMPLETE"


def test_child_save_failure_rolls_back_entire_experiment(monkeypatch):
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    store = ResearchStore(engine=engine)
    frame = portfolio()
    def fail(*args, **kwargs):
        raise RuntimeError("injected order persistence failure")
    monkeypatch.setattr(store.orders, "save", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.save_full_run(scenario_name="atomic", config=Config(),
            summary=ReportGenerator(Config()).summarize(frame), portfolio=frame,
            order_df=pd.DataFrame(), latest_signal={"date": "2025-02-03", "weights": {}})
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(experiment_runs)) == 0
        assert conn.scalar(select(func.count()).select_from(portfolio_daily)) == 0


def test_full_versioned_summary_survives_roundtrip():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    store = ResearchStore(engine=engine)
    summary = ReportGenerator(Config()).summarize(portfolio())
    run_id = store.experiments.save_run(scenario_name="coverage", config=Config(),
        summary=summary, latest_signal={})
    saved = store.experiments.get_run(run_id)["summary_json"]
    assert saved["Metric Schema Version"] == 2
    assert saved["Risk-free Coverage"] == 0.0
    assert saved["Sharpe"] is None
    assert saved["Tax Basis"] == "PRE_TAX"


def test_continuation_metrics_use_actual_opening_nav_and_include_first_loss():
    frame = pd.DataFrame({"equity": [7200., 7200.], "daily_return": [-.2, 0.], "turnover": 0.},
                         index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    frame.attrs["initial_nav"] = 9000.
    summary = ReportGenerator(Config(initial_capital=10000)).summarize(frame)
    assert summary["Start Equity"] == 9000.
    assert summary["Total Return"] == pytest.approx(-.2)
    assert summary["Max Drawdown"] == pytest.approx(-.2)


def test_benchmark_plot_includes_same_opening_nav_and_first_session_return(monkeypatch):
    import matplotlib.pyplot as plt
    plt.switch_backend("Agg")
    frame = pd.DataFrame({"equity": [7200., 7300.]}, index=pd.to_datetime(["2024-01-03", "2024-01-04"]))
    frame.attrs["initial_nav"] = 9000.
    benchmark = pd.Series([125., 100., 110.], index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]))
    monkeypatch.setattr(plt, "show", lambda: None)
    ReportGenerator(Config()).plot(frame, benchmark)
    lines = plt.gca().get_lines()
    assert list(lines[0].get_ydata()) == [9000., 7200., 7300.]
    assert list(lines[1].get_ydata()) == pytest.approx([9000., 7200., 7920.])
    plt.close("all")


def test_invalid_incomplete_and_legacy_runs_never_enter_valid_comparison():
    from services.dashboard_display import eligible_comparison_runs
    frame = pd.DataFrame([
        {"id": 1, "admissible": 0, "status": "invalid_data_v1", "summary_json": {"Metric Status": "FINAL"}},
        {"id": 2, "admissible": 1, "status": "complete", "summary_json": {"Metric Status": "RF_INCOMPLETE"}},
        {"id": 3, "admissible": 1, "status": "complete", "summary_json": None},
        {"id": 4, "admissible": 1, "status": "complete", "runtime_verified": True, "summary_json": {"Metric Status": "FINAL", "Metric Schema Version": 2}},
    ])
    assert eligible_comparison_runs(frame)["id"].tolist() == [4]


def test_experiment_approval_binds_actual_economics_but_allows_new_market_snapshot():
    from tests.test_paper_cycle import _engine, _insert_snapshot
    from storage.schema import admission_runs, strategy_versions
    engine = _engine()
    with engine.begin() as connection:
        runtime_hash = connection.scalar(select(strategy_versions.c.runtime_hash))
        connection.execute(admission_runs.update().values(runtime_hash=runtime_hash))
        _insert_snapshot(connection, snapshot_id=2, session="2026-08-03")
    store = ResearchStore(engine=engine)
    original = Config(strategy_version="SV-001")
    def save(config):
        run_id = store.experiments.save_run(scenario_name="runtime", config=config,
            summary=pd.Series({"Metric Schema Version": 2, "Metric Status": "FINAL"}),
            latest_signal={}, dataset_snapshot_id=2)
        return store.experiments.get_run(run_id)
    assert save(original)["admissible"] == 1
    changed = save(replace(original, trading_cost_bps=0))
    assert changed["admissible"] == 0
    assert "trading_cost_bps" in changed["invalidated_reason"]
    with engine.begin() as connection:
        connection.execute(strategy_versions.update().values(runtime_manifest_json=None, runtime_hash=None))
    legacy = save(original)
    assert legacy["admissible"] == 0
    assert "legacy" in legacy["invalidated_reason"]


def test_experiment_cannot_admit_snapshot_without_immutable_content_identity():
    from tests.test_paper_cycle import _engine
    from storage.schema import admission_runs, dataset_snapshots, strategy_versions
    engine = _engine()
    with engine.begin() as conn:
        runtime_hash = conn.scalar(select(strategy_versions.c.runtime_hash))
        conn.execute(admission_runs.update().values(runtime_hash=runtime_hash))
        conn.execute(dataset_snapshots.update().values(content_hash=""))
    store = ResearchStore(engine=engine)
    run_id = store.experiments.save_run(scenario_name="missing-content", config=Config(strategy_version="SV-001"),
        summary=pd.Series({"Metric Schema Version": 2, "Metric Status": "FINAL"}), latest_signal={}, dataset_snapshot_id=1)
    saved = store.experiments.get_run(run_id)
    assert saved["admissible"] == 0
    assert "not actionable" in saved["invalidated_reason"]
