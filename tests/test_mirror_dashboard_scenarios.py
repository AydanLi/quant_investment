from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from services.mirror_optimization import calculate_optimizer_source_fingerprint
from storage.db import create_all, create_db_engine
from storage.repositories.brokerage_mirror import BrokerageMirrorRepository


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PATH = PROJECT_ROOT / "robinhood_mirror_dashboard.py"
GATES = {
    "same_interval_same_costs": True,
    "holdout_sharpe": True,
    "maximum_drawdown": True,
    "rolling_windows": False,
    "after_costs": False,
    "parameter_robustness": False,
    "start_date_robustness": False,
    "independent_information": False,
    "historical_universe_integrity": False,
}


def _save_snapshot(
    db_url: str,
    *,
    captured_at: datetime,
    average_buy_price: float | None = 100.0,
) -> int:
    engine = create_db_engine(db_url)
    create_all(engine)
    snapshot_id = BrokerageMirrorRepository(engine=engine).save_snapshot(
        provider="robinhood",
        account_ref="0908",
        account_type="individual",
        captured_at=captured_at,
        positions=[
            {
                "symbol": "SPY",
                "quantity": 10.0,
                "average_buy_price": average_buy_price,
                "shares_available_for_sells": 10.0,
                "type": "long",
            }
        ],
    )
    engine.dispose()
    return snapshot_id


def _valid_result(
    snapshot_id: int,
    *,
    generated_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "methodology": "expanding_walk_forward_with_untouched_holdout",
        "generated_at": generated_at.isoformat(),
        "source_fingerprint": calculate_optimizer_source_fingerprint(PROJECT_ROOT),
        "mirror_snapshot_id": snapshot_id,
        "best": {
            "rebalance_frequency": "M",
            "top_n": 3,
            "min_momentum_threshold": 0.03,
            "target_annual_vol": 0.10,
            "max_asset_weight": 0.25,
            "risk_off_cash_weight": 0.30,
            "test_cagr": 0.12,
            "test_sharpe": 1.3,
            "test_max_drawdown": -0.07,
            "test_annual_turnover": 5.0,
        },
        "admission": {
            "selection_uses_holdout": False,
            "selected_label": "isolated_test_candidate",
            "gates": dict(GATES),
            "admitted": False,
            "position_changes_authorized": False,
        },
        "admitted": False,
        "position_changes_authorized": False,
        "latest_signal_date": generated_at.date().isoformat(),
        "latest_weights": {"BIL": 0.75, "SPY": 0.25},
    }


def _run_app(
    monkeypatch,
    tmp_path,
    *,
    result: dict[str, object] | None = None,
    raw_result: str | None = None,
    create_snapshot: bool = True,
    captured_at: datetime | None = None,
    average_buy_price: float | None = 100.0,
) -> AppTest:
    database_path = tmp_path / "mirror.db"
    db_url = f"sqlite:///{database_path.as_posix()}"
    result_path = tmp_path / "mirror-result.json"
    captured_at = captured_at or datetime.now(timezone.utc) - timedelta(hours=1)
    if create_snapshot:
        _save_snapshot(
            db_url,
            captured_at=captured_at,
            average_buy_price=average_buy_price,
        )
    else:
        engine = create_db_engine(db_url)
        create_all(engine)
        engine.dispose()

    if raw_result is not None:
        result_path.write_text(raw_result, encoding="utf-8")
    elif result is not None:
        result_path.write_text(json.dumps(result), encoding="utf-8")

    monkeypatch.setenv("QUANT_MIRROR_DB_URL", db_url)
    monkeypatch.setenv("QUANT_MIRROR_OPTIMIZATION_PATH", str(result_path))
    return AppTest.from_file(str(DASHBOARD_PATH), default_timeout=60).run()


def _assert_read_only(at: AppTest) -> None:
    assert len(at.exception) == 0
    assert len(at.button) == 0
    source = DASHBOARD_PATH.read_text(encoding="utf-8")
    assert "execution." not in source
    assert "submit_order" not in source
    assert "place_order" not in source
    assert "st.form_submit_button" not in source


def _messages(elements) -> list[str]:
    return [str(element.value) for element in elements]


def _metrics(at: AppTest) -> dict[str, str]:
    return {metric.label: str(metric.value) for metric in at.metric}


