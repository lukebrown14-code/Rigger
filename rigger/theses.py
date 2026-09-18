"""Optional long-horizon theses: a claim plus evidence for and against.

A thesis is a claim the user wants researched. Evidence for its targets is
gathered from the unified evidence pool and a structured call proposes
*candidate* evidence (side + note), stored unaccepted: only the user accepts
candidates, so discovery never auto-accepts. Tables live in this module rather
than ``core/db.py`` so the stage ships without touching shared files; every
public function creates them first, idempotently.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlmodel import Field as SQLField
from sqlmodel import Session, SQLModel, col, select

from rigger.core.ids import stable_id
from rigger.core.json import from_json, to_json
from rigger.core.time import to_utc
from rigger.evidence import EvidenceItem, cite, evidence
from rigger.llm.router import model_for

THESIS_TEMPLATE = "thesis_v1.j2"

ThesisStatus = Literal["active", "paused", "concluded"]
EvidenceSide = Literal["support", "against", "neutral"]

#: Sorted for stable "side must be one of ..." error messages.
SIDES: tuple[str, ...] = ("against", "neutral", "support")

#: Sorted for stable "status must be one of ..." error messages.
STATUSES: tuple[str, ...] = ("active", "concluded", "paused")


class Thesis(BaseModel):
    """A long-horizon claim plus the framing that makes it checkable."""

    id: str
    claim: str
    scope: str = ""
    assumptions: list[str] = Field(default_factory=list)
    falsifiers: list[str] = Field(default_factory=list)
    targets: tuple[str, ...] = ()
    time_horizon: str = ""
    created_at: datetime
    status: ThesisStatus = "active"


class ThesisEvidence(BaseModel):
    """One evidence item linked to a thesis; a candidate until the user accepts it."""

    thesis_id: str
    evidence_id: str
    side: EvidenceSide
    note: str
    accepted: bool = False


class CandidateDraft(BaseModel):
    """One (evidence_id, side, note) triple proposed by the thesis model."""

    evidence_id: str
    side: EvidenceSide
    note: str


class ThesisDraft(BaseModel):
    """The structured-call payload: candidate triples for a batch of evidence."""

    candidates: list[CandidateDraft] = Field(default_factory=list)


class ThesisTable(SQLModel, table=True):
    __tablename__ = "thesis"

    id: str = SQLField(primary_key=True)
    claim: str
    scope: str = ""
    assumptions: str
    falsifiers: str
    targets: str
    time_horizon: str = ""
    created_at: datetime
    status: str = "active"


class ThesisEvidenceTable(SQLModel, table=True):
    __tablename__ = "thesis_evidence"

    thesis_id: str = SQLField(primary_key=True)
    evidence_id: str = SQLField(primary_key=True)
    side: str
    note: str
    accepted: bool = False


def thesis_id(claim: str, scope: str) -> str:
    """Stable id: the same claim with the same scope is the same thesis."""
    return stable_id("thesis", claim, scope)


def _ensure_tables(engine: Engine) -> None:
    """Create this module's tables when missing; ``create_all`` checks first, so repeat-safe."""
    SQLModel.metadata.create_all(engine)


def _thesis_from_row(row: ThesisTable) -> Thesis:
    return Thesis(
        id=row.id,
        claim=row.claim,
        scope=row.scope,
        assumptions=from_json(row.assumptions),
        falsifiers=from_json(row.falsifiers),
        targets=tuple(from_json(row.targets)),
        time_horizon=row.time_horizon,
        created_at=to_utc(row.created_at),
        status=cast(ThesisStatus, row.status),
    )


def _evidence_from_row(row: ThesisEvidenceTable) -> ThesisEvidence:
    return ThesisEvidence(
        thesis_id=row.thesis_id,
        evidence_id=row.evidence_id,
        side=cast(EvidenceSide, row.side),
        note=row.note,
        accepted=row.accepted,
    )


