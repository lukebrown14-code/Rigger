"""Watch targets: domain model, kinds, config round-trips, merging, and the [universe] shim."""

from __future__ import annotations

import pytest

from rigger import services
from rigger.core.config import build_config
from rigger.core.ids import make_instrument_id
from rigger.core.models import Instrument
from rigger.core.plugin import discover_targets
from rigger.plugins.data.yfinance import YFinanceData
from rigger.plugins.markets.asx import ASXMarket
from rigger.plugins.markets.us import USMarket
from rigger.plugins.targets.tickers import (
    CompanyTarget,
    LegacyTickersTarget,
    MarketTarget,
    SectorTarget,
)
from rigger.runtime import Rigger
from rigger.targets import target_from_spec


def _target(cls, name: str, market: str, tickers: list[str], **extra):
    target = cls()
    target.configure({"name": name, "market": market, "tickers": tickers, **extra})
    return target


def _rig(raw: dict) -> Rigger:
    rig = Rigger.__new__(Rigger)
    us, asx = USMarket(), ASXMarket()
    universe = raw.get("universe", {})
    us.configure({"tickers": list(universe.get("us", []))})
    asx.configure({"tickers": list(universe.get("asx", []))})
    rig.plugins = {"us": us, "asx": asx}
    rig.cfg = build_config(raw)
    rig.targets = rig._build_targets()
    return rig


def test_make_instrument_id():
    assert make_instrument_id("US", "AAPL") == "US:AAPL"
    assert make_instrument_id("ASX", "BHP") == "ASX:BHP"


def test_company_target_builds_instruments():
    target = _target(CompanyTarget, "mining", "asx", ["BHP", "RIO", "FMG"])
    instruments = target.instruments()
    assert [i.id for i in instruments] == ["ASX:BHP", "ASX:RIO", "ASX:FMG"]
    assert all(i.asset_class == "equity" for i in instruments)
    assert all(i.sector is None for i in instruments)
    assert all(i.watchlists == ("mining",) for i in instruments)


def test_ticker_target_asset_class_and_sector_and_tags():
    target = _target(
        SectorTarget,
        "bonds",
        "us",
        ["TLT"],
        asset_class="bond",
        sector="Government",
        tags=["income", "rates"],
    )
    (instrument,) = target.instruments()
    assert instrument.asset_class == "bond"
    assert instrument.sector == "Government"
    assert instrument.tags == frozenset({"income", "rates"})


def test_ticker_target_currency_defaults_by_market():
    assert _target(CompanyTarget, "us", "us", ["AAPL"]).instruments()[0].currency == "USD"
    assert _target(CompanyTarget, "asx", "asx", ["BHP"]).instruments()[0].currency == "AUD"


def test_symbol_overrides():
    target = _target(
        LegacyTickersTarget, "mixed", "asx", ["BHP"], overrides={"BHP": {"asset_class": "bond"}}
    )
    (instrument,) = target.instruments()
    assert instrument.asset_class == "bond"


def test_missing_market_raises():
    target = CompanyTarget()
    target.configure({"name": "nope", "tickers": ["BHP"]})
    with pytest.raises(ValueError, match="must set market"):
        target.instruments()


def test_market_target_contributes_no_instruments():
    target = _target(MarketTarget, "aussie", "asx", [])
    assert target.instruments() == []


def test_discover_targets_has_all_kinds():
    kinds = discover_targets()
    assert {"company", "sector", "industry", "market", "theme", "tickers"} <= set(kinds)


def test_instrument_defaults_are_stable():
    inst = Instrument(id="US:AAPL", market="us", symbol="AAPL", currency="USD")
    assert inst.asset_class == "equity"
    assert inst.watchlists == ()
    assert inst.tags == frozenset()
    assert inst.industry is None
    assert inst.meta == {}


def test_universe_shim_produces_legacy_targets():
    cfg = build_config({"universe": {"us": ["AAPL", "MSFT"], "asx": ["BHP"]}})
    assert cfg.targets["universe_us"]["kind"] == "tickers"
    assert cfg.targets["universe_us"]["tickers"] == ["AAPL", "MSFT"]
    assert cfg.targets["universe_asx"]["tickers"] == ["BHP"]
    assert cfg.targets["universe_us"]["legacy"] is True


def test_universe_shim_instruments_are_byte_identical():
    """Legacy [universe] + [watchlists] config must yield today's exact instruments."""

    def expected(
        id: str,
        market: str,
        symbol: str,
        currency: str,
        sector: str | None,
        watchlists: tuple[str, ...],
    ) -> Instrument:
        return Instrument(
            id=id,
            market=market,
            symbol=symbol,
            name=symbol,
            currency=currency,
            sector=sector,
            asset_class="equity",
            watchlists=watchlists,
        )

    rig = _rig(
        {
            "universe": {"us": ["AAPL", "MSFT"], "asx": ["BHP"]},
            "watchlists": {
                "mining": {"kind": "tickers", "market": "asx", "tickers": ["BHP", "RIO"]}
            },
        }
    )
    assert rig.universe() == [
        expected("ASX:BHP", "asx", "BHP", "AUD", None, ("mining", "universe_asx")),
        expected("ASX:RIO", "asx", "RIO", "AUD", None, ("mining",)),
        expected("US:AAPL", "us", "AAPL", "USD", "Technology", ("universe_us",)),
        expected("US:MSFT", "us", "MSFT", "USD", "Technology", ("universe_us",)),
    ]


