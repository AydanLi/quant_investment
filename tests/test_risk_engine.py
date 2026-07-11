from config.settings import Config
from risk.engine import RiskEngine


def test_pre_trade_check_valid_weights():
    config = Config()
    engine = RiskEngine(config)
    ok, reason = engine.pre_trade_check({"SPY": 0.3, "QQQ": 0.3, "BIL": 0.4})
    assert ok is True
    assert reason == "OK"


def test_pre_trade_check_invalid_sum():
    config = Config()
    engine = RiskEngine(config)
    ok, reason = engine.pre_trade_check({"SPY": 0.4, "QQQ": 0.4})
    assert ok is False
    assert "Weights do not sum" in reason


def test_enforce_weight_limits_does_not_reexpand_capped_assets():
    engine = RiskEngine(Config(universe=["SPY", "QQQ", "BIL"], max_asset_weight=0.4))

    result = engine.enforce_weight_limits({"SPY": 0.8, "QQQ": 0.2})

    assert abs(sum(result.values()) - 1.0) < 1e-9
    assert abs(result["SPY"] - 0.4) < 1e-9
    assert abs(result["QQQ"] - 0.4) < 1e-9
    assert abs(result["BIL"] - 0.2) < 1e-9


def test_bil_is_exempt_from_risk_asset_cap():
    engine = RiskEngine(Config(max_asset_weight=0.4))

    ok, reason = engine.pre_trade_check({"SPY": 0.4, "BIL": 0.6})

    assert ok is True
    assert reason == "OK"


def test_enforce_weight_limits_preserves_sum_across_target_shapes():
    engine = RiskEngine(Config(max_asset_weight=0.4))
    targets = [
        {"SPY": 1.0},
        {"SPY": 0.5, "QQQ": 0.3, "IWM": 0.2},
        {"SPY": 0.2, "BIL": 0.8},
        {"SPY": 0.3, "QQQ": 0.3, "IWM": 0.4},
    ]

    for target in targets:
        result = engine.enforce_weight_limits(target)
        assert abs(sum(result.values()) - 1.0) < 1e-9
        assert all(
            weight <= engine.config.max_asset_weight + 1e-9
            for ticker, weight in result.items()
            if ticker != "BIL"
        )
        ok, reason = engine.pre_trade_check(result)
        assert ok is True, reason


def test_pre_trade_check_rejects_non_cash_asset_above_cap():
    engine = RiskEngine(Config(max_asset_weight=0.4))

    ok, reason = engine.pre_trade_check({"SPY": 0.5, "BIL": 0.5})

    assert ok is False
    assert "SPY" in reason
    assert "max_asset_weight" in reason


def test_pre_trade_check_rejects_non_finite_weights():
    engine = RiskEngine(Config())

    ok, reason = engine.pre_trade_check({"SPY": float("nan"), "BIL": 1.0})

    assert ok is False
    assert "NaN or infinite" in reason


def test_enforce_weight_limits_raises_when_no_cash_capacity_exists():
    engine = RiskEngine(
        Config(universe=["SPY", "QQQ", "IWM"], top_n=3, max_asset_weight=0.4)
    )

    try:
        engine.enforce_weight_limits({"SPY": 1.0})
    except ValueError as exc:
        assert "infeasible without BIL" in str(exc)
    else:
        raise AssertionError("Expected infeasible target to raise ValueError")


def test_enforce_weight_limits_raises_for_empty_target_without_bil():
    engine = RiskEngine(
        Config(universe=["SPY", "QQQ", "IWM"], top_n=3, max_asset_weight=0.4)
    )

    try:
        engine.enforce_weight_limits({})
    except ValueError as exc:
        assert "zero weights" in str(exc)
    else:
        raise AssertionError("Expected empty target without BIL to raise ValueError")


def test_risk_engine_rejects_infeasible_config_without_bil():
    try:
        RiskEngine(Config(universe=["SPY", "QQQ"], top_n=2, max_asset_weight=0.4))
    except ValueError as exc:
        assert "top_n multiplied by max_asset_weight" in str(exc)
    else:
        raise AssertionError("Expected infeasible risk config to raise ValueError")
