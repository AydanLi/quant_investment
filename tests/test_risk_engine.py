from config.settings import Config
from risk.engine import RiskEngine


def test_pre_trade_check_valid_weights():
    config = Config()
    engine = RiskEngine(config)
    ok, reason = engine.pre_trade_check({"SPY": 0.5, "QQQ": 0.5})
    assert ok is True
    assert reason == "OK"


def test_pre_trade_check_invalid_sum():
    config = Config()
    engine = RiskEngine(config)
    ok, reason = engine.pre_trade_check({"SPY": 0.4, "QQQ": 0.4})
    assert ok is False
    assert "Weights do not sum" in reason