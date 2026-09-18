"""User-owned decision journal.

The journal deliberately records the context of a decision; it does not score
or recommend securities.  Keeping this screen separate from Research makes it
easy to revisit the original rationale without changing the evidence view.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Button, Input, Static

from rigger import decisions
from rigger.tui.screens.research import ResearchState
from rigger.tui.shell import RiggerScreen
from rigger.tui.widgets import Dialog, KeyStrip, Pane, PaneRow, RiggerTable


def _value(row: Any, name: str, default: Any = "") -> Any:
    """Read a model field while keeping the view tolerant of future additions."""
    return getattr(row, name, default)


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value or "—")[:10]


class DecisionForm(Dialog):
    """Small modal used for the immutable opening decision record."""

    dialog_title = "new decision"
    dialog_hint = "tab next field · enter save · esc cancel"

    DEFAULT_CSS = """
    DecisionForm Input { height: 1; border: none; border-left: thick $panel; background: $panel; padding: 0 1; margin: 0 0 1 0; }
    DecisionForm Input:focus { border-left: thick $primary; }
    DecisionForm Button { height: 1; min-width: 0; border: none; padding: 0 1; }
    """

    def compose_dialog(self) -> ComposeResult:
        yield Input(placeholder="Instrument (US:AAPL)", id="decision-instrument")
        yield Input(placeholder="Rationale / thesis", id="decision-rationale")
        yield Input(placeholder="Valuation or price context", id="decision-valuation")
        yield Input(placeholder="Time horizon (5y)", id="decision-horizon")
        yield Input(placeholder="Review date (YYYY-MM-DD)", id="decision-review-at")
        yield Input(placeholder="Invalidation criteria", id="decision-invalidation")
        yield Input(placeholder="Optional thesis ID", id="decision-thesis")
        yield Button("Record decision", id="decision-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#decision-instrument", Input).focus()

    def on_input_submitted(self) -> None:
        self.save()

    def on_button_pressed(self) -> None:
        self.save()

    def save(self) -> None:
        fields = {name: self.query_one(f"#decision-{name}", Input).value.strip() for name in (
            "instrument", "rationale", "valuation", "horizon", "review-at", "invalidation", "thesis"
        )}
        required = {"instrument": "instrument", "rationale": "rationale", "valuation": "valuation context", "horizon": "time horizon", "review-at": "review date", "invalidation": "invalidation criteria"}
        missing = [label for key, label in required.items() if not fields[key]]
        if missing:
            self.notify(f"{', '.join(missing)} required", severity="error")
            return
        try:
            review_date = date.fromisoformat(fields["review-at"])
        except ValueError:
            self.notify("review date must be YYYY-MM-DD", severity="error")
            return
        self.dismiss({
            "instrument_id": fields["instrument"], "rationale": fields["rationale"],
            "valuation_context": fields["valuation"], "time_horizon": fields["horizon"],
            "review_date": review_date, "invalidation_criteria": fields["invalidation"],
            "thesis_id": fields["thesis"] or None,
        })


class ReviewForm(Dialog):
    dialog_title = "review decision"
    dialog_hint = "enter save · esc cancel"

    def compose_dialog(self) -> ComposeResult:
        yield Input(placeholder="What changed since the decision?", id="decision-review-note")
        yield Input(placeholder="Status: open, reviewed, or retired", value="reviewed", id="decision-status")
        yield Button("Append review", id="decision-review-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#decision-review-note", Input).focus()

    def on_input_submitted(self) -> None:
        self.save()

    def on_button_pressed(self) -> None:
        self.save()

    def save(self) -> None:
        note = self.query_one("#decision-review-note", Input).value.strip()
        status = self.query_one("#decision-status", Input).value.strip().lower()
        if not note:
            self.notify("review note is required", severity="error")
        elif status not in {"open", "reviewed", "retired"}:
            self.notify("status must be open, reviewed, or retired", severity="error")
        else:
            self.dismiss({"note": note, "status": status})


class Decisions(RiggerScreen):
    name = "decisions"
    BINDINGS = [
        Binding("n", "new_decision", "new", tooltip="Record decision context"),
        Binding("r", "review", "review", tooltip="Append a dated review"),
        Binding("o", "open_research", "research", tooltip="Open its evidence"),
        Binding("escape", "back", "back", show=False),
    ]
    CSS = """
    #decisions-split { height: 1fr; }
    #decisions-list { width: 2fr; min-width: 32; }
    #decisions-table { height: 1fr; }
    #decision-detail-pane { width: 3fr; min-width: 36; }
    #decision-detail { height: 1fr; }
    #decision-keys { height: auto; }
    .decision-heading { color: $primary; text-style: bold; margin-top: 1; height: 1; }
    .decision-field { height: auto; margin-bottom: 1; }
    .decision-muted { color: $text-muted; }
    #decisions-split.-narrow > Pane { width: 1fr; min-width: 0; }
    """

    def __init__(self, rig: Any, state: ResearchState | None = None) -> None:
        super().__init__(rig)
        self.state = state or ResearchState()
        self.selected: str | None = None
        self.rows: dict[str, Any] = {}

    def compose_content(self) -> ComposeResult:
        with PaneRow(id="decisions-split"):
            with Pane(title="decisions", id="decisions-list"):
                yield RiggerTable(id="decisions-table")
            with Pane(title="original context", id="decision-detail-pane"):
                yield VerticalScroll(id="decision-detail")
        yield KeyStrip(self.BINDINGS, id="decision-keys")

    async def on_mount(self) -> None:
        table = self.query_one("#decisions-table", RiggerTable)
        table.add_column("status", width=10)
        table.add_column("review", width=10)
        table.add_column("instrument", width=14)
        table.add_column("rationale")
        await self.refresh_view()
        table.focus()

    async def refresh_view(self) -> None:
        rows = decisions.list_decisions(self.rig.engine)
        self.rows = {str(_value(row, "id")): row for row in rows}
        table = self.query_one("#decisions-table", RiggerTable)
        with self.prevent(RiggerTable.RowSelected):
            table.clear()
            if self.selected not in self.rows:
                self.selected = str(_value(rows[0], "id")) if rows else None
            for row in rows:
                table.add_row(str(_value(row, "status", "open")), _date(_value(row, "review_date")), str(_value(row, "instrument_id")), str(_value(row, "rationale")), key=str(_value(row, "id")))
            if self.selected and self.selected in self.rows:
                table.move_cursor(row=list(self.rows).index(self.selected))
        self.query_one("#decisions-list", Pane).set_badge(str(len(rows)))
        await self._render_detail()

    async def on_data_table_row_selected(self, event: RiggerTable.RowSelected) -> None:
        if event.data_table.id == "decisions-table" and event.row_key.value is not None:
            self.selected = str(event.row_key.value)
            await self._render_detail()

    async def _render_detail(self) -> None:
        pane = self.query_one("#decision-detail", VerticalScroll)
        await pane.remove_children()
        row = self.rows.get(self.selected or "")
        if row is None:
            await pane.mount(Static("No decisions yet.\n\nPress n to record the context you want to revisit.", markup=False))
            return
        fields = (("instrument", _value(row, "instrument_id")), ("status", _value(row, "status", "open")), ("created", _date(_value(row, "created_at"))), ("next review", _date(_value(row, "review_date"))), ("horizon", _value(row, "time_horizon")), ("thesis", _value(row, "thesis_id") or "—"))
        widgets: list[Any] = [Static("DECISION", classes="decision-heading")]
        widgets.extend(Static(f"{label:12}{value}", classes="decision-field", markup=False) for label, value in fields)
        widgets += [Static("RATIONALE", classes="decision-heading"), Static(str(_value(row, "rationale")), classes="decision-field", markup=False), Static("VALUATION / PRICE CONTEXT", classes="decision-heading"), Static(str(_value(row, "valuation_context")), classes="decision-field", markup=False), Static("INVALIDATION CRITERIA", classes="decision-heading"), Static(str(_value(row, "invalidation_criteria")), classes="decision-field", markup=False), Static("REVIEWS", classes="decision-heading")]
        history = decisions.review_history(self.rig.engine, str(_value(row, "id")))
        if history:
            widgets.extend(Static(f"{_date(_value(review, 'created_at'))} · {_value(review, 'status', '')}\n{_value(review, 'note')}", classes="decision-field", markup=False) for review in history)
        else:
            widgets.append(Static("No reviews yet. Press r to append one.", classes="decision-muted", markup=False))
        await pane.mount(*widgets)

    def action_new_decision(self) -> None:
        self.app.push_screen(DecisionForm(), self._save_decision)

    async def _save_decision(self, fields: dict[str, Any] | None) -> None:
        if fields is None:
            return
        try:
            row = decisions.create_decision(self.rig.engine, **fields)
        except (ValueError, KeyError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.selected = str(_value(row, "id"))
        await self.refresh_view()

    def action_review(self) -> None:
        if self.selected is None:
            self.notify("select a decision first", severity="error")
            return
        self.app.push_screen(ReviewForm(), self._save_review)

    async def _save_review(self, fields: dict[str, str] | None) -> None:
        if fields is None or self.selected is None:
            return
        try:
            decisions.append_review(self.rig.engine, self.selected, **fields)
        except (ValueError, KeyError) as exc:
            self.notify(str(exc), severity="error")
            return
        await self.refresh_view()

    def action_open_research(self) -> None:
        row = self.rows.get(self.selected or "")
        if row is None:
            self.notify("select a decision first", severity="error")
            return
        self.state.company = str(_value(row, "instrument_id"))
        self.app.action_switch_screen("data")

    def action_back(self) -> None:
        self.query_one("#decisions-table", RiggerTable).focus()
