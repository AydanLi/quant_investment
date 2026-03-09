from __future__ import annotations

from typing import Dict

import pandas as pd
import yfinance as yf

from config.settings import Config


class MarketDataLoader:
    def __init__(self, config: Config):
        self.config = config

    def load(self) -> Dict[str, pd.DataFrame]:
        tickers = list(set(self.config.universe + [self.config.benchmark, self.config.fear_gauge]))
        data = yf.download(
            tickers=tickers,
            start=self.config.start_date,
            end=self.config.end_date,
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=True,
        )

        if data.empty:
            raise ValueError("No market data returned. Check ticker list, dates, or network connection.")

        result: Dict[str, pd.DataFrame] = {}
        for t in tickers:
            try:
                if t in data.columns.get_level_values(0):
                    df = data[t].copy()
                else:
                    cols = [c for c in data.columns if isinstance(c, tuple) and c[0] == t]
                    if cols:
                        df = data[cols].copy()
                        df.columns = [c[1] for c in cols]
                    else:
                        continue

                df = df.rename(columns=str.title)
                keep_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
                df = df[keep_cols].dropna(how="all")
                if not df.empty:
                    result[t] = df
            except Exception:
                continue

        if not result:
            raise ValueError("Failed to parse downloaded market data.")

        return result