def create_thesis(
    engine: Engine,
    claim: str,
    *,
    scope: str = "",
    assumptions: Sequence[str] = (),
    falsifiers: Sequence[str] = (),
    targets: Sequence[str] = (),
    time_horizon: str = "",
) -> Thesis:
    """Store a new active thesis; the same claim and scope twice is an error."""
    tid = thesis_id(claim, scope)
    created_at = datetime.now(UTC)
    _ensure_tables(engine)
    with Session(engine) as session:
        if session.get(ThesisTable, tid) is not None:
            raise ValueError(f"thesis {tid} already exists")
        session.add(
            ThesisTable(
                id=tid,
                claim=claim,
                scope=scope,
                assumptions=to_json(list(assumptions)),
                falsifiers=to_json(list(falsifiers)),
                targets=to_json(list(targets)),
                time_horizon=time_horizon,
                created_at=created_at,
                status="active",
            )
        )
        session.commit()
    return Thesis(
        id=tid,
        claim=claim,
        scope=scope,
        assumptions=list(assumptions),
        falsifiers=list(falsifiers),
        targets=tuple(targets),
        time_horizon=time_horizon,
        created_at=created_at,
        status="active",
    )


def list_theses(engine: Engine) -> list[Thesis]:
    """All theses, oldest first."""
    _ensure_tables(engine)
    with Session(engine) as session:
        stmt = select(ThesisTable).order_by(ThesisTable.created_at.asc(), ThesisTable.id)  # type: ignore[attr-defined]
        return [_thesis_from_row(row) for row in session.exec(stmt).all()]


def get_thesis(engine: Engine, id: str) -> Thesis:
    """One thesis by id or unique id prefix; ``KeyError`` when unknown.

    The TUI shows ids truncated, so a prefix is what a user has to hand.
    """
    _ensure_tables(engine)
    with Session(engine) as session:
        row = session.get(ThesisTable, id)
        if row is None:
            matches = session.exec(
                select(ThesisTable).where(col(ThesisTable.id).startswith(id))
            ).all()
            if len(matches) > 1:
                raise KeyError(f"ambiguous thesis id prefix: {id}")
            if not matches:
                raise KeyError(f"unknown thesis: {id}")
            row = matches[0]
        return _thesis_from_row(row)


