"""Backend-agnostic repositories for the quant research store.

Each repository wraps one logical area of the schema and exposes plain
pandas / dict in and out. Application code talks to these objects, never to
SQL or a specific database driver — that boundary is what keeps the SQLite ->
Postgres/MySQL switch a one-line URL change.
"""
from __future__ import annotations

from storage.repositories.experiments import ExperimentRepository
from storage.repositories.governance import GovernanceRepository
from storage.repositories.execution import ExecutionRepository
from storage.repositories.market_data import MarketDataRepository
from storage.repositories.orders import OrderRepository
from storage.repositories.portfolio import PortfolioRepository
from storage.repositories.signals import SignalRepository
from storage.repositories.trusted_data import TrustedMarketDataRepository

__all__ = [
    "ExperimentRepository",
    "PortfolioRepository",
    "OrderRepository",
    "SignalRepository",
    "MarketDataRepository",
    "TrustedMarketDataRepository",
    "GovernanceRepository",
    "ExecutionRepository",
]
