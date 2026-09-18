"""yfinance data plugin: historical daily bars for any market.

yfinance needs an exchange suffix for non-US symbols (BHP -> BHP.AX). The
suffix per market comes from ``[plugins.yfinance].suffixes`` so adding a market
is a config change, not a code change.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from rigger.core.models import Bar, Event, Fundamental, Instrument, NewsItem
from rigger.core.plugin import DataPlugin

DEFAULT_SUFFIXES: dict[str, str] = {"us": "", "asx": ".AX"}


def yf_symbol(inst: Instrument, suffixes: dict[str, str] | None = None) -> str:
    """yfinance ticker for an instrument: BHP on asx -> BHP.AX. One source of truth."""
    table = suffixes if suffixes is not None else DEFAULT_SUFFIXES
    return f"{inst.symbol.upper()}{table.get(inst.market, '')}"


class YFinanceSymbols(DataPlugin):
    """Base for yfinance-backed plugins: shares the ``suffixes`` config table.

    Every subclass reads ``[plugins.yfinance].suffixes`` (via ``shared_config``)
    so a market added there applies to bars and calendar alike.
    """

    market = None  # works for any market with a known suffix
    shared_config = "yfinance"

    def __init__(self) -> None:
        self.suffixes: dict[str, str] = dict(DEFAULT_SUFFIXES)

    def configure(self, cfg: dict[str, Any]) -> None:
        self.suffixes.update(cfg.get("suffixes", {}))

    def set_market_suffixes(self, suffixes: dict[str, str]) -> None:
        """Apply config-backed exchange profiles after plugin configuration."""
        self.suffixes.update(suffixes)

    def yf_symbol(self, inst: Instrument) -> str:
        return yf_symbol(inst, self.suffixes)


class YFinanceData(YFinanceSymbols):
    name = "yfinance"

    async def fetch(
        self, instruments: list[Instrument], since: datetime
    ) -> list[Bar | NewsItem | Fundamental | Event]:
        import yfinance as yf

        bars: list[Bar] = []
        for inst in instruments:
            ticker = yf.Ticker(self.yf_symbol(inst))
            df = ticker.history(start=since.date(), interval="1d", auto_adjust=True)
            if df is None or df.empty:
                continue
            for ts, row in df.iterrows():
                # FX and some thin symbols return rows with NaN prices; SQLite
                # would store them as NULL and violate the bar NOT NULL columns.
                if any(math.isnan(float(row[c])) for c in ("Open", "High", "Low", "Close")):
                    continue
                bars.append(
                    Bar(
                        instrument_id=inst.id,
                        ts=ts.to_pydatetime().astimezone(UTC),
                        open=float(row["Open"]),
                        high=float(row["High"]),
                        low=float(row["Low"]),
                        close=float(row["Close"]),
                        volume=float(row["Volume"]),
                        source=self.name,
                    )
                )
        return bars
