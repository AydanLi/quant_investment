from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from services.mirror_optimization import (
    calculate_optimizer_source_fingerprint,
    normalize_cost_basis_weights,
    optimization_result_error,
)
from storage.db import get_engine
from storage.repositories.brokerage_mirror import BrokerageMirrorRepository


OPTIMIZATION_PATH = Path(".runtime/mirror_optimization.json")


def main() -> None:
    st.set_page_config(page_title="Robinhood Read-Only Mirror", layout="wide")
    st.title("Robinhood Read-Only Mirror")
    st.caption("Individual account ••••0908 · Local snapshot only · Order submission disabled")
    positions = BrokerageMirrorRepository(get_engine()).get_latest("robinhood", "0908")
    if positions.empty:
        st.error("No Robinhood mirror snapshot is available.")
        return

    optimization = {}
    optimization_error = ""
    if OPTIMIZATION_PATH.exists():
        try:
            optimization = json.loads(
                OPTIMIZATION_PATH.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            optimization_error = f"Optimization result cannot be read: {exc}"
            optimization = {}
    current_snapshot_id = int(positions["snapshot_id"].iloc[0])
    expected_source_fingerprint = None
    if optimization:
        try:
            expected_source_fingerprint = calculate_optimizer_source_fingerprint(
                Path.cwd()
            )
        except OSError as exc:
            optimization_error = (
                f"Current optimizer source cannot be fingerprinted: {exc}"
            )
            optimization = {}
    validation_error = optimization_result_error(
        optimization,
        current_snapshot_id,
        expected_source_fingerprint=expected_source_fingerprint,
        snapshot_captured_at=positions["captured_at"].iloc[0],
    )
    if validation_error:
        optimization_error = validation_error
        optimization = {}
    if optimization_error:
        st.error(optimization_error)
    positions = positions.copy()
    positions["cost_basis"] = positions["quantity"] * positions["average_buy_price"]
    cost_basis_weights, total_cost, valid_total_cost = normalize_cost_basis_weights(
        positions["cost_basis"]
    )
    positions["cost_basis_weight"] = cost_basis_weights
    if valid_total_cost:
        recorded_cost_label = f"${total_cost:,.2f}"
    else:
        recorded_cost_label = "N/A"
        st.warning(
            "Recorded cost basis is zero or invalid; cost-basis weights are "
            "shown as zero."
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Positions", str(len(positions)))
    c2.metric("Recorded cost basis", recorded_cost_label)
    c3.metric("Snapshot", f"#{current_snapshot_id}")
    c4.metric("Account", "••••0908")

    tab1, tab2 = st.tabs(["Current mirror", "Optimized test allocation"])
    with tab1:
        view = positions[["symbol", "quantity", "average_buy_price", "shares_available_for_sells", "cost_basis", "cost_basis_weight"]].sort_values("cost_basis", ascending=False)
        st.dataframe(
            view,
            width="stretch",
            hide_index=True,
            column_config={
                "symbol": "Symbol",
                "quantity": st.column_config.NumberColumn("Quantity", format="%.6f"),
                "average_buy_price": st.column_config.NumberColumn("Average cost", format="$%.2f"),
                "shares_available_for_sells": st.column_config.NumberColumn("Sellable", format="%.6f"),
                "cost_basis": st.column_config.NumberColumn("Recorded cost", format="$%.2f"),
                "cost_basis_weight": st.column_config.ProgressColumn("Cost-basis weight", min_value=0.0, max_value=1.0, format="%.2%%"),
            },
        )

    with tab2:
        if not optimization:
            st.info(
                "Run the strict mirror optimizer to create a version 2 "
                "walk-forward result."
            )
        else:
            best = optimization["best"]
            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Final holdout CAGR", f"{best['test_cagr']:.2%}")
            b2.metric("Final holdout Sharpe", f"{best['test_sharpe']:.2f}")
            b3.metric(
                "Final holdout drawdown",
                f"{best['test_max_drawdown']:.2%}",
            )
            b4.metric(
                "Final holdout turnover",
                f"{best['test_annual_turnover']:.2f}x",
            )
            target = pd.DataFrame(sorted(optimization["latest_weights"].items(), key=lambda item: item[1], reverse=True), columns=["symbol", "target_weight"])
            st.dataframe(
                target,
                width="stretch",
                hide_index=True,
                column_config={"symbol": "Symbol", "target_weight": st.column_config.ProgressColumn("Test target", min_value=0.0, max_value=1.0, format="%.2%%")},
            )
            st.caption(
                f"Method: {optimization['methodology']} · "
                f"Admitted: {optimization['admitted']}"
            )
            if not optimization.get("position_changes_authorized", False):
                st.warning(
                    "Diagnostic result only. Admission gates do not authorize "
                    "position changes, and this mirror cannot place orders."
                )


if __name__ == "__main__":
    main()
