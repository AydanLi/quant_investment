from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd


class SQLiteStore:
    def __init__(self, db_path: str = "quant_research.db"):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON;")

    def close(self) -> None:
        self.conn.close()

    def init_db(self) -> None:
        cursor = self.conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS experiment_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_time TEXT NOT NULL,
                scenario_name TEXT,
                start_date TEXT,
                rebalance_frequency TEXT,
                top_n INTEGER,
                min_momentum_threshold REAL,
                target_annual_vol REAL,
                max_asset_weight REAL,
                risk_off_cash_weight REAL,
                vix_risk_off_threshold REAL,
                vix_high_threshold REAL,
                trading_cost_bps REAL,
                start_equity REAL,
                end_equity REAL,
                total_return REAL,
                cagr REAL,
                annual_vol REAL,
                sharpe REAL,
                sortino REAL,
                max_drawdown REAL,
                avg_turnover REAL,
                latest_signal_date TEXT,
                latest_regime TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                equity REAL,
                daily_return REAL,
                regime TEXT,
                turnover REAL,
                FOREIGN KEY (run_id) REFERENCES experiment_runs(id) ON DELETE CASCADE
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                order_time TEXT NOT NULL,
                ticker TEXT,
                side TEXT,
                weight_change REAL,
                FOREIGN KEY (run_id) REFERENCES experiment_runs(id) ON DELETE CASCADE
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                signal_date TEXT NOT NULL,
                regime TEXT,
                ticker TEXT,
                weight REAL,
                FOREIGN KEY (run_id) REFERENCES experiment_runs(id) ON DELETE CASCADE
            )
            """
        )

        self.conn.commit()

    def save_experiment_run(
        self,
        scenario_name: str,
        config,
        summary: pd.Series,
        latest_signal: dict,
        run_time: Optional[str] = None,
    ) -> int:
        if run_time is None:
            run_time = pd.Timestamp.now().isoformat()

        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO experiment_runs (
                run_time,
                scenario_name,
                start_date,
                rebalance_frequency,
                top_n,
                min_momentum_threshold,
                target_annual_vol,
                max_asset_weight,
                risk_off_cash_weight,
                vix_risk_off_threshold,
                vix_high_threshold,
                trading_cost_bps,
                start_equity,
                end_equity,
                total_return,
                cagr,
                annual_vol,
                sharpe,
                sortino,
                max_drawdown,
                avg_turnover,
                latest_signal_date,
                latest_regime
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_time,
                scenario_name,
                config.start_date,
                config.rebalance_frequency,
                config.top_n,
                config.min_momentum_threshold,
                config.target_annual_vol,
                config.max_asset_weight,
                config.risk_off_cash_weight,
                config.vix_risk_off_threshold,
                config.vix_high_threshold,
                config.trading_cost_bps,
                float(summary.get("Start Equity", 0.0)),
                float(summary.get("End Equity", 0.0)),
                float(summary.get("Total Return", 0.0)),
                float(summary.get("CAGR", 0.0)),
                float(summary.get("Annual Vol", 0.0)),
                float(summary.get("Sharpe", 0.0)) if pd.notna(summary.get("Sharpe", None)) else None,
                float(summary.get("Sortino", 0.0)) if pd.notna(summary.get("Sortino", None)) else None,
                float(summary.get("Max Drawdown", 0.0)),
                float(summary.get("Avg Turnover", 0.0)),
                latest_signal.get("date"),
                latest_signal.get("regime"),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def save_portfolio_daily(self, run_id: int, portfolio: pd.DataFrame) -> None:
        if portfolio.empty:
            return

        records = []
        for date, row in portfolio.iterrows():
            records.append(
                (
                    run_id,
                    str(date.date()) if hasattr(date, "date") else str(date),
                    float(row.get("equity", 0.0)) if pd.notna(row.get("equity", None)) else None,
                    float(row.get("daily_return", 0.0)) if pd.notna(row.get("daily_return", None)) else None,
                    row.get("regime"),
                    float(row.get("turnover", 0.0)) if pd.notna(row.get("turnover", None)) else None,
                )
            )

        self.conn.executemany(
            """
            INSERT INTO portfolio_daily (
                run_id, date, equity, daily_return, regime, turnover
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            records,
        )
        self.conn.commit()

    def save_orders(self, run_id: int, orders: pd.DataFrame) -> None:
        if orders.empty:
            return

        records = []
        for _, row in orders.iterrows():
            records.append(
                (
                    run_id,
                    str(row.get("date")),
                    row.get("ticker"),
                    row.get("side"),
                    float(row.get("weight_change", 0.0)) if pd.notna(row.get("weight_change", None)) else None,
                )
            )

        self.conn.executemany(
            """
            INSERT INTO orders (
                run_id, order_time, ticker, side, weight_change
            ) VALUES (?, ?, ?, ?, ?)
            """,
            records,
        )
        self.conn.commit()

    def save_signals(self, run_id: int, latest_signal: dict) -> None:
        weights = latest_signal.get("weights", {})
        if not weights:
            return

        signal_date = latest_signal.get("date")
        regime = latest_signal.get("regime")

        records = []
        for ticker, weight in weights.items():
            records.append((run_id, signal_date, regime, ticker, float(weight)))

        self.conn.executemany(
            """
            INSERT INTO signals (
                run_id, signal_date, regime, ticker, weight
            ) VALUES (?, ?, ?, ?, ?)
            """,
            records,
        )
        self.conn.commit()

    def get_experiment_runs(self, limit: int = 20) -> pd.DataFrame:
        query = f"""
            SELECT *
            FROM experiment_runs
            ORDER BY id DESC
            LIMIT {int(limit)}
        """
        return pd.read_sql_query(query, self.conn)

    def get_run_portfolio(self, run_id: int) -> pd.DataFrame:
        query = "SELECT * FROM portfolio_daily WHERE run_id = ? ORDER BY date"
        return pd.read_sql_query(query, self.conn, params=(run_id,))

    def get_run_orders(self, run_id: int) -> pd.DataFrame:
        query = "SELECT * FROM orders WHERE run_id = ? ORDER BY id"
        return pd.read_sql_query(query, self.conn, params=(run_id,))

    def get_run_signals(self, run_id: int) -> pd.DataFrame:
        query = "SELECT * FROM signals WHERE run_id = ? ORDER BY id"
        return pd.read_sql_query(query, self.conn, params=(run_id,))