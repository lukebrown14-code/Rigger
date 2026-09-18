"""A user-owned, append-only investment decision journal.

The journal intentionally stores the investor's original framing separately
from later reviews.  It is a record of process, not a recommendation engine.
Like :mod:`rigger.theses`, this module owns its tables and creates them
idempotently so existing databases need no migration step.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal, cast
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, col, select

from rigger.core.time import to_utc

DecisionStatus = Literal["open", "reviewed", "retired"]
DECISION_STATUSES: tuple[str, ...] = ("open", "retired", "reviewed")


class Decision(BaseModel):
    """The immutable original framing plus its current review status."""

    id: str
    instrument_id: str
    rationale: str
    valuation_context: str
    time_horizon: str
    review_date: date
    invalidation_criteria: str
    thesis_id: str | None = None
    thesis_claim_snapshot: str | None = None
    created_at: datetime
    status: DecisionStatus = "open"


class DecisionReview(BaseModel):
    """One dated, append-only follow-up to a decision."""

    id: int
    decision_id: str
    note: str
    created_at: datetime
    status: DecisionStatus | None = None


class DecisionTable(SQLModel, table=True):
    __tablename__ = "decision"

    id: str = Field(primary_key=True)
    instrument_id: str = Field(index=True)
    rationale: str
    valuation_context: str
    time_horizon: str
    review_date: date = Field(index=True)
    invalidation_criteria: str
    thesis_id: str | None = Field(default=None, index=True)
    thesis_claim_snapshot: str | None = None
    created_at: datetime
    status: str = Field(default="open", index=True)


class DecisionReviewTable(SQLModel, table=True):
    __tablename__ = "decision_review"

    id: int | None = Field(default=None, primary_key=True)
    decision_id: str = Field(index=True)
    note: str
    created_at: datetime = Field(index=True)
    status: str | None = None


def _ensure_tables(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


def _decision_from_row(row: DecisionTable) -> Decision:
    return Decision(
        id=row.id,
        instrument_id=row.instrument_id,
        rationale=row.rationale,
        valuation_context=row.valuation_context,
        time_horizon=row.time_horizon,
        review_date=row.review_date,
        invalidation_criteria=row.invalidation_criteria,
        thesis_id=row.thesis_id,
        thesis_claim_snapshot=row.thesis_claim_snapshot,
        created_at=to_utc(row.created_at),
        status=cast(DecisionStatus, row.status),
    )


def _review_from_row(row: DecisionReviewTable) -> DecisionReview:
    assert row.id is not None
    return DecisionReview(
        id=row.id,
        decision_id=row.decision_id,
        note=row.note,
        created_at=to_utc(row.created_at),
        status=cast(DecisionStatus | None, row.status),
    )


def _required(name: str, value: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{name} is required")
    return result


def _validate_status(status: str) -> DecisionStatus:
    if status not in DECISION_STATUSES:
        raise ValueError(
            f"status must be one of {', '.join(DECISION_STATUSES)}; got {status!r}"
        )
    return cast(DecisionStatus, status)


def _linked_thesis(engine: Engine, thesis_id: str, instrument_id: str) -> str:
    """Return the current claim, rejecting a thesis scoped to another target."""
    # Local import avoids a module-level dependency cycle and keeps the journal
    # usable independently of the optional thesis feature.
    from rigger.theses import get_thesis

    thesis = get_thesis(engine, thesis_id)
    if thesis.targets and instrument_id not in thesis.targets:
        raise ValueError(f"thesis {thesis_id} does not target {instrument_id}")
    return thesis.claim


def create_decision(
    engine: Engine,
    instrument_id: str,
    rationale: str,
    valuation_context: str,
    time_horizon: str,
    review_date: date,
    invalidation_criteria: str,
    *,
    thesis_id: str | None = None,
    created_at: datetime | None = None,
) -> Decision:
    """Create an open decision with immutable original framing.

    If a thesis is supplied it must cover the instrument (unless it is a
    global thesis); its claim is snapshotted for historical display.
    """
    instrument_id = _required("instrument_id", instrument_id)
    rationale = _required("rationale", rationale)
    valuation_context = _required("valuation_context", valuation_context)
    time_horizon = _required("time_horizon", time_horizon)
    invalidation_criteria = _required("invalidation_criteria", invalidation_criteria)
    _ensure_tables(engine)
    snapshot = _linked_thesis(engine, thesis_id, instrument_id) if thesis_id else None
    now = to_utc(created_at or datetime.now(UTC))
    decision = Decision(
        id=uuid4().hex,
        instrument_id=instrument_id,
        rationale=rationale,
        valuation_context=valuation_context,
        time_horizon=time_horizon,
        review_date=review_date,
        invalidation_criteria=invalidation_criteria,
        thesis_id=thesis_id,
        thesis_claim_snapshot=snapshot,
        created_at=now,
    )
    with Session(engine) as session:
        session.add(
            DecisionTable(
                id=decision.id,
                instrument_id=decision.instrument_id,
                rationale=decision.rationale,
                valuation_context=decision.valuation_context,
                time_horizon=decision.time_horizon,
                review_date=decision.review_date,
                invalidation_criteria=decision.invalidation_criteria,
                thesis_id=decision.thesis_id,
                thesis_claim_snapshot=decision.thesis_claim_snapshot,
                created_at=decision.created_at,
                status=decision.status,
            )
        )
        session.commit()
    return decision


def get_decision(engine: Engine, id: str) -> Decision:
    """Return one decision or raise ``KeyError`` when it does not exist."""
    _ensure_tables(engine)
    with Session(engine) as session:
        row = session.get(DecisionTable, id)
        if row is None:
            raise KeyError(f"unknown decision: {id}")
        return _decision_from_row(row)


def list_decisions(
    engine: Engine, *, instrument_id: str | None = None, include_retired: bool = True
) -> list[Decision]:
    """List newest decisions first, optionally narrowed to one instrument."""
    _ensure_tables(engine)
    with Session(engine) as session:
        stmt = select(DecisionTable)
        if instrument_id is not None:
            stmt = stmt.where(DecisionTable.instrument_id == instrument_id)
        if not include_retired:
            stmt = stmt.where(DecisionTable.status != "retired")
        stmt = stmt.order_by(col(DecisionTable.created_at).desc(), col(DecisionTable.id))
        return [_decision_from_row(row) for row in session.exec(stmt).all()]


def append_review(
    engine: Engine,
    decision_id: str,
    note: str,
    *,
    status: str | None = None,
    created_at: datetime | None = None,
) -> DecisionReview:
    """Append a review note; the original decision fields are never modified."""
    note = _required("note", note)
    valid_status = _validate_status(status) if status is not None else None
    _ensure_tables(engine)
    with Session(engine) as session:
        decision = session.get(DecisionTable, decision_id)
        if decision is None:
            raise KeyError(f"unknown decision: {decision_id}")
        row = DecisionReviewTable(
            decision_id=decision_id,
            note=note,
            created_at=to_utc(created_at or datetime.now(UTC)),
            status=valid_status,
        )
        session.add(row)
        if valid_status is not None:
            decision.status = valid_status
        session.commit()
        session.refresh(row)
        return _review_from_row(row)


def review_history(engine: Engine, decision_id: str) -> list[DecisionReview]:
    """All reviews in chronological order; unknown decisions raise ``KeyError``."""
    _ensure_tables(engine)
    with Session(engine) as session:
        if session.get(DecisionTable, decision_id) is None:
            raise KeyError(f"unknown decision: {decision_id}")
        stmt = (
            select(DecisionReviewTable)
            .where(DecisionReviewTable.decision_id == decision_id)
            .order_by(col(DecisionReviewTable.created_at), col(DecisionReviewTable.id))
        )
        return [_review_from_row(row) for row in session.exec(stmt).all()]


def due_reviews(engine: Engine, *, as_of: date | None = None) -> list[Decision]:
    """Open/reviewed decisions due on or before ``as_of``, earliest first."""
    _ensure_tables(engine)
    cutoff = as_of or datetime.now(UTC).date()
    with Session(engine) as session:
        stmt = (
            select(DecisionTable)
            .where(DecisionTable.review_date <= cutoff)
            .where(DecisionTable.status != "retired")
            .order_by(
                col(DecisionTable.review_date),
                col(DecisionTable.created_at),
                col(DecisionTable.id),
            )
        )
        return [_decision_from_row(row) for row in session.exec(stmt).all()]


def relink_thesis(
    engine: Engine, old_id: str, new_id: str, new_claim: str
) -> int:
    """Move current thesis links after a thesis rename, preserving snapshots.

    Call this from the thesis rename transaction boundary.  Historical claim
    snapshots deliberately remain unchanged.
    """
    _ensure_tables(engine)
    with Session(engine) as session:
        rows = session.exec(select(DecisionTable).where(DecisionTable.thesis_id == old_id)).all()
        for row in rows:
            row.thesis_id = new_id
        session.commit()
        return len(rows)
