from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

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
    if OPTIMIZATION_PATH.exists():
        optimization = json.loads(OPTIMIZATION_PATH.read_text(encoding="utf-8"))
    positions = positions.copy()
    positions["cost_basis"] = positions["quantity"] * positions["average_buy_price"]
    total_cost = float(positions["cost_basis"].sum())
    positions["cost_basis_weight"] = positions["cost_basis"] / total_cost

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Positions", str(len(positions)))
    c2.metric("Recorded cost basis", f"${total_cost:,.2f}")
    c3.metric("Snapshot", f"#{int(positions['snapshot_id'].iloc[0])}")
    c4.metric("Account", "••••0908")

    tab1, tab2 = st.tabs(["Current mirror", "Optimized test allocation"])
    with tab1:
        view = positions[["symbol", "quantity", "average_buy_price", "shares_available_for_sells", "cost_basis", "cost_basis_weight"]].sort_values("cost_basis", ascending=False)
        st.dataframe(
            view,
            use_container_width=True,
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
            st.info("Run the mirror optimizer to create optimization results.")
        else:
            best = optimization["best"]
            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Test CAGR", f"{best['test_cagr']:.2%}")
            b2.metric("Test Sharpe", f"{best['test_sharpe']:.2f}")
            b3.metric("Max drawdown", f"{best['test_max_drawdown']:.2%}")
            b4.metric("Annual turnover", f"{best['test_annual_turnover']:.2f}x")
            target = pd.DataFrame(sorted(optimization["latest_weights"].items(), key=lambda item: item[1], reverse=True), columns=["symbol", "target_weight"])
            st.dataframe(
                target,
                use_container_width=True,
                hide_index=True,
                column_config={"symbol": "Symbol", "target_weight": st.column_config.ProgressColumn("Test target", min_value=0.0, max_value=1.0, format="%.2%%")},
            )
            st.warning("Research output only. This mirror cannot place Robinhood orders.")


if __name__ == "__main__":
    main()
