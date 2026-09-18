"""Config-only watch targets: ticker baskets per kind, and bare markets."""

from __future__ import annotations

from typing import Any

from rigger.core.ids import make_instrument_id
from rigger.core.models import AssetClass, Instrument
from rigger.core.plugin import TargetPlugin


class TickerTarget(TargetPlugin):
    """A target followed via tickers in one market.

    Backs the company, sector, industry, and theme kinds (and the legacy
    ``tickers`` kind); instrument construction is shared by all of them.
    """

    version = "0.1.0"

    def __init__(self) -> None:
        self.name = ""
        self.label = ""
        self.market = ""
        self.tickers: list[str] = []
        self.asset_class: AssetClass = "equity"
        self.sector: str | None = None
        self.tags: frozenset[str] = frozenset()
        self.notes = ""
        self.overrides: dict[str, dict[str, Any]] = {}
        self.market_currency = ""

    def configure(self, cfg: dict[str, Any]) -> None:
        self.name = str(cfg.get("name", self.name))
        self.label = str(cfg.get("label", self.name))
        self.market = str(cfg.get("market", "")).lower()
        self.market_currency = str(cfg.get("market_currency", self.market_currency))
        self.tickers = [str(t).upper() for t in cfg.get("tickers", [])]
        self.asset_class = cfg.get("asset_class", "equity")
        self.sector = cfg.get("sector")
        self.tags = frozenset(str(tag) for tag in cfg.get("tags", []))
        self.notes = str(cfg.get("notes", ""))
        self.overrides = {
            str(symbol).upper(): dict(values) for symbol, values in cfg.get("overrides", {}).items()
        }

    def instruments(self) -> list[Instrument]:
        if not self.market:
            raise ValueError(f"target {self.name!r} must set market")
        currency = self.market_currency or {"us": "USD", "asx": "AUD"}.get(self.market, "AUD")
        result = []
        for symbol in self.tickers:
            override = self.overrides.get(symbol, {})
            result.append(
                Instrument(
                    id=make_instrument_id(self.market.upper(), symbol),
                    market=self.market,
                    symbol=symbol,
                    name=symbol,
                    currency=str(override.get("currency", currency)),
                    sector=override.get("sector", self.sector),
                    asset_class=override.get("asset_class", self.asset_class),
                    watchlists=(self.name,),
                    tags=frozenset(override.get("tags", self.tags)),
                    industry=override.get("industry"),
                    meta=dict(override.get("meta", {})),
                )
            )
        return result


class CompanyTarget(TickerTarget):
    kind = "company"


class SectorTarget(TickerTarget):
    kind = "sector"


class IndustryTarget(TickerTarget):
    kind = "industry"


class ThemeTarget(TickerTarget):
    kind = "theme"


class LegacyTickersTarget(TickerTarget):
    """The ``tickers`` kind: legacy ``[watchlists.<name>]`` tables."""

    kind = "tickers"


class MarketTarget(TargetPlugin):
    """A market followed directly: no tickers, no instruments (yet)."""

    kind = "market"
    version = "0.1.0"

    def __init__(self) -> None:
        self.name = ""
        self.label = ""
        self.market = ""
        self.tags: frozenset[str] = frozenset()
        self.notes = ""

    def configure(self, cfg: dict[str, Any]) -> None:
        self.name = str(cfg.get("name", self.name))
        self.label = str(cfg.get("label", self.name))
        self.market = str(cfg.get("market", "")).lower()
        self.tags = frozenset(str(tag) for tag in cfg.get("tags", []))
        self.notes = str(cfg.get("notes", ""))

    def instruments(self) -> list[Instrument]:
        return []
