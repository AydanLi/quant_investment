from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from execution.models import (
    AccountSnapshot,
    BrokerEnvironment,
    BrokerPosition,
    ExecutionFill,
    OrderIntent,
    Quote,
)


class BrokerAdapter(ABC):
    environment: BrokerEnvironment

    @abstractmethod
    def account_snapshot(self) -> AccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def quote(self, ticker: str) -> Quote:
        raise NotImplementedError

    @abstractmethod
    def preview(self, intent: OrderIntent) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def submit(self, intent: OrderIntent) -> str:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, broker_order_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def order_status(self, broker_order_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def fills(self, broker_order_id: str | None = None) -> Sequence[ExecutionFill]:
        raise NotImplementedError

    @abstractmethod
    def open_order_ids(self) -> Sequence[str]:
        raise NotImplementedError

    @abstractmethod
    def supports_fractional(self, ticker: str, order_type: str = "LMT") -> bool:
        raise NotImplementedError


class IbkrBrokerAdapter(BrokerAdapter):
    """Explicit IBKR boundary; disabled until connection details are supplied.

    No order is submitted merely by constructing this adapter.  The optional
    ``ibapi`` dependency is imported only when ``connect`` is called, keeping
    research installations physically separated from broker connectivity.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        account_ref: str,
        environment: BrokerEnvironment = BrokerEnvironment.PAPER,
    ) -> None:
        if environment == BrokerEnvironment.RESEARCH:
            raise ValueError("IBKR adapter requires PAPER or LIVE environment.")
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.account_ref = account_ref
        self.environment = environment
        self._connected = False

    def connect(self) -> None:
        try:
            import ibapi  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "IBKR connectivity is not installed. Configure the paper account first, then install ibapi."
            ) from exc
        # The event-loop implementation intentionally remains gated on account,
        # market-data, fractional, and commission details from the user.
        raise RuntimeError(
            "IBKR connection is configuration-blocked pending paper account permissions and TWS/Gateway settings."
        )

    def _blocked(self) -> RuntimeError:
        return RuntimeError("IBKR adapter is not connected to an approved paper session.")

    def account_snapshot(self) -> AccountSnapshot:
        raise self._blocked()

    def quote(self, ticker: str) -> Quote:
        raise self._blocked()

    def preview(self, intent: OrderIntent) -> dict[str, object]:
        raise self._blocked()

    def submit(self, intent: OrderIntent) -> str:
        raise self._blocked()

    def cancel(self, broker_order_id: str) -> None:
        raise self._blocked()

    def order_status(self, broker_order_id: str) -> str:
        raise self._blocked()

    def fills(self, broker_order_id: str | None = None) -> Sequence[ExecutionFill]:
        raise self._blocked()

    def open_order_ids(self) -> Sequence[str]:
        raise self._blocked()

    def supports_fractional(self, ticker: str, order_type: str = "LMT") -> bool:
        raise self._blocked()


class InMemoryPaperBroker(BrokerAdapter):
    """Deterministic broker used only by OMS integration tests and paper drills."""

    environment = BrokerEnvironment.PAPER

    def __init__(self, account: AccountSnapshot, quotes: dict[str, Quote]) -> None:
        self._account = account
        self._quotes = quotes
        self._orders: dict[str, OrderIntent] = {}

    def account_snapshot(self) -> AccountSnapshot:
        return self._account

    def quote(self, ticker: str) -> Quote:
        return self._quotes[ticker]

    def preview(self, intent: OrderIntent) -> dict[str, object]:
        return {"accepted": True, "notional": intent.notional}

    def submit(self, intent: OrderIntent) -> str:
        broker_id = f"paper-{len(self._orders) + 1}"
        self._orders[broker_id] = intent
        return broker_id

    def cancel(self, broker_order_id: str) -> None:
        self._orders.pop(broker_order_id, None)

    def order_status(self, broker_order_id: str) -> str:
        return "SUBMITTED" if broker_order_id in self._orders else "CANCELED"

    def fills(self, broker_order_id: str | None = None) -> Sequence[ExecutionFill]:
        return ()

    def open_order_ids(self) -> Sequence[str]:
        return tuple(self._orders)

    def supports_fractional(self, ticker: str, order_type: str = "LMT") -> bool:
        return True

