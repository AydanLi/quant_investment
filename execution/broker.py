from __future__ import annotations

from typing import Dict, List

import pandas as pd


class MockBroker:
    def __init__(self):
        self.order_log: List[Dict] = []

    def submit_orders(self, date: pd.Timestamp, current_weights: Dict[str, float], target_weights: Dict[str, float]) -> List[Dict]:
        orders: List[Dict] = []
        all_tickers = sorted(set(current_weights.keys()).union(target_weights.keys()))

        for ticker in all_tickers:
            current_w = current_weights.get(ticker, 0.0)
            target_w = target_weights.get(ticker, 0.0)
            delta = target_w - current_w
            if abs(delta) > 1e-4:
                orders.append(
                    {
                        "date": date,
                        "ticker": ticker,
                        "side": "BUY" if delta > 0 else "SELL",
                        "weight_change": delta,
                    }
                )

        self.order_log.extend(orders)
        return orders