from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from services.mirror_optimization import (
    calculate_optimizer_source_fingerprint,
    normalize_cost_basis_weights,
    optimization_result_error,
)
from services.mirror_dashboard import (
    build_admission_gate_table,
    build_allocation_comparison,
    format_result_age,
    format_timestamp_utc,
)
from storage.db import get_engine
from storage.repositories.brokerage_mirror import BrokerageMirrorRepository


OPTIMIZATION_PATH = Path(".runtime/mirror_optimization.json")


def mirror_db_url() -> str:
    return os.environ.get("QUANT_MIRROR_DB_URL", "sqlite:///quant_research.db")


def optimization_path() -> Path:
    return Path(
        os.environ.get(
            "QUANT_MIRROR_OPTIMIZATION_PATH",
            str(OPTIMIZATION_PATH),
        )
    )


def main() -> None:
    st.set_page_config(page_title="Robinhood Read-Only Mirror", layout="wide")
    st.title("Robinhood Read-Only Mirror")
    st.caption(
        "Individual account ••••0908 · Local snapshot only · "
        "Order submission disabled"
    )
    st.info(
        "Read-only research surface. Diagnostic allocations are not trade "
        "instructions and this application cannot submit orders."
    )
    positions = BrokerageMirrorRepository(get_engine(mirror_db_url())).get_latest(
        "robinhood",
        "0908",
    )
    if positions.empty:
        st.error("No Robinhood mirror snapshot is available.")
        return

    optimization = {}
    optimization_error = ""
    result_path = optimization_path()
    if result_path.exists():
        try:
            optimization = json.loads(
                result_path.read_text(encoding="utf-8")
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
    st.caption(
        "Snapshot captured: "
        + format_timestamp_utc(positions["captured_at"].iloc[0])
    )

    if optimization:
        admission_label = "Admitted" if optimization["admitted"] else "Not admitted"
        authorization_label = (
            "Authorized"
            if optimization["position_changes_authorized"]
            else "Blocked"
        )
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Result integrity", "Valid")
        r2.metric(
            "Result age",
            format_result_age(optimization["generated_at"]),
        )
        r3.metric("Admission", admission_label)
        r4.metric("Position changes", authorization_label)
        st.caption(
            "Result generated: "
            f"{format_timestamp_utc(optimization['generated_at'])} · "
            f"Signal date: {optimization['latest_signal_date']} · "
            f"Source: {optimization['source_fingerprint'][:12]}…"
        )
        if not optimization["position_changes_authorized"]:
            st.warning(
                "Position changes are blocked. This result remains diagnostic "
                "until every admission gate passes and separate authorization "
                "is implemented."
            )

    tab1, tab2, tab3 = st.tabs(
        ["Current mirror", "Diagnostic comparison", "Admission audit"]
    )
    with tab1:
        view = positions[
            [
                "symbol",
                "quantity",
                "average_buy_price",
                "shares_available_for_sells",
                "cost_basis",
                "cost_basis_weight",
            ]
        ].sort_values("cost_basis", ascending=False)
        st.dataframe(
            view,
            width="stretch",
            hide_index=True,
            column_config={
                "symbol": "Symbol",
                "quantity": st.column_config.NumberColumn("Quantity", format="%.6f"),
                "average_buy_price": st.column_config.NumberColumn(
                    "Average cost", format="$%.2f"
                ),
                "shares_available_for_sells": st.column_config.NumberColumn(
                    "Sellable", format="%.6f"
                ),
                "cost_basis": st.column_config.NumberColumn(
                    "Recorded cost", format="$%.2f"
                ),
                "cost_basis_weight": st.column_config.ProgressColumn(
                    "Cost-basis weight",
                    min_value=0.0,
                    max_value=1.0,
                    format="%.2%%",
                ),
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
            comparison = build_allocation_comparison(
                positions,
                optimization["latest_weights"],
            )
            st.subheader("Current versus diagnostic allocation")
            st.caption(
                "Diagnostic delta is a research comparison only; it is not a "
                "recommended order or authorized rebalance."
            )
            st.bar_chart(
                comparison.set_index("symbol")[
                    ["current_weight", "diagnostic_target"]
                ]
            )
            st.dataframe(
                comparison,
                width="stretch",
                hide_index=True,
                column_config={
                    "symbol": "Symbol",
                    "current_weight": st.column_config.ProgressColumn(
                        "Current cost-basis weight",
                        min_value=0.0,
                        max_value=1.0,
                        format="%.2%%",
                    ),
                    "diagnostic_target": st.column_config.ProgressColumn(
                        "Diagnostic target",
                        min_value=0.0,
                        max_value=1.0,
                        format="%.2%%",
                    ),
                    "diagnostic_delta": st.column_config.NumberColumn(
                        "Diagnostic delta", format="%+.2%%"
                    ),
                    "absolute_delta": None,
                },
            )
            st.caption(
                f"Selected diagnostic parameters: {best['rebalance_frequency']} "
                f"rebalance · top {best['top_n']} · momentum floor "
                f"{best['min_momentum_threshold']:.2%} · target volatility "
                f"{best['target_annual_vol']:.2%} · maximum asset weight "
                f"{best['max_asset_weight']:.2%}"
            )

    with tab3:
        if not optimization:
            st.info(
                "A valid strict walk-forward result is required before the "
                "admission audit can be displayed."
            )
        else:
            gate_table = build_admission_gate_table(optimization["admission"])
            passed_count = int(gate_table["passed"].sum())
            failed = gate_table.loc[~gate_table["passed"], "gate"].tolist()
            a1, a2, a3 = st.columns(3)
            a1.metric("Gates passed", f"{passed_count}/{len(gate_table)}")
            a2.metric(
                "Selection used holdout",
                str(optimization["admission"]["selection_uses_holdout"]),
            )
            a3.metric(
                "Position changes",
                "Authorized"
                if optimization["position_changes_authorized"]
                else "Blocked",
            )
            st.dataframe(
                gate_table[["gate", "status"]],
                width="stretch",
                hide_index=True,
                column_config={"gate": "Admission gate", "status": "Result"},
            )
            if failed:
                st.warning("Failed gates: " + "; ".join(failed))
            st.caption(
                f"Method: {optimization['methodology']} · "
                f"Selected candidate: "
                f"{optimization['admission']['selected_label']}"
            )


if __name__ == "__main__":
    main()
