"""Focused persistence tests for the append-only decision journal."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from rigger import decisions, theses

INST = "US:AAPL"
OTHER = "US:MSFT"
NOW = datetime(2026, 3, 22, 12, tzinfo=UTC)


def _create(engine, **overrides):
    values = {
        "instrument_id": INST,
        "rationale": "Services revenue is growing.",
        "valuation_context": "Trading at 20x forward earnings.",
        "time_horizon": "5 years",
        "review_date": date(2026, 4, 1),
        "invalidation_criteria": "Services revenue growth turns negative.",
        "created_at": NOW,
    }
    values.update(overrides)
    return decisions.create_decision(engine, **values)


def test_create_list_and_get_round_trip(tmp_engine):
    created = _create(tmp_engine)

    assert created.status == "open"
    assert created.created_at == NOW
    assert decisions.get_decision(tmp_engine, created.id) == created
    assert decisions.list_decisions(tmp_engine) == [created]
    with pytest.raises(KeyError, match="unknown decision"):
        decisions.get_decision(tmp_engine, "nope")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instrument_id", " "),
        ("rationale", " "),
        ("valuation_context", " "),
        ("time_horizon", " "),
        ("invalidation_criteria", " "),
    ],
)
def test_original_framing_fields_are_required(tmp_engine, field, value):
    with pytest.raises(ValueError, match=field):
        _create(tmp_engine, **{field: value})


def test_reviews_append_without_changing_original_framing(tmp_engine):
    created = _create(tmp_engine)
    later = decisions.append_review(
        tmp_engine,
        created.id,
        "Revenue remains on plan.",
        status="reviewed",
        created_at=NOW + timedelta(days=1),
    )
    earlier = decisions.append_review(
        tmp_engine,
        created.id,
        "Initial check.",
        created_at=NOW + timedelta(hours=1),
    )

    stored = decisions.get_decision(tmp_engine, created.id)
    assert stored.rationale == created.rationale
    assert stored.valuation_context == created.valuation_context
    assert stored.invalidation_criteria == created.invalidation_criteria
    assert stored.status == "reviewed"
    assert [review.id for review in decisions.review_history(tmp_engine, created.id)] == [
        earlier.id,
        later.id,
    ]
    assert later.status == "reviewed"


def test_due_reviews_order_and_retirement(tmp_engine):
    late = _create(tmp_engine, review_date=date(2026, 3, 20), created_at=NOW)
    early = _create(tmp_engine, review_date=date(2026, 3, 19), created_at=NOW + timedelta(seconds=1))
    future = _create(tmp_engine, review_date=date(2026, 3, 23), created_at=NOW + timedelta(seconds=2))
    decisions.append_review(tmp_engine, late.id, "No longer applicable.", status="retired")

    assert [item.id for item in decisions.due_reviews(tmp_engine, as_of=date(2026, 3, 22))] == [
        early.id
    ]
    assert future.id not in {item.id for item in decisions.due_reviews(tmp_engine, as_of=date(2026, 3, 22))}


def test_thesis_link_is_compatible_and_snapshotted(tmp_engine):
    thesis = theses.create_thesis(tmp_engine, "Apple services keep growing.", targets=(INST,))
    created = _create(tmp_engine, thesis_id=thesis.id)

    assert created.thesis_id == thesis.id
    assert created.thesis_claim_snapshot == thesis.claim
    incompatible = theses.create_thesis(tmp_engine, "Microsoft grows.", targets=(OTHER,))
    with pytest.raises(ValueError, match="does not target"):
        _create(tmp_engine, thesis_id=incompatible.id)


def test_relink_thesis_preserves_historical_snapshot(tmp_engine):
    thesis = theses.create_thesis(tmp_engine, "Apple services keep growing.", targets=(INST,))
    created = _create(tmp_engine, thesis_id=thesis.id)
    new_id = "renamed-thesis"

    assert decisions.relink_thesis(tmp_engine, thesis.id, new_id, "New claim") == 1
    stored = decisions.get_decision(tmp_engine, created.id)
    assert stored.thesis_id == new_id
    assert stored.thesis_claim_snapshot == thesis.claim


def test_thesis_rename_relinks_decision_but_preserves_snapshot(tmp_engine):
    thesis = theses.create_thesis(tmp_engine, "Apple services keep growing.", targets=(INST,))
    created = _create(tmp_engine, thesis_id=thesis.id)

    renamed = theses.update_thesis(
        tmp_engine,
        thesis.id,
        claim="Apple services keep compounding.",
        targets=(INST,),
        time_horizon="5 years",
        status="active",
    )

    stored = decisions.get_decision(tmp_engine, created.id)
    assert stored.thesis_id == renamed.id
    assert stored.thesis_claim_snapshot == thesis.claim


def test_public_apis_create_tables_idempotently(tmp_engine):
    assert decisions.list_decisions(tmp_engine) == []
    created = _create(tmp_engine)
    assert decisions.review_history(tmp_engine, created.id) == []
    assert decisions.due_reviews(tmp_engine, as_of=date(2026, 4, 1)) == [created]
