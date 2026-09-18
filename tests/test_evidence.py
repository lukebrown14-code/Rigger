"""Tests for the unified evidence read model.

``_seed`` writes 5 daily bars per instrument from 2026-03-18 (autoincrement ids
1-5 for AAPL, 6-10 for MSFT), one sec_edgar filing, one two-instrument news
item, one event, and one fundamental. ``EXPECTED_ORDER`` is the full pool
ordered ts desc then id asc; ids order as strings, so "bar:10" precedes
"bar:5" at a shared timestamp.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlmodel import Session

from rigger.core.db import EventTable, FundamentalTable, NewsItemTable
from rigger.core.json import to_json
from rigger.evidence import cite, evidence, evidence_by_ids
from tests.conftest import seed_bars

INST = "US:AAPL"
OTHER = "US:MSFT"
NEWS_URL = "https://example.com/news-1"
FILING_URL = "https://example.com/filing-1"

# 5 daily bars per instrument from 2026-03-18 (autoincrement ids 1-5 AAPL, 6-10
# MSFT), one filing, one two-instrument news item, one event, one fundamental.
# "bar:10" sorts before "bar:5" because ids order as strings.
EXPECTED_ORDER = [
    "bar:10",
    "bar:5",
    "filing:filing-1",
    "bar:4",
    "bar:9",
    "news:news-1",
    "bar:3",
    "bar:8",
    "event:event-1",
    "bar:2",
    "bar:7",
    "bar:1",
    "bar:6",
    "fundamental:1",
]


def _seed(engine) -> None:
    start = datetime(2026, 3, 18, tzinfo=UTC)
    seed_bars(engine, INST, n=5, start=start)
    seed_bars(engine, OTHER, n=5, start=start)
    with Session(engine) as session:
        session.add(
            NewsItemTable(
                id="news-1",
                instrument_ids=to_json([INST, OTHER]),
                published=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
                title="Apple and Microsoft sign cloud deal",
                url=NEWS_URL,
                body="Both companies announced a partnership.",
                source="rss",
            )
        )
        session.add(
            NewsItemTable(
                id="filing-1",
                instrument_ids=to_json([INST]),
                published=datetime(2026, 3, 21, 12, 0, tzinfo=UTC),
                title="Apple 10-K filed",
                url=FILING_URL,
                source="sec_edgar",
            )
        )
        session.add(
            EventTable(
                id="event-1",
                instrument_id=INST,
                ts=datetime(2026, 3, 19, 12, 0, tzinfo=UTC),
                kind="earnings",
                summary="Reported EPS above consensus",
                sentiment=0.4,
                evidence_ids=to_json(["news-1"]),
                extracted_by="test/model",
                prompt_version="extract_v1",
            )
        )
        session.add(
            FundamentalTable(
                instrument_id=INST,
                as_of=date(2026, 1, 1),
                metric="eps",
                value=1.0,
                source="edgar",
            )
        )
        session.commit()


def test_mixed_sources_unify_with_kinds(tmp_engine):
    _seed(tmp_engine)
    items = evidence(tmp_engine)
    by_id = {item.id: item for item in items}
    assert set(by_id) == set(EXPECTED_ORDER)
    assert {item.kind for item in items} == {"bar", "news", "filing", "event", "fundamental"}

    bar = by_id["bar:5"]
    assert bar.target_ids == (INST,)
    assert bar.ts == datetime(2026, 3, 22, tzinfo=UTC)
    assert bar.title == "US:AAPL close 102.00"
    assert bar.source == "test"
    assert bar.url is None and bar.sentiment is None and bar.body is None
    assert bar.raw["close"] == 102.0

    news = by_id["news:news-1"]
    assert news.kind == "news"
    assert news.target_ids == (INST, OTHER)
    assert news.ts == datetime(2026, 3, 20, 12, 0, tzinfo=UTC)
    assert news.body == "Both companies announced a partnership."
    assert news.url == NEWS_URL

    event = by_id["event:event-1"]
    assert event.kind == "event"
    assert event.sentiment == 0.4
    assert event.source == "test/model"
    assert event.title == "earnings: Reported EPS above consensus"

    fundamental = by_id["fundamental:1"]
    assert fundamental.kind == "fundamental"
    assert fundamental.title == "eps: 1.00 (2026-01-01, edgar)"
    assert fundamental.ts == datetime(2026, 1, 1, tzinfo=UTC)
    assert fundamental.target_ids == (INST,)


def test_deterministic_order_ts_desc_then_id_asc(tmp_engine):
    _seed(tmp_engine)
    assert [item.id for item in evidence(tmp_engine)] == EXPECTED_ORDER
    again = evidence(tmp_engine)
    assert [item.id for item in again] == EXPECTED_ORDER


def test_target_filter_matches_any_target_id(tmp_engine):
    _seed(tmp_engine)
    aapl = evidence(tmp_engine, target=INST)
    msft = evidence(tmp_engine, target=OTHER)
    assert [item.id for item in msft] == [
        "bar:10",
        "bar:9",
        "news:news-1",
        "bar:8",
        "bar:7",
        "bar:6",
    ]
    assert {item.id for item in aapl} == set(EXPECTED_ORDER) - {
        "bar:6",
        "bar:7",
        "bar:8",
        "bar:9",
        "bar:10",
    }
    assert "news:news-1" in {item.id for item in aapl}
    assert all(INST in item.target_ids for item in aapl)
    assert all(OTHER in item.target_ids for item in msft)
    assert evidence(tmp_engine, target="US:NVDA") == []


def test_since_filter_is_inclusive(tmp_engine):
    _seed(tmp_engine)
    items = evidence(tmp_engine, since="2026-03-20")
    assert {item.id for item in items} == {
        "bar:3",
        "bar:4",
        "bar:5",
        "bar:8",
        "bar:9",
        "bar:10",
        "news:news-1",
        "filing:filing-1",
    }
    assert [item.id for item in evidence(tmp_engine, since="2026-03-22")] == ["bar:10", "bar:5"]
    assert [item.id for item in evidence(tmp_engine, since="2026-01-01")] == EXPECTED_ORDER


def test_kind_filters(tmp_engine):
    _seed(tmp_engine)
    assert [item.id for item in evidence(tmp_engine, kind="news")] == ["news:news-1"]
    assert [item.id for item in evidence(tmp_engine, kind="filing")] == ["filing:filing-1"]
    assert [item.id for item in evidence(tmp_engine, kind="event")] == ["event:event-1"]
    assert [item.id for item in evidence(tmp_engine, kind="fundamental")] == ["fundamental:1"]
    assert [item.id for item in evidence(tmp_engine, kind="bar")] == [
        id for id in EXPECTED_ORDER if id.startswith("bar:")
    ]
    assert evidence(tmp_engine, kind="web") == []
    combined = evidence(tmp_engine, target=INST, since="2026-03-20", kind="bar")
    assert [item.id for item in combined] == ["bar:5", "bar:4", "bar:3"]


def test_filings_detected_from_sec_edgar_source(tmp_engine):
    with Session(tmp_engine) as session:
        for row_id, source in (("n1", "rss"), ("f1", "sec_edgar")):
            session.add(
                NewsItemTable(
                    id=row_id,
                    instrument_ids=to_json([INST]),
                    published=datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
                    title=f"Item {row_id}",
                    url=f"https://example.com/{row_id}",
                    source=source,
                )
            )
        session.commit()
    items = evidence(tmp_engine)
    assert {item.id: item.kind for item in items} == {"news:n1": "news", "filing:f1": "filing"}
    assert all(item.source == "sec_edgar" for item in items if item.kind == "filing")
    assert [item.id for item in evidence(tmp_engine, kind="news")] == ["news:n1"]


def test_asx_announcements_are_primary_filings(tmp_engine):
    with Session(tmp_engine) as session:
        session.add(
            NewsItemTable(
                id="asx-1",
                instrument_ids=to_json([INST]),
                published=datetime(2026, 3, 21, tzinfo=UTC),
                title="Price sensitive announcement",
                url="https://example.com/asx-1",
                source="asx_announcements",
            )
        )
        session.commit()

    item = evidence(tmp_engine, kind="filing")[0]
    assert item.id == "filing:asx-1"
    assert item.quality == "primary"


def test_cite_is_deterministic_and_omits_url(tmp_engine):
    _seed(tmp_engine)
    by_id = {item.id: item for item in evidence(tmp_engine)}
    assert cite(by_id["news:news-1"]) == (
        "[rss] Apple and Microsoft sign cloud deal <https://example.com/news-1>"
    )
    assert (
        cite(by_id["filing:filing-1"])
        == "[sec_edgar] Apple 10-K filed <https://example.com/filing-1>"
    )
    assert cite(by_id["bar:5"]) == "[test] US:AAPL close 102.00"
    assert cite(by_id["event:event-1"]) == "[test/model] earnings: Reported EPS above consensus"
    assert "<" not in cite(by_id["bar:5"])
    again = {item.id: item for item in evidence(tmp_engine)}
    assert [cite(item) for item in again.values()] == [cite(item) for item in by_id.values()]


def test_limit_is_respected(tmp_engine):
    _seed(tmp_engine)
    assert len(evidence(tmp_engine)) == 14
    assert [item.id for item in evidence(tmp_engine, limit=3)] == [
        "bar:10",
        "bar:5",
        "filing:filing-1",
    ]
    assert evidence(tmp_engine, limit=0) == []
    assert len(evidence(tmp_engine, limit=200)) == 14


def test_evidence_by_ids_reaches_past_the_pool_window(tmp_engine):
    """Ids are read directly, so an item outside the newest-N window is still found."""
    _seed(tmp_engine)
    recent = [item.id for item in evidence(tmp_engine, limit=2)]
    aged = "fundamental:1"
    assert aged not in recent

    found = evidence_by_ids(tmp_engine, [aged, "news:news-1", "bar:1", "event:event-1"])

    assert [item.id for item in found] == ["news:news-1", "event:event-1", "bar:1", aged]


def test_evidence_by_ids_skips_unknown_and_malformed_ids(tmp_engine):
    _seed(tmp_engine)
    assert evidence_by_ids(tmp_engine, []) == []
    assert evidence_by_ids(tmp_engine, ["no-prefix", "news:nope"]) == []
