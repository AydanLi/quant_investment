"""Pure, shared cash-account order sizing for historical and paper execution."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Mapping

from config.settings import Config
from execution.validation import finite_number


@dataclass(frozen=True)
class PlannedOrder:
    ticker: str
    side: str
    quantity: float
    reference_price: float
    price: float
    commission: float
    slippage: float
    impact: float
    adv_fraction: float
    estimated_impact_bps: float

    @property
    def notional(self) -> float:
        return self.quantity * self.price

    @property
    def signed_cash_flow(self) -> float:
        return (-self.notional if self.side == "BUY" else self.notional) - self.commission


@dataclass(frozen=True)
class RebalancePlan:
    orders: tuple[PlannedOrder, ...]
    reserve: float
    projected_cash: float
    total_commission: float
    total_slippage: float
    total_impact: float


def price_order(config: Config, *, ticker: str, side: str, quantity: float,
                reference_price: float, adv: float | None = None,
                risk_off: bool = False, impact_bps: float | None = None,
                enforce_adv: bool = True) -> PlannedOrder:
    quantity = finite_number(quantity, "Order quantity", positive=True)
    reference_price = finite_number(reference_price, f"{ticker} price", positive=True)
    if side not in {"BUY", "SELL"}:
        raise ValueError("Order side must be BUY or SELL.")
    notional = finite_number(quantity * reference_price, "Order notional", positive=True)
    commission_bps = finite_number(config.trading_cost_bps, "Commission bps", nonnegative=True)
    slippage_bps = finite_number(config.slippage_bps, "Slippage bps", nonnegative=True)
    fraction = 0.0
    if adv is not None:
        fraction = notional / finite_number(adv, f"{ticker} ADV", positive=True)
        if enforce_adv and fraction > config.maximum_order_adv + 1e-12:
            raise ValueError(f"{ticker} order exceeds 1% ADV and is blocked.")
    if impact_bps is None:
        impact_bps = (config.impact_coefficient_bps * sqrt(fraction / config.impact_model_adv_threshold)
                      if fraction >= config.impact_model_adv_threshold else 0.0)
    impact_bps = finite_number(impact_bps, "Impact bps", nonnegative=True)
    direction = 1.0 if side == "BUY" else -1.0
    if commission_bps >= 10000:
        raise ValueError("Commission rate must be less than 100%.")
    if risk_off:
        floor = max(config.cost_scenarios_bps)
        slippage_bps = max(slippage_bps, (floor - commission_bps) / (1 + direction * commission_bps / 10000) - impact_bps)
    price = finite_number(reference_price * (1 + direction * (slippage_bps + impact_bps) / 10000),
                          "Execution price", positive=True)
    return PlannedOrder(ticker, side, quantity, reference_price, price,
                        finite_number(quantity * price * commission_bps / 10000, "Commission", nonnegative=True),
                        notional * slippage_bps / 10000, notional * impact_bps / 10000,
                        fraction, impact_bps)


def plan_rebalance(config: Config, nav: float, cash_available: float,
                   quantities: Mapping[str, float], target_weights: Mapping[str, float],
                   prices: Mapping[str, float], median_dollar_volume: Mapping[str, float] | None = None,
                   risk_off: bool = False) -> RebalancePlan:
    nav = finite_number(nav, "NAV", positive=True)
    cash_available = finite_number(cash_available, "Available cash", nonnegative=True)
    weights = {k: finite_number(v, f"{k} target", nonnegative=True) for k, v in target_weights.items()}
    if abs(sum(weights.values()) - 1.0) > 1e-6:
        raise ValueError("Target weights must sum to 1 before execution.")
    holdings = {k: finite_number(v, f"{k} quantity", nonnegative=True) for k, v in quantities.items()}
    tickers = sorted((set(holdings) | set(weights)) - {config.synthetic_cash_asset})
    marks = {k: finite_number(prices.get(k), f"{k} execution price", positive=True) for k in tickers}
    synthetic_weight = weights.get(config.synthetic_cash_asset, 0.0)
    reserve = min(nav, nav * synthetic_weight + max(config.operational_cash_buffer_min,
                                                    nav * config.operational_cash_buffer_pct))

    def evaluate(investment: float) -> tuple[tuple[PlannedOrder, ...], float]:
        orders = []
        for ticker in tickers:
            target = investment * weights.get(ticker, 0.0) / max(1 - synthetic_weight, 1e-12)
            delta = target / marks[ticker] - holdings.get(ticker, 0.0)
            if abs(delta) * marks[ticker] <= max(nav * 1e-8, 1e-6):
                continue
            adv = None if median_dollar_volume is None else median_dollar_volume.get(ticker)
            if median_dollar_volume is not None and adv is None:
                raise ValueError(f"Missing positive ADV for {ticker}.")
            orders.append(price_order(config, ticker=ticker, side="BUY" if delta > 0 else "SELL",
                                      quantity=abs(delta), reference_price=marks[ticker], adv=adv, risk_off=risk_off,
                                      enforce_adv=False))
        orders.sort(key=lambda item: item.side == "BUY")
        cash = finite_number(cash_available + sum(x.signed_cash_flow for x in orders), "Projected cash")
        return tuple(orders), cash

    # A bounded search sizes the whole basket including nonlinear impact and
    # both sides' fees. No account or order is mutated during planning.
    upper = max(nav - reserve, 0.0)
    orders, cash = evaluate(upper)
    if cash < reserve - 1e-8:
        lower = 0.0
        liquidation, liquidation_cash = evaluate(lower)
        if liquidation_cash < -1e-8:
            raise ValueError("Available funds cannot pay the liquidation costs.")
        reserve = min(reserve, liquidation_cash)
        orders, cash = liquidation, liquidation_cash
        for _ in range(64):
            midpoint = (lower + upper) / 2
            candidate, candidate_cash = evaluate(midpoint)
            if candidate_cash >= reserve:
                lower, orders, cash = midpoint, candidate, candidate_cash
            else:
                upper = midpoint
    if any(order.adv_fraction > config.maximum_order_adv + 1e-12 for order in orders):
        raise ValueError("Planned order exceeds 1% ADV and is blocked.")
    return RebalancePlan(orders, reserve, cash, sum(x.commission for x in orders),
                         sum(x.slippage for x in orders), sum(x.impact for x in orders))