def test_valid_schema_v2_result_renders_full_read_only_audit(monkeypatch, tmp_path):
    captured_at = datetime.now(timezone.utc) - timedelta(hours=1)
    snapshot_id = _save_snapshot(
        f"sqlite:///{(tmp_path / 'seed.db').as_posix()}",
        captured_at=captured_at,
    )
    result = _valid_result(
        snapshot_id,
        generated_at=captured_at + timedelta(minutes=30),
    )
    at = _run_app(
        monkeypatch,
        tmp_path,
        result=result,
        captured_at=captured_at,
    )

    _assert_read_only(at)
    assert len(at.error) == 0
    assert [tab.label for tab in at.tabs] == [
        "Current mirror",
        "Diagnostic comparison",
        "Admission audit",
    ]
    metrics = _metrics(at)
    assert metrics["Result integrity"] == "Valid"
    assert metrics["Admission"] == "Not admitted"
    assert metrics["Position changes"] == "Blocked"
    assert metrics["Gates passed"] == "3/9"
    assert len(at.dataframe) == 3


def test_no_result_degrades_to_instructions_without_exception(monkeypatch, tmp_path):
    at = _run_app(monkeypatch, tmp_path)

    _assert_read_only(at)
    assert len(at.error) == 0
    info_messages = _messages(at.info)
    assert any("Run the strict mirror optimizer" in message for message in info_messages)
    assert any("admission audit" in message for message in info_messages)
    assert "Result integrity" not in _metrics(at)


@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        ("malformed", "Optimization result cannot be read"),
        ("legacy", "Legacy single-split"),
        ("snapshot", "different mirror snapshot"),
        ("source", "different source code"),
        ("stale", "older than the allowed freshness window"),
    ],
)
def test_invalid_results_are_blocked_without_app_exception(
    monkeypatch,
    tmp_path,
    case,
    expected_message,
):
    if case == "stale":
        captured_at = datetime.now(timezone.utc) - timedelta(days=10)
        generated_at = datetime.now(timezone.utc) - timedelta(days=8)
    else:
        captured_at = datetime.now(timezone.utc) - timedelta(hours=1)
        generated_at = captured_at + timedelta(minutes=30)
    database_path = tmp_path / "id-source.db"
    snapshot_id = _save_snapshot(
        f"sqlite:///{database_path.as_posix()}",
        captured_at=captured_at,
    )
    result = _valid_result(snapshot_id, generated_at=generated_at)
    raw_result = None
    if case == "malformed":
        raw_result = "{not-json"
        result = None
    elif case == "legacy":
        result.pop("schema_version")
    elif case == "snapshot":
        result["mirror_snapshot_id"] = snapshot_id + 100
    elif case == "source":
        result["source_fingerprint"] = "0" * 64

    at = _run_app(
        monkeypatch,
        tmp_path,
        result=result,
        raw_result=raw_result,
        captured_at=captured_at,
    )

    _assert_read_only(at)
    assert any(expected_message in message for message in _messages(at.error))
    assert "Result integrity" not in _metrics(at)
    assert len(at.dataframe) == 1


def test_missing_snapshot_stops_before_result_or_tabs(monkeypatch, tmp_path):
    at = _run_app(monkeypatch, tmp_path, create_snapshot=False)

    _assert_read_only(at)
    assert _messages(at.error) == ["No Robinhood mirror snapshot is available."]
    assert len(at.tabs) == 0
    assert len(at.metric) == 0


@pytest.mark.parametrize("average_buy_price", [0.0, None])
def test_zero_or_unknown_cost_basis_uses_safe_degraded_weights(
    monkeypatch,
    tmp_path,
    average_buy_price,
):
    captured_at = datetime.now(timezone.utc) - timedelta(hours=1)
    database_path = tmp_path / "cost-source.db"
    snapshot_id = _save_snapshot(
        f"sqlite:///{database_path.as_posix()}",
        captured_at=captured_at,
        average_buy_price=average_buy_price,
    )
    result = _valid_result(
        snapshot_id,
        generated_at=captured_at + timedelta(minutes=30),
    )
    at = _run_app(
        monkeypatch,
        tmp_path,
        result=result,
        captured_at=captured_at,
        average_buy_price=average_buy_price,
    )

    _assert_read_only(at)
    assert len(at.error) == 0
    assert _metrics(at)["Recorded cost basis"] == "N/A"
    assert any(
        "cost basis is zero or invalid" in message
        for message in _messages(at.warning)
    )
    comparison = at.dataframe[1].value.set_index("symbol")
    assert comparison.at["SPY", "current_weight"] == pytest.approx(0.0)
