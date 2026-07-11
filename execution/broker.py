from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd


class MockBroker:
    def __init__(self):
        self.order_log: List[Dict] = []

    def submit_orders(
        self,
        date: pd.Timestamp,
        current_weights: Dict[str, float],
        target_weights: Dict[str, float],
        prices: Optional[pd.Series] = None,
        trading_cost_bps: float = 0.0,
    ) -> List[Dict]:
        orders: List[Dict] = []
        all_tickers = sorted(set(current_weights.keys()).union(target_weights.keys()))
        cost_rate = trading_cost_bps / 10000.0

        for ticker in all_tickers:
            current_w = current_weights.get(ticker, 0.0)
            target_w = target_weights.get(ticker, 0.0)
            delta = target_w - current_w
            if abs(delta) > 1e-4:
                price = None
                if prices is not None:
                    candidate = prices.get(ticker)
                    if candidate is not None and pd.notna(candidate):
                        price = float(candidate)
                orders.append(
                    {
                        "date": date,
                        "ticker": ticker,
                        "side": "BUY" if delta > 0 else "SELL",
                        "weight_change": delta,
                        "price": price,
                        "est_cost": abs(delta) * cost_rate,
                    }
                )

        self.order_log.extend(orders)
        return orders