def set_status(engine: Engine, id: str, status: str) -> Thesis:
    """Pause, conclude, or reactivate a thesis; returns the updated model."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}; got {status!r}")
    _ensure_tables(engine)
    with Session(engine) as session:
        row = session.get(ThesisTable, id)
        if row is None:
            raise KeyError(f"unknown thesis: {id}")
        row.status = status
        session.commit()
        return _thesis_from_row(row)


def update_thesis(
    engine: Engine,
    id: str,
    *,
    claim: str,
    targets: Sequence[str],
    time_horizon: str,
    status: str,
    scope: str | None = None,
    assumptions: Sequence[str] | None = None,
    falsifiers: Sequence[str] | None = None,
) -> Thesis:
    """Update a thesis and retain its linked evidence if its identity changes.

    A thesis id is derived from its claim and scope. Editing either therefore
    changes the id; evidence rows are moved in the same transaction so editing
    does not silently discard a review history.

    ``scope``, ``assumptions`` and ``falsifiers`` default to ``None``, meaning
    "leave as stored" — a caller that does not collect a field cannot erase it.
    """
    claim = claim.strip()
    if not claim:
        raise ValueError("claim is required")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}; got {status!r}")
    _ensure_tables(engine)
    renamed = False
    old_id = id
    with Session(engine) as session:
        row = session.get(ThesisTable, id)
        if row is None:
            raise KeyError(f"unknown thesis: {id}")
        new_scope = row.scope if scope is None else scope.strip()
        new_assumptions = row.assumptions if assumptions is None else to_json(list(assumptions))
        new_falsifiers = row.falsifiers if falsifiers is None else to_json(list(falsifiers))
        # One spelling of the column set, so a new field cannot be added to the
        # rename branch and forgotten in the in-place one.
        values = {
            "claim": claim,
            "scope": new_scope,
            "assumptions": new_assumptions,
            "falsifiers": new_falsifiers,
            "targets": to_json(list(targets)),
            "time_horizon": time_horizon,
            "status": status,
        }
        new_id = thesis_id(claim, new_scope)
        if new_id != row.id:
            if session.get(ThesisTable, new_id) is not None:
                raise ValueError(f"thesis {new_id} already exists")
            evidence_rows = session.exec(
                select(ThesisEvidenceTable).where(ThesisEvidenceTable.thesis_id == row.id)
            ).all()
            for evidence_row in evidence_rows:
                session.delete(evidence_row)
                session.add(
                    ThesisEvidenceTable(
                        thesis_id=new_id,
                        evidence_id=evidence_row.evidence_id,
                        side=evidence_row.side,
                        note=evidence_row.note,
                        accepted=evidence_row.accepted,
                    )
                )
            session.delete(row)
            row = ThesisTable(id=new_id, created_at=row.created_at, **values)
            session.add(row)
            renamed = True
        else:
            for column, value in values.items():
                setattr(row, column, value)
        session.commit()
        thesis = _thesis_from_row(row)
    if renamed:
        # Keep optional decision-journal links current while retaining each
        # decision's original claim snapshot for its historical rationale.
        from rigger.decisions import relink_thesis

        relink_thesis(engine, old_id, thesis.id, thesis.claim)
    return thesis


def add_evidence(
    engine: Engine,
    thesis_id: str,
    evidence_id: str,
    side: str,
    note: str,
    *,
    accepted: bool | None = None,
) -> ThesisEvidence:
    """Link evidence to a thesis, inserting or updating the (thesis, evidence) row.

    ``accepted=None`` keeps an existing link's accepted flag, so re-linking to
    correct a side or note does not silently drop the item out of the thesis.
    New links default to not accepted.
    """
    if side not in SIDES:
        raise ValueError(f"side must be one of {', '.join(SIDES)}; got {side!r}")
    _ensure_tables(engine)
    with Session(engine) as session:
        if session.get(ThesisTable, thesis_id) is None:
            raise KeyError(f"unknown thesis: {thesis_id}")
        row = session.get(ThesisEvidenceTable, (thesis_id, evidence_id))
        if row is None:
            effective = bool(accepted)
            session.add(
                ThesisEvidenceTable(
                    thesis_id=thesis_id,
                    evidence_id=evidence_id,
                    side=side,
                    note=note,
                    accepted=effective,
                )
            )
        else:
            effective = row.accepted if accepted is None else accepted
            row.side = side
            row.note = note
            row.accepted = effective
        session.commit()
    return ThesisEvidence(
        thesis_id=thesis_id,
        evidence_id=evidence_id,
        side=cast(EvidenceSide, side),
        note=note,
        accepted=effective,
    )


def set_accepted(
    engine: Engine, thesis_id: str, evidence_id: str, accepted: bool
) -> ThesisEvidence:
    """Confirm or un-confirm a candidate; only accepted evidence feeds the thesis."""
    _ensure_tables(engine)
    with Session(engine) as session:
        row = session.get(ThesisEvidenceTable, (thesis_id, evidence_id))
        if row is None:
            raise KeyError(f"unknown thesis evidence: {thesis_id}/{evidence_id}")
        row.accepted = accepted
        session.commit()
        return _evidence_from_row(row)


def remove_evidence(engine: Engine, thesis_id: str, evidence_id: str) -> None:
    """Drop a rejected candidate from the thesis."""
    _ensure_tables(engine)
    with Session(engine) as session:
        row = session.get(ThesisEvidenceTable, (thesis_id, evidence_id))
        if row is None:
            raise KeyError(f"unknown thesis evidence: {thesis_id}/{evidence_id}")
        session.delete(row)
        session.commit()


def evidence_for(
    engine: Engine, thesis_id: str, *, accepted_only: bool = True
) -> list[ThesisEvidence]:
    """Evidence linked to a thesis, id-ordered; candidates hidden unless asked for."""
    _ensure_tables(engine)
    with Session(engine) as session:
        stmt = select(ThesisEvidenceTable).where(ThesisEvidenceTable.thesis_id == thesis_id)
        if accepted_only:
            stmt = stmt.where(ThesisEvidenceTable.accepted.is_(True))  # type: ignore[attr-defined]
        stmt = stmt.order_by(ThesisEvidenceTable.evidence_id)
        return [_evidence_from_row(row) for row in session.exec(stmt).all()]


def accepted_items(
    engine: Engine, thesis_id: str, *, limit: int = 10_000
) -> list[tuple[EvidenceItem, ThesisEvidence]]:
    """Accepted evidence resolved to ``(EvidenceItem, ThesisEvidence)`` pairs.

    Resolution runs over the thesis targets (all evidence when no targets), the
    same pool discovery proposes from, so accepted ids align with stored rows.
    """
    thesis = get_thesis(engine, thesis_id)
    rows = evidence_for(engine, thesis_id, accepted_only=True)
    by_id = {item.id: item for item in _gather(engine, thesis.targets, since=None, limit=limit)}
    return [(by_id[row.evidence_id], row) for row in rows if row.evidence_id in by_id]


async def propose_evidence(
    rig: Any,
    thesis_id: str,
    *,
    since: str | None = None,
    limit: int = 100,
) -> list[ThesisEvidence]:
    """Ask the thesis model to classify fresh evidence for and against the claim.

    Gathers evidence for the thesis targets, drops ids already linked to the
    thesis, and stores the model's (evidence_id, side, note) triples as
    candidates with ``accepted=False`` — discovery never auto-accepts. Only ids
    present in the gathered set are stored.
    """
    from rigger.llm import structured as structured_mod

    engine = rig.engine
    thesis = get_thesis(engine, thesis_id)
    model = model_for(rig.cfg, "thesis")
    linked = {row.evidence_id for row in evidence_for(engine, thesis_id, accepted_only=False)}
    fresh = [
        item
        for item in _gather(engine, thesis.targets, since=since, limit=limit)
        if item.id not in linked
    ]
    if not fresh:
        return []

    draft, _result = await structured_mod.structured(
        rig.llm,
        task="thesis",
        model=model,
        template=THESIS_TEMPLATE,
        vars={
            "claim": thesis.claim,
            "scope": thesis.scope,
            "assumptions": thesis.assumptions,
            "falsifiers": thesis.falsifiers,
            "time_horizon": thesis.time_horizon,
            "items": [{"id": item.id, "cite": cite(item)} for item in fresh],
        },
        schema=ThesisDraft,
    )

    fresh_ids = {item.id for item in fresh}
    stored: list[ThesisEvidence] = []
    seen: set[str] = set()
    for candidate in draft.candidates:
        if candidate.evidence_id not in fresh_ids or candidate.evidence_id in seen:
            continue
        seen.add(candidate.evidence_id)
        stored.append(
            add_evidence(
                engine,
                thesis_id,
                candidate.evidence_id,
                candidate.side,
                candidate.note,
                accepted=False,
            )
        )
    return stored


def _gather(
    engine: Engine, targets: tuple[str, ...], *, since: str | None, limit: int
) -> list[EvidenceItem]:
    """Evidence across the thesis targets (all evidence when no targets), newest first.

    Per-target reads are deduped by id, re-ordered ``ts`` desc then ``id`` asc,
    and capped at ``limit`` overall.
    """
    if targets:
        items: list[EvidenceItem] = []
        for target in targets:
            items += evidence(engine, target=target, since=since, limit=limit)
    else:
        items = evidence(engine, since=since, limit=limit)
    by_id: dict[str, EvidenceItem] = {}
    for item in items:
        by_id.setdefault(item.id, item)
    merged = list(by_id.values())
    merged.sort(key=lambda item: item.id)
    merged.sort(key=lambda item: item.ts, reverse=True)
    return merged[: max(limit, 0)]