def test_each_kind_round_trips_through_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("", encoding="utf-8")

    services.add_target(
        "bhp",
        kind="company",
        market="asx",
        tickers=["BHP"],
        tags=["miners"],
        notes="big miner",
        label="BHP Group",
    )
    services.add_target("banks", kind="sector", market="asx", tickers=["CBA", "WBC"])
    services.add_target("gold", kind="industry", market="asx", tickers=["EVN", "NCM"])
    services.add_target("solar", kind="theme", market="us", tickers=["ENPH", "FSLR"])
    services.add_target("aussie", kind="market", market="asx")

    specs = services.target_specs()
    assert specs["bhp"].kind == "company"
    assert specs["bhp"].markets == ("asx",)
    assert specs["bhp"].tickers == ("BHP",)
    assert specs["bhp"].tags == frozenset({"miners"})
    assert specs["bhp"].notes == "big miner"
    assert specs["bhp"].name == "BHP Group"
    assert specs["banks"].kind == "sector"
    assert specs["banks"].tickers == ("CBA", "WBC")
    assert specs["gold"].kind == "industry"
    assert specs["solar"].kind == "theme"
    assert specs["solar"].tickers == ("ENPH", "FSLR")
    assert specs["aussie"].kind == "market"
    assert specs["aussie"].markets == ("asx",)
    assert specs["aussie"].tickers == ()

    rig = _rig(
        {
            "targets": {
                "bhp": {"kind": "company", "market": "asx", "tickers": ["BHP"]},
                "solar": {"kind": "theme", "market": "us", "tickers": ["ENPH", "FSLR"]},
                "aussie": {"kind": "market", "market": "asx"},
            }
        }
    )
    assert [i.id for i in rig.targets["bhp"].instruments()] == ["ASX:BHP"]
    assert [i.id for i in rig.targets["solar"].instruments()] == ["US:ENPH", "US:FSLR"]
    assert rig.targets["aussie"].instruments() == []


def test_market_target_adds_no_instruments_to_universe():
    rig = _rig(
        {
            "targets": {
                "bhp": {"kind": "company", "market": "asx", "tickers": ["BHP"]},
                "aussie": {"kind": "market", "market": "asx"},
            }
        }
    )
    assert [i.id for i in rig.universe()] == ["ASX:BHP"]
    assert set(rig.targets) == {"bhp", "aussie"}


def test_ticker_in_two_targets_merges():
    rig = _rig(
        {
            "targets": {
                "mining": {
                    "kind": "industry",
                    "market": "asx",
                    "tickers": ["BHP", "RIO"],
                    "tags": ["diggers"],
                },
                "bigminer": {
                    "kind": "company",
                    "market": "asx",
                    "tickers": ["BHP"],
                    "tags": ["core"],
                },
            }
        }
    )
    universe = {i.id: i for i in rig.universe()}
    assert sorted(universe) == ["ASX:BHP", "ASX:RIO"]
    assert set(universe["ASX:BHP"].watchlists) == {"mining", "bigminer"}
    assert universe["ASX:BHP"].tags == frozenset({"diggers", "core"})
    assert universe["ASX:RIO"].watchlists == ("mining",)


def test_unknown_market_fails_loudly_naming_known_markets():
    rig = Rigger.__new__(Rigger)
    rig.plugins = {"us": USMarket(), "asx": ASXMarket()}
    rig.cfg = build_config(
        {"targets": {"bogus": {"kind": "company", "market": "asz", "tickers": ["X"]}}}
    )
    with pytest.raises(KeyError, match=r"known markets are asx, us"):
        rig._build_targets()


def test_custom_market_builds_target_and_yahoo_symbol():
    rig = _rig(
        {
            "markets": {"lse": {"label": "London Stock Exchange", "currency": "GBP", "yahoo_suffix": ".L"}},
            "targets": {"hargreaves": {"kind": "company", "market": "lse", "tickers": ["HL"]}},
        }
    )
    (instrument,) = rig.targets["hargreaves"].instruments()
    assert (instrument.id, instrument.currency) == ("LSE:HL", "GBP")
    feed = YFinanceData()
    feed.configure({})
    feed.set_market_suffixes({name: item.yahoo_suffix for name, item in rig.cfg.markets.items()})
    assert feed.yf_symbol(instrument) == "HL.L"


def test_unknown_kind_fails_loudly_naming_known_kinds():
    rig = Rigger.__new__(Rigger)
    rig.plugins = {"us": USMarket(), "asx": ASXMarket()}
    rig.cfg = build_config(
        {"targets": {"bogus": {"kind": "planet", "market": "asx", "tickers": ["X"]}}}
    )
    with pytest.raises(KeyError, match="known kinds are"):
        rig._build_targets()


def test_legacy_watchlist_kinds_are_inferred_from_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[watchlists.single]\nmarket = "asx"\ntickers = ["BHP"]\n'
        '[watchlists.basket]\nmarket = "asx"\ntickers = ["BHP", "RIO"]\n',
        encoding="utf-8",
    )
    specs = services.target_specs()
    assert specs["single"].kind == "company"
    assert specs["basket"].kind == "theme"
    assert (
        target_from_spec("basket", {"kind": "tickers", "tickers": ["BHP", "RIO"]}).kind == "theme"
    )


def test_target_specs_hide_universe_shim(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('[universe]\nus = ["AAPL"]\n', encoding="utf-8")
    assert services.target_specs() == {}


def test_legacy_watchlist_keeps_third_party_kind_usable(tmp_path, monkeypatch):
    """A [watchlists] kind from a third-party plugin is modelled by shape, not rejected."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[watchlists.custom]\nkind = "crypto_pairs"\nmarket = "asx"\ntickers = ["BTC", "ETH"]\n',
        encoding="utf-8",
    )
    assert services.target_specs()["custom"].kind == "theme"


def test_targets_table_still_rejects_unknown_kind(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[targets.custom]\nkind = "crypto_pairs"\nmarket = "asx"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown kind"):
        services.target_specs()
