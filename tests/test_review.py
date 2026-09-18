"""Read-only evidence quality and review-queue behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from rigger.core.db import store_items
from rigger.core.models import Bar, Instrument, NewsItem
from rigger.review import evidence_audit, primary_sources_for, review_queue
from rigger.theses import create_thesis

NOW = datetime(2026, 1, 31, tzinfo=UTC)
INST = Instrument(id="US:ACME", market="us", symbol="ACME", currency="USD")
ASX = Instrument(id="ASX:BHP", market="asx", symbol="BHP", currency="AUD")


class Rig:
    def __init__(self, engine, universe, plugins):
        self.engine = engine
        self._universe = universe
        self.plugins = plugins

    def universe(self):
        return self._universe


def _bar(inst: str, at: datetime) -> Bar:
    return Bar(instrument_id=inst, ts=at, open=1, high=1, low=1, close=1, volume=1, source="yfinance")


def _news(id: str, inst: str, at: datetime, *, source="rss", title="News", body=None) -> NewsItem:
    return NewsItem(id=id, instrument_ids=[inst], published=at, title=title, body=body, url=f"https://example.test/{id}", source=source)


def test_audit_marks_primary_not_configured_instead_of_absent(tmp_engine):
    store_items(tmp_engine, [_bar(INST.id, NOW - timedelta(days=1)), _news("n1", INST.id, NOW - timedelta(days=1))])
    audit = evidence_audit(tmp_engine, INST.id, now=NOW)
    assert audit.primary_coverage == "not_configured"
    assert "no primary disclosure source configured" in audit.warnings


def test_audit_tracks_stale_data_and_recent_source_diversity(tmp_engine):
    store_items(
        tmp_engine,
        [
            _bar(INST.id, NOW - timedelta(days=4)),
            _news("n1", INST.id, NOW - timedelta(days=15)),
            _news("f1", INST.id, NOW - timedelta(days=31), source="sec_edgar"),
        ],
    )
    audit = evidence_audit(tmp_engine, INST.id, now=NOW, primary_sources={"sec_edgar"})
    assert audit.primary_coverage == "stale"
    assert audit.non_price_items == 1
    assert audit.source_count == 1
    assert "latest price is 4 days old" in audit.warnings
    assert "latest news is 15 days old" in audit.warnings
    assert "thin recent evidence" in audit.warnings
    assert "low source diversity" in audit.warnings


def test_primary_source_scope_and_asx_filing_classification(tmp_engine):
    rig = Rig(
        tmp_engine,
        [INST, ASX],
        {"sec_edgar": SimpleNamespace(enabled=True), "asx_announcements": SimpleNamespace(enabled=True)},
    )
    assert primary_sources_for(rig, INST.id) == frozenset({"sec_edgar"})
    assert primary_sources_for(rig, ASX.id) == frozenset({"asx_announcements"})
    store_items(tmp_engine, [_news("asx1", ASX.id, NOW - timedelta(days=1), source="asx_announcements")])
    audit = evidence_audit(tmp_engine, ASX.id, now=NOW, primary_sources=primary_sources_for(rig, ASX.id))
    assert audit.primary_coverage == "fresh"


def test_queue_ranks_disclosures_then_falsifiers_then_coverage(tmp_engine):
    rig = Rig(tmp_engine, [INST], {"sec_edgar": SimpleNamespace(enabled=True)})
    thesis = create_thesis(tmp_engine, "Revenue keeps rising", targets=(INST.id,), falsifiers=("guidance cut",))
    store_items(
        tmp_engine,
        [
            _bar(INST.id, NOW - timedelta(days=5)),
            _news("filing", INST.id, NOW - timedelta(days=1), source="sec_edgar", title="10-K filed"),
            _news("match", INST.id, NOW - timedelta(days=1), title="Company guidance cut", body="guidance cut"),
            _news("future", INST.id, NOW + timedelta(days=1), source="sec_edgar", title="Future filing"),
        ],
    )
    queue = review_queue(rig, since=NOW - timedelta(days=2), now=NOW)
    assert [(item.kind, item.evidence_id) for item in queue[:2]] == [
        ("primary_disclosure", "filing:filing"),
        ("falsifier", "news:match"),
    ]
    assert queue[1].thesis_id == thesis.id
    assert "filing:future" not in {item.evidence_id for item in queue}
    assert [item.kind for item in queue[2:]] == ["stale", "thin_evidence"]


def test_queue_is_deterministic_and_deduplicates_each_navigation_target(tmp_engine):
    rig = Rig(tmp_engine, [INST], {"sec_edgar": SimpleNamespace(enabled=True)})
    store_items(
        tmp_engine,
        [
            _news("b", INST.id, NOW - timedelta(days=1), source="sec_edgar", title="B"),
            _news("a", INST.id, NOW - timedelta(days=1), source="sec_edgar", title="A"),
        ],
    )
    first = review_queue(rig, since=NOW - timedelta(days=2), now=NOW)
    second = review_queue(rig, since=NOW - timedelta(days=2), now=NOW)
    assert first == second
    assert [item.evidence_id for item in first if item.kind == "primary_disclosure"] == ["filing:a", "filing:b"]
