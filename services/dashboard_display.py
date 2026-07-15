from __future__ import annotations

import pandas as pd


def format_parameter_display_value(value: object) -> str:
    """Return an Arrow-safe string for a Dashboard parameter table cell."""
    if value is None:
        return ""

    if pd.api.types.is_scalar(value):
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass

    return str(value)
