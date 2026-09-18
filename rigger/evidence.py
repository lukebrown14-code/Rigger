"""Unified read model over the source tables: one pool of cited evidence.

The existing tables (``bar``, ``newsitem``, ``event``, ``fundamental``) keep
their write paths and data plugins untouched; this module flattens them into
``EvidenceItem`` rows that reports and chat read. Web hits are a chat-stage
search tool rather than stored evidence, so they never appear here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from rigger.core.db import BarTable, EventTable, FundamentalTable, NewsItemTable
from rigger.core.json import from_json
from rigger.core.time import parse_date, to_utc

EvidenceKind = Literal["bar", "news", "filing", "fundamental", "event", "web", "note"]

# ``FILING_SOURCE`` remains for compatibility with existing callers.  New
# consumers should use the set: ASX announcements are primary disclosures too.
FILING_SOURCE = "sec_edgar"
PRIMARY_FILING_SOURCES = frozenset((FILING_SOURCE, "asx_announcements"))
# Alias used by configurable-source consumers.  A source is primary only when
# it is a recognised direct disclosure feed; installed RSS sources stay
# secondary evidence.
PRIMARY_DISCLOSURE_SOURCES = PRIMARY_FILING_SOURCES


def source_quality(source: str) -> str:
    return "primary" if source in PRIMARY_DISCLOSURE_SOURCES else "secondary"


@dataclass
class EvidenceItem:
    """One sourced fact, normalised from any source table."""

    id: str
    target_ids: tuple[str, ...]
    ts: datetime
    kind: EvidenceKind
    title: str
    body: str | None
    source: str
    url: str | None
    sentiment: float | None
    raw: dict[str, Any] = field(default_factory=dict)
    quality: str = "secondary"


def cite(item: EvidenceItem) -> str:
    """Deterministic cite text: ``[source] title <url>``, url omitted when absent."""
    text = f"[{item.source}] {item.title}"
    if item.url is not None:
        text += f" <{item.url}>"
    return text


def evidence(
    engine: Engine,
    *,
    target: str | None = None,
    since: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    search: str | None = None,
) -> list[EvidenceItem]:
    """Read evidence across all source tables.

    ``target`` keeps items whose ``target_ids`` contain it; ``since`` is an
    inclusive ``YYYY-MM-DD`` floor on ``ts``; ``kind`` filters the mapped kind.
    ``search`` matches title, body and source case-insensitively before limiting.
    Results are ordered ``ts`` desc then ``id`` asc, truncated to ``limit``.
    """
    limit = max(limit, 0)
    # Search must see older matches before the final result limit.
    read_limit = None if search else limit
    with Session(engine) as session:
        items: list[EvidenceItem] = []
        if kind is None or kind == "bar":
            items += _bars(session, target, since, read_limit)
        if kind is None or kind in ("news", "filing"):
            items += _news(session, target, since, kind, read_limit)
        if kind is None or kind == "event":
            items += _events(session, target, since, read_limit)
        if kind is None or kind == "fundamental":
            items += _fundamentals(session, target, since, read_limit)
    floor = parse_date(since) if since is not None else None
    matched = [
        item
        for item in items
        if (target is None or target in item.target_ids)
        and (floor is None or item.ts >= floor)
        and (kind is None or item.kind == kind)
    ]
    if search:
        needle = search.casefold()
        matched = [
            item
            for item in matched
            if needle in " ".join((item.title, item.body or "", item.source)).casefold()
        ]
    return _ordered(matched)[:limit]


def evidence_by_ids(engine: Engine, ids: Sequence[str]) -> list[EvidenceItem]:
    """Evidence items for the given ids, newest first; unknown ids are skipped.

    ``evidence()`` truncates to the newest ``limit`` items, so callers holding
    specific ids (thesis links, report cites) read them through here instead:
    an item that has aged out of the pool window is still found.
    """
    raw_ids: dict[str, list[str]] = {}
    for item_id in ids:
        prefix, _, rest = item_id.partition(":")
        if rest:
            raw_ids.setdefault(prefix, []).append(rest)
    if not raw_ids:
        return []
    items: list[EvidenceItem] = []
    with Session(engine) as session:
        bar_ids = raw_ids.get("bar", [])
        if bar_ids:
            stmt = select(BarTable).where(col(BarTable.id).in_(bar_ids))
            items += [_bar_item(row) for row in session.exec(stmt).all()]
        event_ids = raw_ids.get("event", [])
        if event_ids:
            stmt2 = select(EventTable).where(col(EventTable.id).in_(event_ids))
            items += [_event_item(row) for row in session.exec(stmt2).all()]
        fundamental_ids = raw_ids.get("fundamental", [])
        if fundamental_ids:
            stmt3 = select(FundamentalTable).where(col(FundamentalTable.id).in_(fundamental_ids))
            items += [_fundamental_item(row) for row in session.exec(stmt3).all()]
        # news and filing are the same table, split only by source.
        news_ids = raw_ids.get("news", []) + raw_ids.get("filing", [])
        if news_ids:
            stmt4 = select(NewsItemTable).where(col(NewsItemTable.id).in_(news_ids))
            items += [_news_item(row) for row in session.exec(stmt4).all()]
    return _ordered(items)


def _ordered(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """Newest first; items sharing a timestamp keep ascending id order."""
    items.sort(key=lambda item: item.id)
    items.sort(key=lambda item: item.ts, reverse=True)
    return items


def _bars(
    session: Session, target: str | None, since: str | None, limit: int | None
) -> list[EvidenceItem]:
    stmt = select(BarTable)
    if target is not None:
        stmt = stmt.where(BarTable.instrument_id == target)
    if since is not None:
        stmt = stmt.where(BarTable.ts >= parse_date(since))
    stmt = stmt.order_by(BarTable.ts.desc(), BarTable.id).limit(limit)  # type: ignore[attr-defined]
    return [_bar_item(row) for row in session.exec(stmt).all()]


def _news(
    session: Session, target: str | None, since: str | None, kind: str | None, limit: int | None
) -> list[EvidenceItem]:
    stmt = select(NewsItemTable)
    if target is not None:
        stmt = stmt.where(
            NewsItemTable.instrument_ids.contains(f'"{target}"', autoescape=True)  # type: ignore[attr-defined]
        )
    if since is not None:
        stmt = stmt.where(NewsItemTable.published >= parse_date(since))
    if kind == "news":
        stmt = stmt.where(NewsItemTable.source.not_in(PRIMARY_FILING_SOURCES))
    elif kind == "filing":
        stmt = stmt.where(NewsItemTable.source.in_(PRIMARY_FILING_SOURCES))
    stmt = stmt.order_by(NewsItemTable.published.desc(), NewsItemTable.id).limit(limit)  # type: ignore[attr-defined]
    return [_news_item(row) for row in session.exec(stmt).all()]


def _events(
    session: Session, target: str | None, since: str | None, limit: int | None
) -> list[EvidenceItem]:
    stmt = select(EventTable)
    if target is not None:
        stmt = stmt.where(EventTable.instrument_id == target)
    if since is not None:
        stmt = stmt.where(EventTable.ts >= parse_date(since))
    stmt = stmt.order_by(EventTable.ts.desc(), EventTable.id).limit(limit)  # type: ignore[attr-defined]
    return [_event_item(row) for row in session.exec(stmt).all()]


def _fundamentals(
    session: Session, target: str | None, since: str | None, limit: int | None
) -> list[EvidenceItem]:
    stmt = select(FundamentalTable)
    if target is not None:
        stmt = stmt.where(FundamentalTable.instrument_id == target)
    if since is not None:
        stmt = stmt.where(FundamentalTable.as_of >= parse_date(since).date())
    stmt = stmt.order_by(FundamentalTable.as_of.desc(), FundamentalTable.id).limit(limit)  # type: ignore[attr-defined]
    return [_fundamental_item(row) for row in session.exec(stmt).all()]


def _bar_item(row: BarTable) -> EvidenceItem:
    return EvidenceItem(
        id=f"bar:{row.id}",
        target_ids=(row.instrument_id,),
        ts=to_utc(row.ts),
        kind="bar",
        title=f"{row.instrument_id} close {row.close:.2f}",
        body=None,
        source=row.source,
        url=None,
        sentiment=None,
        raw={
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
        },
    )


def _news_item(row: NewsItemTable) -> EvidenceItem:
    instrument_ids = from_json(row.instrument_ids)
    kind: EvidenceKind = "filing" if row.source in PRIMARY_FILING_SOURCES else "news"
    return EvidenceItem(
        id=f"{kind}:{row.id}",
        target_ids=tuple(instrument_ids),
        ts=to_utc(row.published),
        kind=kind,
        title=row.title,
        body=row.body,
        source=row.source,
        url=row.url,
        sentiment=None,
        raw={"instrument_ids": instrument_ids},
        quality=source_quality(row.source),
    )


def _event_item(row: EventTable) -> EvidenceItem:
    return EvidenceItem(
        id=f"event:{row.id}",
        target_ids=(row.instrument_id,),
        ts=to_utc(row.ts),
        kind="event",
        title=f"{row.kind}: {row.summary}",
        body=None,
        source=row.extracted_by,
        url=None,
        sentiment=row.sentiment,
        raw={
            "kind": row.kind,
            "summary": row.summary,
            "evidence_ids": from_json(row.evidence_ids),
            "extracted_by": row.extracted_by,
            "prompt_version": row.prompt_version,
        },
    )


def _fundamental_item(row: FundamentalTable) -> EvidenceItem:
    as_of = f"{row.as_of:%Y-%m-%d}"
    return EvidenceItem(
        id=f"fundamental:{row.id}",
        target_ids=(row.instrument_id,),
        ts=parse_date(as_of),
        kind="fundamental",
        title=f"{row.metric}: {row.value:.2f} ({as_of}, {row.source})",
        body=None,
        source=row.source,
        url=None,
        sentiment=None,
        raw={"metric": row.metric, "value": row.value, "as_of": as_of},
    )
