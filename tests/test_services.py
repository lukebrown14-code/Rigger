"""Service layer: research services and read-side queries, offline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from rigger import services
from rigger.core.db import LLMCallTable
from rigger.core.models import Instrument
from rigger.plugins.markets.us import USMarket
from tests.conftest import FakeConfig, FakeLLM, seed_bars

AAPL = Instrument(id="US:AAPL", market="us", symbol="AAPL", currency="USD", sector="Tech")
MSFT = Instrument(id="US:MSFT", market="us", symbol="MSFT", currency="USD", sector="Tech")


class FakeRig:
    """Just enough of :class:`rigger.runtime.Rigger` for the services."""

    def __init__(self, engine, llm: FakeLLM, universe: list[Instrument]) -> None:
        self.engine = engine
        self.llm = llm
        self._universe = universe
        self.settings = SimpleNamespace(
            openrouter_api_key="", openai_api_key="", anthropic_api_key=""
        )
        self.cfg = SimpleNamespace(
            base_currency="AUD",
            llm_provider="openrouter",
            llm_routing={"analyse": "test/model"},
            plugins={"sec_edgar": {}},
            universe={"us": [i.symbol for i in universe]},
        )
        market = USMarket()
        market.configure({"tickers": [i.symbol for i in universe]})
        self.plugins = {
            "us": market,
            "sec_edgar": SimpleNamespace(enabled=True),
        }

    def universe(self) -> list[Instrument]:
        return self._universe

    def context(self, universe):
        from rigger.core.plugin import Context

        return Context(
            engine=self.engine,
            settings=self.settings,
            config=FakeConfig(self.cfg.llm_routing),
            llm=self.llm,
            universe=universe,
            plugins=self.plugins,
        )


@pytest.fixture
def rig(tmp_engine, fake_llm):
    for inst in (AAPL, MSFT):
        seed_bars(tmp_engine, inst.id, price_fn=lambda i: 100.0)
    return FakeRig(tmp_engine, fake_llm, [AAPL, MSFT])


def test_setup_checks_flag_missing_key(rig):
    checks = {c.name: c for c in services.setup_checks(rig)}
    assert not checks["LLM provider (openrouter)"].ok
    assert "OPENROUTER_API_KEY" in checks["LLM provider (openrouter)"].fix
    assert checks["Price history"].ok
    assert not checks["SEC EDGAR contact"].ok


def test_data_health_counts_bars(rig):
    health = services.data_health(rig)
    assert health.counts["bar"] == 160
    assert set(health.latest_bar) >= {AAPL.id, MSFT.id}


def test_llm_costs_groups_by_task_and_model(tmp_engine):
    with Session(tmp_engine) as session:
        for i in range(3):
            session.add(
                LLMCallTable(
                    id=f"c{i}",
                    ts=datetime.now(UTC),
                    task="analyse",
                    model="m",
                    prompt_version="v",
                    prompt_hash=f"h{i}",
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd=0.5,
                    latency_ms=1,
                    cached=False,
                )
            )
        session.commit()
    rows = services.llm_costs(tmp_engine)
    assert rows == [services.CostRow("analyse", "m", 3, 1.5)]


def test_target_add_and_remove(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('[universe]\nus = ["AAPL"]\n', encoding="utf-8")

    services.add_target(
        "mining",
        kind="industry",
        market="asx",
        tickers=["BHP", "RIO"],
        tags=["diggers"],
        notes="big miners",
    )
    specs = services.target_specs()
    assert specs["mining"].kind == "industry"
    assert specs["mining"].tickers == ("BHP", "RIO")
    assert specs["mining"].tags == frozenset({"diggers"})
    assert specs["mining"].notes == "big miners"
    assert "universe_us" not in specs

    services.remove_target("mining")
    assert "mining" not in services.target_specs()


def test_add_target_unknown_market(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="known markets"):
        services.add_target("bogus", market="asz", tickers=["X"])


def test_save_custom_market_then_add_target_and_prevent_referenced_removal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    services.save_market("lse", label="London Stock Exchange", currency="GBP", yahoo_suffix=".L")
    assert services.market_profiles()["lse"].currency == "GBP"
    services.add_target("hargreaves", market="lse", tickers=["HL"])
    with pytest.raises(ValueError, match="hargreaves"):
        services.remove_market("lse")
    services.remove_target("hargreaves")
    services.remove_market("lse")
    assert "lse" not in services.market_profiles()


def test_save_market_validates_identity_and_currency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="market ID"):
        services.save_market("LSE!", label="London", currency="GBP", yahoo_suffix=".L")
    with pytest.raises(ValueError, match="three-letter"):
        services.save_market("lse", label="London", currency="GB", yahoo_suffix=".L")


def test_add_target_unknown_kind(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="known kinds"):
        services.add_target("bogus", kind="planet", market="asx", tickers=["X"])


def test_add_target_market_kind_takes_no_tickers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    services.add_target("aussie", kind="market", market="asx")
    assert services.target_specs()["aussie"].tickers == ()
    with pytest.raises(ValueError, match="takes no tickers"):
        services.add_target("aus2", kind="market", market="asx", tickers=["BHP"])


def test_add_target_non_market_kind_requires_tickers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="requires tickers"):
        services.add_target("bhp", kind="company", market="asx", tickers=[])


def test_remove_target_also_removes_legacy_watchlist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[watchlists.mining]\nmarket = "asx"\ntickers = ["BHP"]\n', encoding="utf-8"
    )
    services.remove_target("mining")
    assert services.target_specs() == {}
    with pytest.raises(KeyError, match="unknown target"):
        services.remove_target("mining")


# --- pulse -------------------------------------------------------------------


def _news(engine, id_, instrument_ids, published, source="rss"):
    import json

    from rigger.core.db import NewsItemTable

    with Session(engine) as session:
        session.add(
            NewsItemTable(
                id=id_,
                instrument_ids=json.dumps(list(instrument_ids)),
                published=published,
                title=id_,
                url="http://example.test",
                source=source,
            )
        )
        session.commit()


def _event(engine, id_, instrument_id, ts):
    from rigger.core.db import EventTable

    with Session(engine) as session:
        session.add(
            EventTable(
                id=id_,
                instrument_id=instrument_id,
                ts=ts,
                kind="earnings",
                summary=id_,
                sentiment=0.0,
                evidence_ids="[]",
                extracted_by="test",
                prompt_version="v1",
            )
        )
        session.commit()


def test_pulse_empty_database_returns_zeros(tmp_engine):
    since = datetime.now(UTC) - timedelta(days=1)
    result = services.pulse(tmp_engine, instrument_ids=["US:AAPL"], since=since)

    assert (result.articles, result.filings, result.events, result.total) == (0, 0, 0, 0)
    assert result.daily == [0] * 30
    assert result.busiest is None and result.quietest is None


def test_pulse_excludes_future_dated_calendar_events(tmp_engine):
    """The calendar plugin writes upcoming earnings into event.ts; they have not happened."""
    now = datetime.now(UTC)
    _event(tmp_engine, "past", "US:AAPL", now - timedelta(hours=2))
    _event(tmp_engine, "upcoming", "US:AAPL", now + timedelta(days=9))

    result = services.pulse(tmp_engine, instrument_ids=["US:AAPL"], since=now - timedelta(days=1))

    assert result.events == 1
    assert sum(result.daily) == 1


def test_pulse_splits_filings_from_articles(tmp_engine):
    now = datetime.now(UTC)
    _news(tmp_engine, "a1", ["US:AAPL"], now - timedelta(hours=1))
    _news(tmp_engine, "a2", ["US:AAPL"], now - timedelta(hours=2))
    _news(tmp_engine, "f1", ["US:AAPL"], now - timedelta(hours=3), source="sec_edgar")

    result = services.pulse(tmp_engine, instrument_ids=["US:AAPL"], since=now - timedelta(days=1))

    assert (result.articles, result.filings) == (2, 1)
    assert result.total == 3


def test_pulse_counts_only_since_but_ranks_over_whole_window(tmp_engine):
    """A short visit must still name a busiest: the tally spans the full window."""
    now = datetime.now(UTC)
    for n in range(5):
        _news(tmp_engine, f"old{n}", ["US:MSFT"], now - timedelta(days=10, hours=n))
    _news(tmp_engine, "recent", ["US:AAPL"], now - timedelta(minutes=5))

    result = services.pulse(
        tmp_engine, instrument_ids=["US:AAPL", "US:MSFT"], since=now - timedelta(hours=1)
    )

    assert result.articles == 1  # only the recent one is "new"
    assert result.busiest == ("US:MSFT", 5)  # but ranking sees the older five
    assert result.quietest == ("US:AAPL", 1)


def test_pulse_daily_is_zero_filled_oldest_first(tmp_engine):
    now = datetime.now(UTC)
    recent = now - timedelta(minutes=5)
    older = now - timedelta(days=3)
    _news(tmp_engine, "today", ["US:AAPL"], recent)
    _news(tmp_engine, "back", ["US:AAPL"], older)

    result = services.pulse(
        tmp_engine, instrument_ids=["US:AAPL"], since=now - timedelta(days=30), days=7
    )

    # Buckets are UTC days, so an item lands in the bucket for its own date —
    # do not assume "five minutes ago" is today, which is false for the five
    # minutes after UTC midnight and made this test fail once a day.
    def bucket(ts: datetime) -> int:
        return 6 - (now.date() - ts.date()).days

    assert len(result.daily) == 7
    assert result.daily[bucket(recent)] == 1
    assert result.daily[bucket(older)] == 1
    assert sum(result.daily) == 2


def test_pulse_ignores_instruments_outside_the_watchlist(tmp_engine):
    now = datetime.now(UTC)
    _news(tmp_engine, "off", ["US:TSLA"], now - timedelta(minutes=5))

    result = services.pulse(tmp_engine, instrument_ids=["US:AAPL"], since=now - timedelta(days=1))

    assert result.articles == 1  # still counted as new evidence
    assert result.busiest is None  # but TSLA is not on the watchlist, so nothing to rank


# --- upcoming_events ----------------------------------------------------------


def test_upcoming_events_excludes_the_past(tmp_engine):
    """The mirror of pulse(): pulse counts what happened, this takes what has not."""
    now = datetime.now(UTC)
    _event(tmp_engine, "past", "US:AAPL", now - timedelta(days=2))
    _event(tmp_engine, "soon", "US:AAPL", now + timedelta(days=4))

    result = services.upcoming_events(tmp_engine, instrument_ids=["US:AAPL"])

    assert [item.instrument_id for item in result] == ["US:AAPL"]
    assert result[0].ts > now


def test_upcoming_events_are_soonest_first_and_respect_limit(tmp_engine):
    now = datetime.now(UTC)
    for days in (30, 3, 12):
        _event(tmp_engine, f"e{days}", "US:AAPL", now + timedelta(days=days))

    result = services.upcoming_events(tmp_engine, instrument_ids=["US:AAPL"], limit=2)

    assert len(result) == 2
    assert [round((item.ts - now).total_seconds() / 86400) for item in result] == [3, 12]


def test_upcoming_events_ignores_unwatched_instruments(tmp_engine):
    now = datetime.now(UTC)
    _event(tmp_engine, "watched", "US:AAPL", now + timedelta(days=1))
    _event(tmp_engine, "other", "US:TSLA", now + timedelta(hours=1))

    result = services.upcoming_events(tmp_engine, instrument_ids=["US:AAPL"])

    assert [item.instrument_id for item in result] == ["US:AAPL"]


def test_upcoming_events_with_no_instruments_returns_empty(tmp_engine):
    """An empty IN () is a SQL error, so this must short-circuit before querying."""
    _event(tmp_engine, "future", "US:AAPL", datetime.now(UTC) + timedelta(days=1))

    assert services.upcoming_events(tmp_engine, instrument_ids=[]) == []


def _write_report(root, company, stem, as_of=None):
    """A report markdown file, optionally with the ``.json`` sidecar beside it."""
    import json

    folder = root / company
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.md").write_text("# report\n", encoding="utf-8")
    if as_of is not None:
        (folder / f"{stem}.json").write_text(
            json.dumps(
                {
                    "summary": "s",
                    "sentiment": 0.0,
                    "target_id": "apple",
                    "as_of": as_of.isoformat(),
                    "prompt_version": "report_v1",
                }
            ),
            encoding="utf-8",
        )


def test_latest_report_prefers_the_sidecar_as_of(tmp_path):
    stamp = datetime(2024, 3, 2, 9, 30, tzinfo=UTC)
    _write_report(tmp_path, "US:AAPL", "2024-03-01", as_of=stamp)

    result = services.latest_report(tmp_path, "US:AAPL")

    assert result is not None
    assert result.path.name == "2024-03-01.md"
    assert result.as_of == stamp
    assert services.latest_report_age(tmp_path, "US:AAPL") == stamp


def test_latest_report_falls_back_to_the_filename_then_gives_up(tmp_path):
    """No sidecar: the stem is the only date on offer, and a name that is not a
    date yields no age rather than an invented one."""
    _write_report(tmp_path, "US:AAPL", "2024-03-01")
    _write_report(tmp_path, "US:MSFT", "draft")

    assert services.latest_report_age(tmp_path, "US:AAPL") == datetime(2024, 3, 1, tzinfo=UTC)
    stamp = services.latest_report(tmp_path, "US:MSFT")
    assert stamp is not None and stamp.as_of is None
    assert services.latest_report_age(tmp_path, "US:MSFT") is None


def test_latest_report_takes_the_newest_and_tolerates_a_missing_folder(tmp_path):
    _write_report(tmp_path, "US:AAPL", "2024-01-01")
    _write_report(tmp_path, "US:AAPL", "2024-06-30")

    stamp = services.latest_report(tmp_path, "US:AAPL")
    assert stamp is not None and stamp.path.stem == "2024-06-30"
    assert services.latest_report(tmp_path, "US:NOPE") is None
    assert services.latest_report_age(tmp_path / "missing", "US:AAPL") is None
