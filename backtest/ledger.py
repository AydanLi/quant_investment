from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import numpy as np
import pandas as pd

from config.settings import Config
from data.calendar import NyseCalendar


@dataclass
class CashSettlement:
    settlement_date: pd.Timestamp
    amount: float


@dataclass
class PortfolioLedger:
    config: Config
    quantities: dict[str, float] = field(default_factory=dict)
    average_costs: dict[str, float] = field(default_factory=dict)
    settled_cash_balance: float = 0.0
    settlement_queue: list[CashSettlement] = field(default_factory=list)
    order_log: list[dict[str, object]] = field(default_factory=list)
    calendar: NyseCalendar = field(default_factory=NyseCalendar, repr=False)

    @classmethod
    def initialize(
        cls,
        config: Config,
        *,
        session: pd.Timestamp,
        prices: pd.Series,
    ) -> "PortfolioLedger":
        ledger = cls(config=config, settled_cash_balance=float(config.initial_capital))
        cash_asset = config.cash_asset
        price = prices.get(cash_asset)
        if cash_asset in config.universe and price is not None and pd.notna(price) and float(price) > 0:
            ledger.quantities[cash_asset] = ledger.settled_cash_balance / float(price)
            ledger.average_costs[cash_asset] = float(price)
            ledger.settled_cash_balance = 0.0
        return ledger

    def settle(self, session: pd.Timestamp) -> None:
        session = pd.Timestamp(session).normalize()
        pending: list[CashSettlement] = []
        for item in self.settlement_queue:
            if item.settlement_date <= session:
                self.settled_cash_balance += item.amount
            else:
                pending.append(item)
        self.settlement_queue = pending
        if self.settled_cash_balance < -1e-6:
            raise ValueError("Settled cash became negative; cash-account invariant failed.")

    @property
    def unsettled_cash(self) -> float:
        return float(sum(item.amount for item in self.settlement_queue))

    @property
    def cash(self) -> float:
        """Total cash claim: settled balance plus signed T+1 settlements."""
        return float(self.settled_cash_balance + self.unsettled_cash)

    def mark(self, prices: pd.Series) -> float:
        value = float(self.cash)
        for ticker, quantity in self.quantities.items():
            price = prices.get(ticker)
            if price is None or pd.isna(price) or float(price) <= 0.0:
                raise ValueError(f"Missing valid mark price for active holding {ticker}.")
            value += quantity * float(price)
        if not np.isfinite(value) or value <= 0.0:
            raise ValueError("Portfolio NAV is non-finite or non-positive.")
        return value

    def weights(self, prices: pd.Series) -> dict[str, float]:
        nav = self.mark(prices)
        result = {
            ticker: quantity * float(prices[ticker]) / nav
            for ticker, quantity in self.quantities.items()
            if abs(quantity) > 1e-12
        }
        cash_weight = self.cash / nav
        if cash_weight > 1e-12:
            result[self.config.synthetic_cash_asset] = cash_weight
        return result

    def rebalance(
        self,
        *,
        signal_date: pd.Timestamp,
        execution_date: pd.Timestamp,
        target_weights: Mapping[str, float],
        prices: pd.Series,
        median_dollar_volume: pd.Series | None = None,
        risk_off: bool = False,
    ) -> dict[str, float]:
        if abs(sum(target_weights.values()) - 1.0) > 1e-6:
            raise ValueError("Target weights must sum to 1 before execution.")
        open_nav = self.mark(prices)
        buffer = max(
            self.config.operational_cash_buffer_min,
            open_nav * self.config.operational_cash_buffer_pct,
        )
        synthetic_cash_weight = float(
            target_weights.get(self.config.synthetic_cash_asset, 0.0)
        )
        investable_before_cost = max(
            open_nav * (1.0 - synthetic_cash_weight) - buffer,
            0.0,
        )

        current_values = {
            ticker: quantity * float(prices[ticker])
            for ticker, quantity in self.quantities.items()
        }

        def desired_values(investable: float) -> dict[str, float]:
            values: dict[str, float] = {}
            for ticker, weight in target_weights.items():
                if ticker == self.config.synthetic_cash_asset or weight <= 0.0:
                    continue
                price = prices.get(ticker)
                if price is None or pd.isna(price) or float(price) <= 0.0:
                    raise ValueError(f"Missing valid T+1 execution price for {ticker}.")
                values[ticker] = float(weight) * investable / max(
                    1.0 - synthetic_cash_weight, 1e-12
                )
            return values

        first_desired = desired_values(investable_before_cost)
        first_deltas = {
            ticker: abs(first_desired.get(ticker, 0.0) - current_values.get(ticker, 0.0))
            for ticker in set(first_desired).union(current_values)
        }
        trading_rate = self.config.trading_cost_bps / 10000.0
        slippage_rate = self.config.slippage_bps / 10000.0

        def cost_components(ticker: str, notional: float) -> tuple[float, float, float, float]:
            trading = notional * trading_rate
            slippage = notional * slippage_rate
            if risk_off:
                floor_rate = 20.0 / 10000.0
                slippage += max(notional * floor_rate - trading - slippage, 0.0)
            impact = 0.0
            adv_fraction = float("nan")
            if median_dollar_volume is not None:
                adv = median_dollar_volume.get(ticker)
                if adv is None or pd.isna(adv) or float(adv) <= 0.0:
                    raise ValueError(f"Missing positive trailing ADV for {ticker}.")
                adv_fraction = notional / float(adv)
                if adv_fraction > self.config.maximum_order_adv + 1e-12:
                    raise ValueError(f"{ticker} historical order exceeds 1% ADV.")
                if adv_fraction >= self.config.impact_model_adv_threshold:
                    impact_bps = self.config.impact_coefficient_bps * np.sqrt(
                        adv_fraction / self.config.impact_model_adv_threshold
                    )
                    impact = notional * impact_bps / 10000.0
            return trading, slippage, impact, adv_fraction

        estimated_cost = sum(
            sum(cost_components(ticker, notional)[:3])
            for ticker, notional in first_deltas.items()
            if notional > 0.0
        )
        desired = desired_values(max(investable_before_cost - estimated_cost, 0.0))

        orders: list[dict[str, object]] = []
        total_notional = 0.0
        sale_proceeds = 0.0
        all_tickers = sorted(set(desired).union(self.quantities))
        for ticker in all_tickers:
            price = prices.get(ticker)
            if price is None or pd.isna(price) or float(price) <= 0.0:
                raise ValueError(f"Missing valid T+1 execution price for {ticker}.")
            price = float(price)
            current_quantity = self.quantities.get(ticker, 0.0)
            target_quantity = desired.get(ticker, 0.0) / price
            quantity_change = target_quantity - current_quantity
            notional = abs(quantity_change) * price
            if notional <= max(open_nav * 1e-8, 1e-6):
                continue
            side = "BUY" if quantity_change > 0 else "SELL"
            cash_flow = -quantity_change * price
            if side == "SELL":
                sale_proceeds += cash_flow
            self.quantities[ticker] = target_quantity
            total_notional += notional
            orders.append(
                {
                    "signal_date": pd.Timestamp(signal_date),
                    "date": pd.Timestamp(execution_date),
                    "ticker": ticker,
                    "side": side,
                    "quantity": abs(quantity_change),
                    "quantity_change": quantity_change,
                    "current_quantity": current_quantity,
                    "target_quantity": target_quantity,
                    "notional": notional,
                    "price": price,
                    "average_entry_cost": self.average_costs.get(ticker),
                    "weight_change": (
                        desired.get(ticker, 0.0) - current_values.get(ticker, 0.0)
                    ) / open_nav,
                }
            )

        trading_cost = 0.0
        slippage = 0.0
        impact_cost = 0.0
        maximum_adv_fraction = float("nan")
        for order in orders:
            components = cost_components(str(order["ticker"]), float(order["notional"]))
            order_trading, order_slippage, order_impact, adv_fraction = components
            trading_cost += order_trading
            slippage += order_slippage
            impact_cost += order_impact
            if np.isfinite(adv_fraction):
                maximum_adv_fraction = (
                    adv_fraction
                    if not np.isfinite(maximum_adv_fraction)
                    else max(maximum_adv_fraction, adv_fraction)
                )
            order["trading_cost_dollars"] = order_trading
            order["slippage_dollars"] = order_slippage
            order["impact_cost_dollars"] = order_impact
            order["adv_fraction"] = adv_fraction
            order_cost = order_trading + order_slippage + order_impact
            ticker = str(order["ticker"])
            current_quantity = float(order["current_quantity"])
            target_quantity = float(order["target_quantity"])
            if order["side"] == "BUY":
                old_average = float(order["average_entry_cost"] or 0.0)
                total_basis = current_quantity * old_average + float(
                    order["notional"]
                ) + order_cost
                if target_quantity <= 0.0:
                    raise ValueError("Buy order produced a non-positive target quantity.")
                new_average = total_basis / target_quantity
                self.average_costs[ticker] = new_average
                order["average_entry_cost"] = new_average
                order["gross_realized_pnl"] = None
                order["realized_pnl"] = None
            else:
                entry_cost = order["average_entry_cost"]
                if entry_cost is None:
                    raise ValueError(f"Missing cost basis for sell order {ticker}.")
                gross_realized = (
                    float(order["price"]) - float(entry_cost)
                ) * float(order["quantity"])
                order["gross_realized_pnl"] = gross_realized
                order["realized_pnl"] = gross_realized - order_cost
                if target_quantity <= 1e-12:
                    self.average_costs.pop(ticker, None)
        total_cost = trading_cost + slippage + impact_cost
        net_settlement = sale_proceeds - sum(
            float(order["notional"])
            for order in orders
            if order["side"] == "BUY"
        ) - total_cost
        if self.cash + net_settlement < -1e-6:
            raise ValueError("Cash-account execution would create a negative cash balance.")
        self.quantities = {
            ticker: quantity
            for ticker, quantity in self.quantities.items()
            if abs(quantity) > 1e-12
        }
        if abs(net_settlement) > 1e-12:
            self.settlement_queue.append(
                CashSettlement(
                    settlement_date=self.calendar.next_session(execution_date),
                    amount=net_settlement,
                )
            )
        for order in orders:
            order["est_trading_cost"] = float(order["trading_cost_dollars"]) / open_nav
            order["est_slippage"] = float(order["slippage_dollars"]) / open_nav
            order["est_impact"] = float(order["impact_cost_dollars"]) / open_nav
            order["est_cost"] = (
                float(order["trading_cost_dollars"])
                + float(order["slippage_dollars"])
                + float(order["impact_cost_dollars"])
            ) / open_nav
        self.order_log.extend(orders)
        return {
            "turnover": total_notional / open_nav,
            "est_trading_cost": trading_cost / open_nav,
            "est_slippage": slippage / open_nav,
            "est_impact": impact_cost / open_nav,
            "est_cost": total_cost / open_nav,
            "cost_dollars": total_cost,
            "net_cash_settlement": net_settlement,
            "maximum_adv_fraction": maximum_adv_fraction,
        }
