import numpy as np
import pandas as pd

from config.settings import Config
from risk.covariance import DynamicFactorRiskModel
from risk.engine import RiskEngine


def _correlated_returns(seed=7):
    generator = np.random.default_rng(seed)
    market = generator.normal(0.0, 0.01, 140)
    values = np.column_stack(
        [
            market + generator.normal(0.0, 0.003, 140),
            0.8 * market + generator.normal(0.0, 0.004, 140),
            generator.normal(0.0, 0.008, 140),
        ]
    )
    return pd.DataFrame(values, columns=["SPY", "QQQ", "TLT"])


def test_dynamic_factor_covariance_is_finite_symmetric_and_positive_semidefinite():
    estimate = DynamicFactorRiskModel().estimate(_correlated_returns())

    assert estimate.observations == 120
    assert 0.0 < estimate.first_factor_share <= 1.0
    assert np.isfinite(estimate.covariance).all()
    assert np.allclose(estimate.covariance, estimate.covariance.T)
    assert np.linalg.eigvalsh(estimate.covariance).min() >= -1e-12


def test_pca_stress_increases_dominant_factor_variance():
    returns = _correlated_returns()
    unstressed = DynamicFactorRiskModel(pca_stress_multiplier=1.0).estimate(
        returns
    )
    stressed = DynamicFactorRiskModel(pca_stress_multiplier=1.5).estimate(returns)

    assert np.linalg.eigvalsh(stressed.covariance).max() > np.linalg.eigvalsh(
        unstressed.covariance
    ).max()


def test_risk_engine_supports_reproducible_sample_baseline_and_dynamic_model():
    date = pd.Timestamp("2025-01-31")
    returns = _correlated_returns().set_axis(
        pd.bdate_range(end=date, periods=140), axis="index"
    )
    raw = {"SPY": 0.5, "QQQ": 0.5}
    sample = RiskEngine(
        Config(
            universe=["SPY", "QQQ", "BIL"],
            max_asset_weight=1.0,
            risk_model="sample",
        )
    ).scale_to_target_vol(date, raw, returns)
    dynamic = RiskEngine(
        Config(
            universe=["SPY", "QQQ", "BIL"],
            max_asset_weight=1.0,
            risk_model="dynamic_factor",
        )
    ).scale_to_target_vol(date, raw, returns)

    assert abs(sum(sample.values()) - 1.0) < 1e-12
    assert abs(sum(dynamic.values()) - 1.0) < 1e-12
    assert dynamic.get("BIL", 0.0) >= sample.get("BIL", 0.0)


def test_invalid_dynamic_factor_settings_are_rejected():
    for config in [
        Config(risk_model="unknown"),
        Config(ewma_half_life_days=0),
        Config(pca_stress_multiplier=0.9),
        Config(slippage_bps=-1.0),
    ]:
        try:
            RiskEngine(config)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected invalid risk-model configuration to fail.")
