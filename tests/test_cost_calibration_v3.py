import numpy as np
import pandas as pd
import pytest

from execution.calibration import calibrate_cost_model


def test_cost_calibration_requires_30_fills_and_updates_coefficients_only():
    too_small = pd.DataFrame(
        {"implementation_shortfall_bps": [5.0], "commission": [0.35], "notional": [1000.0]}
    )
    with pytest.raises(ValueError, match="30 paper fills"):
        calibrate_cost_model(too_small)

    fills = pd.DataFrame(
        {
            "implementation_shortfall_bps": np.linspace(1.0, 10.0, 30),
            "commission": np.full(30, 0.35),
            "notional": np.full(30, 1000.0),
        }
    )
    result = calibrate_cost_model(fills)
    assert result.fill_count == 30
    assert result.coefficient_only_update is True
    assert result.median_shortfall_bps <= result.p95_shortfall_bps
    assert result.suggested_all_in_bps > result.median_shortfall_bps
