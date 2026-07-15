from datetime import date

from services.experiment_validation import validate_experiment_parameters


def _valid_parameters():
    return {
        "history_limit": 20,
        "scenario_name": "dashboard_manual_run",
        "start_date": "2018-01-01",
        "rebalance_frequency": "M",
        "top_n": 3,
        "min_momentum_threshold": 0.0,
        "target_annual_vol": 0.12,
        "max_asset_weight": 0.4,
        "risk_off_cash_weight": 0.5,
        "vix_risk_off_threshold": 28.0,
        "vix_high_threshold": 22.0,
        "trading_cost_bps": 5.0,
        "slippage_bps": 2.0,
        "today": date(2026, 7, 11),
    }


def test_default_dashboard_parameters_are_valid():
    assert validate_experiment_parameters(**_valid_parameters()) == []


def test_invalid_identity_date_and_frequency_are_reported():
    parameters = _valid_parameters()
    parameters.update(
        scenario_name=" ",
        start_date="07/11/2026",
        rebalance_frequency="Q",
    )

    errors = validate_experiment_parameters(**parameters)

    assert any("Scenario Name" in error for error in errors)
    assert any("YYYY-MM-DD" in error for error in errors)
    assert any("Rebalance Frequency" in error for error in errors)


def test_future_date_and_out_of_range_numbers_are_reported():
    parameters = _valid_parameters()
    parameters.update(
        start_date="2027-01-01",
        history_limit=101,
        top_n=0,
        target_annual_vol=0.0,
        max_asset_weight=1.1,
        risk_off_cash_weight=-0.1,
        trading_cost_bps=31.0,
        slippage_bps=31.0,
    )

    errors = validate_experiment_parameters(**parameters)

    assert any("不能晚于今天" in error for error in errors)
    assert any("读取最近实验数量" in error for error in errors)
    assert any("Top N Assets" in error for error in errors)
    assert any("Target Annual Vol" in error for error in errors)
    assert any("Max Asset Weight" in error for error in errors)
    assert any("Risk-Off Cash Weight" in error for error in errors)
    assert any("Trading Cost" in error for error in errors)
    assert any("Slippage" in error for error in errors)


def test_non_finite_values_and_invalid_vix_order_are_reported():
    parameters = _valid_parameters()
    parameters.update(
        min_momentum_threshold=float("nan"),
        vix_high_threshold=30.0,
        vix_risk_off_threshold=28.0,
    )

    errors = validate_experiment_parameters(**parameters)

    assert any("NaN" in error for error in errors)
    assert any("必须小于" in error for error in errors)


def test_non_numeric_values_return_errors_instead_of_raising():
    parameters = _valid_parameters()
    parameters.update(
        history_limit="many",
        top_n="three",
        vix_high_threshold="high",
        vix_risk_off_threshold="risk-off",
    )

    errors = validate_experiment_parameters(**parameters)

    assert any("读取最近实验数量必须是整数" in error for error in errors)
    assert any("Top N Assets 必须是整数" in error for error in errors)
    assert any("VIX High Threshold 必须是数字" in error for error in errors)
    assert any("VIX Risk-Off Threshold 必须是数字" in error for error in errors)
