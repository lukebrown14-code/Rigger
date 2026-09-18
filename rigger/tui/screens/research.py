"""Company research: browse the evidence pool and follow report citations."""

from __future__ import annotations

import asyncio
from asyncio import CancelledError
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Markdown, MarkdownViewer, Static
from textual.widgets._markdown import MarkdownBlock
from textual.worker import Worker, get_current_worker

from rigger import review, services, theses
from rigger.evidence import EvidenceItem, evidence, evidence_by_ids
from rigger.llm.router import model_for
from rigger.plugins.data.yfinance import DEFAULT_SUFFIXES
from rigger.quotes import YahooQuotes
from rigger.reports import (
    _SECTIONS,
    Report,
    build_report,
    read_report,
    render_markdown,
    report_history,
    section_counts,
    write_report,
)
from rigger.tui.shell import RiggerScreen, age_text
from rigger.tui.widgets import (
    ActionChip,
    Pane,
    PaneRow,
    Pill,
    RiggerTable,
    StatusDot,
    hint_markup,
    sentiment_variant,
    token_color,
)

#: Evidence kind -> the theme token that colours its Type cell. The colour
#: sits on that cell alone, never the whole row, so the block cursor stays
#: readable over it.
KIND_TOKENS = {
    "news": "kind-news",
    "filing": "kind-filing",
    "event": "kind-event",
    "fundamental": "kind-fundamental",
    "bar": "kind-price",
}

#: What a kind is called on screen. The store calls a daily close a ``bar``;
#: nobody reading a research pane does.
KIND_LABELS = {"bar": "price", "fundamental": "fundam."}

#: Keys of ``EvidenceItem.raw`` that are plumbing, not evidence: they go to a
#: dim provenance footer instead of the value table.
INTERNAL_RAW = ("id", "content_hash", "extracted_by", "prompt_version", "evidence_ids")

#: Evidence kinds in the order ``k`` cycles them; label, filter value.
KINDS: tuple[tuple[str, str], ...] = (
    ("all", "all"),
    ("news", "news"),
    ("filings", "filing"),
    ("prices", "bar"),
    ("fundamentals", "fundamental"),
    ("events", "event"),
)


@dataclass
class CompanyView:
    search: str = ""
    kind: str = "all"
    #: Price runs the reader has unfolded, by the key of their group row.
    expanded: set[str] = field(default_factory=set)
    limit: int = 200
    selected: str = ""
    inspected: str = ""
    report_y: float = 0
    list_y: float = 0
    preview_y: float = 0


@dataclass
class ResearchState:
    target: str = ""
    company: str = ""
    companies: dict[str, CompanyView] = field(default_factory=dict)
    busy: bool = False
    activity: str = ""


class ResearchViewer(MarkdownViewer):
    def on_show(self) -> None:
        self.screen.restore_report_position()

    async def _on_markdown_link_clicked(self, message: Markdown.LinkClicked) -> None:
        message.prevent_default()
        if message.href.startswith("evidence:"):
            message.stop()
            await self.screen.inspect_evidence(message.href.removeprefix("evidence:"))
        elif message.href.startswith("thesis:"):
            message.stop()
            self.screen.promote_claim(message.href.removeprefix("thesis:"))
        elif message.href.startswith(("https://", "http://")):
            message.stop()
            self.app.open_url(message.href)
        elif message.href.startswith("#"):
            await super()._on_markdown_link_clicked(message)
        else:
            message.stop()


class Research(RiggerScreen):
    #: Every button here has a key. The keys dodge the app-level bindings in
    #: ``RiggerApp`` (1-5, c, h, m, p, g, q) so the global navigation still
    #: works from this screen — notably ``g``, which is the Go picker.
    BINDINGS = [
        ("e", "show_evidence", "evidence"),
        ("r", "show_report", "report"),
        ("n", "generate_report", "generate report"),
        ("u", "update_evidence", "gather company"),
        ("U", "gather_all", "gather all targets"),
        ("slash", "focus_search", "search evidence"),
        ("k", "cycle_kind", "kind"),
        ("l", "load_more", "load more"),
        ("space", "fold_prices", "fold prices"),
        ("t", "focus_targets", "company"),
        ("o", "open_source", "open link"),
        ("v", "view_in_report", "view in report"),
        ("escape", "back", "back"),
    ]
    CSS = """
    /* Header: one company list, seven rows including its frame (four rows
       of companies at 120x40, two at 80x24 — see layout_views). */
    #research-header { height: 7; }
    #research-header.-narrow { height: 5; }
    #research-companies { height: 1fr; width: 1fr; overflow-y: auto; }
    #research-actions { height: 1; margin: 0 1; }
    #research-actions .chip-gap { width: 1fr; height: 1; }
    #research-audit { height: 1; margin: 0 1; color: $warning; text-wrap: nowrap; text-overflow: ellipsis; }
    #evidence-layout, #report-doc { height: 1fr; }
    #evidence-list-pane { width: 2fr; }
    #evidence-preview-pane { width: 3fr; }
    #evidence-filters { height: 1; margin: 0 0 0 0; }
    #evidence-search { width: 1fr; }
    #evidence-kind { width: auto; height: 1; padding: 0 1; color: $text-muted; }
    #evidence-table { height: 1fr; }
    #evidence-preview, #report-view { height: 1fr; }
    #evidence-count { height: 1; padding: 0 1; color: $text-muted; }
    #source-body { height: auto; padding: 0 1; }
    #report-meta { height: 1; padding: 0 1; }
    #report-meta StatusDot { width: 2; }
    #report-meta Static, #report-meta Pill { width: auto; padding: 0 1 0 0; }
    #report-age, #report-sentiment-delta { color: $text-muted; }
    #report-history, #report-legacy { height: auto; padding: 0 1; color: $text-muted; }
    """
    initial_tab = "evidence"

    def __init__(self, rig: Any, state: ResearchState | None = None) -> None:
        super().__init__(rig)
        self.state = state or ResearchState()
        self.tab = self.initial_tab
        self.report: Report | None = None
        self.items: dict[str, EvidenceItem] = {}
        self.groups: dict[str, list[EvidenceItem]] = {}
        self.ready = False
        self.detail_open = False
        self.more_available = False
        self.can_open = False
        self.can_view = False
        self._rendered_company = ""
        self.status_text = ""
        self._claim_to_reveal = ""
        self._job: Worker | None = None
        self.feed: YahooQuotes | None = None
        self.feed_task: asyncio.Task[None] | None = None
        self.feed_company: tuple[str, ...] = ()
        self.quote_state = ""

    @property
    def view(self) -> CompanyView:
        return self.state.companies.setdefault(self.state.company, CompanyView())

    def compose_content(self) -> ComposeResult:
        with Pane(
            title="company",
            key="t",
            hints=hint_markup(("↑↓", "select"), ("enter", "open")),
            id="research-header",
        ):
            yield RiggerTable(id="research-companies")
        with Horizontal(id="research-actions"):
            yield ActionChip("e", "evidence", id="tab-evidence")
            yield ActionChip("r", "report", id="tab-report")
            yield Static("", classes="chip-gap")
            yield ActionChip("u", "gather company", id="research-refresh")
            yield ActionChip("U", "gather all", id="research-gather")
            yield ActionChip("n", "generate report", id="report-generate", classes="-primary")
        yield Static("", id="research-audit", markup=False)
        with PaneRow(id="evidence-layout"):
            with Pane(
                title="evidence",
                key="e",
                hints=hint_markup(("/", "search"), ("k", "kind"), ("space", "fold"), ("l", "more")),
                id="evidence-list-pane",
            ):
                with Horizontal(id="evidence-filters"):
                    yield Input(placeholder="/ search evidence", id="evidence-search")
                    yield Static("", id="evidence-kind")
                yield RiggerTable(id="evidence-table")
                yield Static("", id="evidence-count", markup=False)
            with Pane(
                title="preview",
                hints=hint_markup(("o", "open link"), ("v", "view in report"), ("esc", "back")),
                id="evidence-preview-pane",
            ):
                with VerticalScroll(id="evidence-preview"):
                    yield Static("select evidence to preview it", id="source-body", markup=False)
        with Pane(
            title="report",
            key="r",
            hints=hint_markup(("↑↓", "scroll"), ("enter", "follow citation"), ("n", "regenerate")),
            id="report-doc",
        ):
            with Horizontal(id="report-meta"):
                yield StatusDot("warn", id="report-age-dot")
                yield Static("", id="report-age", markup=False)
                yield Pill("", id="report-sentiment")
                yield Static("", id="report-sentiment-delta", markup=False)
            yield Static("", id="report-history", markup=False)
            yield Static("", id="report-legacy", markup=False)
            yield ResearchViewer(id="report-view", show_table_of_contents=True, open_links=False)

    async def on_mount(self) -> None:
        companies = self.query_one("#research-companies", RiggerTable)
        for label in ("Company", "Symbol", "Target", "Report", "Live"):
            companies.add_column(label, key=label.casefold())
        # Fixed widths for the two narrow columns: a long row in Evidence must
        # never push Type (which carries the kind colour) or Date off the pane.
        table = self.query_one("#evidence-table", RiggerTable)
        table.add_column("Evidence", key="source")
        table.add_column("Type", key="type", width=11)
        table.add_column("Date", key="date", width=10)
        self.ready = True
        self.set_interval(2, self.render_status)
        await self.refresh_view()

    def save_position(self) -> None:
        if self.ready and self._rendered_company:
            old = self.state.companies.setdefault(self._rendered_company, CompanyView())
            if self.tab == "report":
                old.report_y = self.query_one("#report-view", MarkdownViewer).scroll_y
            else:
                if self.query_one("#evidence-list-pane").display:
                    old.list_y = self.query_one("#evidence-table", RiggerTable).scroll_y
                if self.query_one("#evidence-preview-pane").display:
                    old.preview_y = self.query_one("#evidence-preview", VerticalScroll).scroll_y

    def on_screen_suspend(self) -> None:
        self.save_position()
        # Drop the socket when the panel is not visible; resuming re-subscribes
        # through refresh_view -> load_company.
        self.stop_quotes()

    def companies(self) -> list[Any]:
        """Every instrument under a configured target, grouped by target.

        You research a company, not a target: the target is a column here.
        Sorting by target first keeps a sector's members adjacent.
        """
        specs = services.target_specs()
        rows = [
            (target, instrument)
            for instrument in self.rig.universe()
            for target in instrument.watchlists
            if target in specs
        ]
        rows.sort(key=lambda row: (row[0], row[1].id))
        seen: set[str] = set()
        unique = []
        for target, instrument in rows:
            if instrument.id not in seen:
                seen.add(instrument.id)
                unique.append((target, instrument))
        return unique

    async def refresh_view(self) -> None:
        if not self.ready:
            return
        specs = services.target_specs()
        rows = self.companies()
        table = self.query_one("#research-companies", RiggerTable)
        with self.prevent(RiggerTable.RowHighlighted):
            table.clear()
            for target, instrument in rows:
                spec = specs[target]
                table.add_row(
                    getattr(instrument, "name", "") or instrument.symbol,
                    instrument.id,
                    f"{target} · {spec.kind}",
                    self.company_report_age(instrument.id),
                    "",
                    key=instrument.id,
                )
        ids = [instrument.id for _target, instrument in rows]
        if self.state.company not in ids:
            self.state.company = ids[0] if ids else ""
        self.state.target = next((t for t, i in rows if i.id == self.state.company), "")
        self.query_one("#research-header", Pane).set_badge(str(len(ids)))
        if self.state.company:
            with self.prevent(RiggerTable.RowHighlighted):
                table.move_cursor(row=ids.index(self.state.company))
        await self.load_company()

    def company_report_age(self, company: str) -> Text:
        """``date · age`` of the newest report, so the list answers "what is stale?"."""
        stamp = services.latest_report(self.reports_dir(), company)
        if stamp is None:
            return Text("no report", style=token_color(self.app, "text-muted"))
        if stamp.as_of is None:
            # A legacy report whose filename is not a date: show the stem, which
            # is all it records, rather than invent an age for it.
            return Text(stamp.path.stem)
        label, state = age_text(datetime.now(UTC) - stamp.as_of)
        colour = {
            "ok": token_color(self.app, "foreground"),
            "warn": token_color(self.app, "text-warning"),
        }.get(state, token_color(self.app, "text-error"))
        return Text.assemble(f"{stamp.path.stem} ", (label, colour))

    async def load_company(self) -> None:
        self._rendered_company = self.state.company
        with self.prevent(Input.Changed):
            self.query_one("#evidence-search", Input).value = self.view.search
        self.render_kind()
        for button in ("#report-generate", "#research-gather", "#research-refresh"):
            self.query_one(button, Button).disabled = self.state.busy
        self.detail_open = False
        await self.show_latest(self.state.company)
        self.show_audit(self.state.company)
        self.load_evidence()
        if self.view.inspected:
            await self.inspect_evidence(self.view.inspected, save=False)
        self.show_tab(self.tab)
        self.start_quotes(self.state.company)
        self.call_after_refresh(self.restore_position)

    def restore_position(self) -> None:
        self.query_one("#report-view", MarkdownViewer).scroll_to(
            y=self.view.report_y, animate=False
        )
        self.query_one("#evidence-table", RiggerTable).scroll_to(y=self.view.list_y, animate=False)
        self.query_one("#evidence-preview", VerticalScroll).scroll_to(
            y=self.view.preview_y, animate=False
        )

    async def on_data_table_row_highlighted(self, event: RiggerTable.RowHighlighted) -> None:
        # A table that empties posts a highlight with no row (cursor_row -1).
        if event.row_key is None or event.row_key.value is None:
            return
        value = str(event.row_key.value)
        if event.data_table.id == "research-companies":
            if value != self.state.company:
                self.save_position()
                self.state.company = value
                self.state.target = next(
                    (t for t, i in self.companies() if i.id == value), self.state.target
                )
                await self.load_company()
        elif value in self.items:
            self.view.selected = value
            self.view.inspected = ""
            self.preview(self.items[value])
        elif value in self.groups:
            self._preview_row(value)

    def on_data_table_row_selected(self, event: RiggerTable.RowSelected) -> None:
        if event.data_table.id == "evidence-table":
            self.detail_open = True
            self.layout_views()

    def render_kind(self) -> None:
        label = next((label for label, value in KINDS if value == self.view.kind), self.view.kind)
        self.query_one("#evidence-kind", Static).update(f"kind: {label}")

    def action_cycle_kind(self) -> None:
        self.show_tab("evidence")
        values = [value for _label, value in KINDS]
        index = values.index(self.view.kind) if self.view.kind in values else 0
        self.view.kind = values[(index + 1) % len(values)]
        self.view.limit = 200
        self.view.inspected = ""
        self.render_kind()
        self.load_evidence()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.ready and event.input.id == "evidence-search" and event.value != self.view.search:
            self.view.search = event.value
            self.view.limit = 200
            self.view.inspected = ""
            self.load_evidence()

    def _fold_prices(self, rows: list[EvidenceItem]) -> list[tuple[str, Any]]:
        """Collapse each run of consecutive price bars into one group row.

        A company's pool is mostly closes — 180 of 200 rows on a seeded
        target — and one colour of them buries the filings and news that are
        the reason to open this pane. A run becomes a single row that says how
        many and over what dates; ``space`` unfolds it.
        """
        folded: list[tuple[str, Any]] = []
        run: list[EvidenceItem] = []

        def flush() -> None:
            if not run:
                return
            if len(run) == 1:
                folded.append(("item", run[0]))
            else:
                key = f"prices:{run[0].id}"
                folded.append(("group", (key, list(run))))
                if key in self.view.expanded:
                    folded.extend(("child", item) for item in run)
            run.clear()

        for item in rows:
            if item.kind == "bar":
                run.append(item)
                continue
            flush()
            folded.append(("item", item))
        flush()
        return folded

    def _kind_cell(self, kind: str) -> Text:
        return Text(
            KIND_LABELS.get(kind, kind),
            style=token_color(self.app, KIND_TOKENS.get(kind, "text-muted")),
        )

    def load_evidence(self) -> None:
        rows = (
            evidence(
                self.rig.engine,
                target=self.state.company,
                kind=None if self.view.kind == "all" else self.view.kind,
                search=self.view.search,
                limit=self.view.limit + 1,
            )
            if self.state.company
            else []
        )
        self.more_available = len(rows) > self.view.limit
        page = rows[: self.view.limit]
        self.items = {item.id: item for item in page}
        self.groups = {}
        table = self.query_one("#evidence-table", RiggerTable)
        with self.prevent(RiggerTable.RowHighlighted):
            table.clear()
            for role, payload in self._fold_prices(page):
                if role == "group":
                    key, run = payload
                    self.groups[key] = run
                    arrow = "▾" if key in self.view.expanded else "▸"
                    table.add_row(
                        Text.assemble(
                            (f"{arrow} prices", token_color(self.app, "kind-price")),
                            (f" · {len(run)}", token_color(self.app, "text-muted")),
                        ),
                        self._kind_cell("bar"),
                        Text(f"{run[0].ts:%Y-%m-%d}", style=token_color(self.app, "text-muted")),
                        key=key,
                    )
                    continue
                item = payload
                title = item.title if role == "item" else f"   {item.title}"
                table.add_row(
                    Text(title, no_wrap=True, overflow="ellipsis"),
                    self._kind_cell(item.kind),
                    Text(item.ts.strftime("%Y-%m-%d"), style=token_color(self.app, "text-muted")),
                    key=item.id,
                )
        self._fit_columns(table)
        self.query_one("#evidence-list-pane", Pane).set_badge(
            f"{len(self.items)}+" if self.more_available else str(len(self.items))
        )
        self.query_one("#evidence-count", Static).update(self._count_line(bool(rows)))
        selected = self.items.get(self.view.selected) or next(iter(self.items.values()), None)
        if selected:
            self.view.selected = selected.id
        # Put the cursor on the row that actually exists: a selection inside a
        # folded run has no row of its own, so the run's group row stands for
        # it. Preview whatever the cursor lands on, or the pane would describe
        # a different row from the one highlighted.
        row_key = selected.id if selected else ""
        if selected and row_key not in self.groups:
            folded = next(
                (key for key, run in self.groups.items() if any(i.id == selected.id for i in run)),
                "",
            )
            row_key = folded or row_key
        if row_key:
            with suppress(Exception):
                with self.prevent(RiggerTable.RowHighlighted):
                    table.move_cursor(row=table.get_row_index(row_key))
        self._preview_row(row_key)

    def _count_line(self, any_rows: bool) -> str:
        """What the list has, or why it is empty — the pane is 46 columns wide."""
        if any_rows:
            return (
                f"{len(self.items)} shown — press l to load more"
                if self.more_available
                else f"{len(self.items)} shown"
            )
        if not self.state.company:
            return "no companies yet — press 1 to add a target"
        if self.view.search or self.view.kind != "all":
            return "nothing matches this filter"
        return "no evidence yet — press U to gather"

    def _preview_row(self, key: str) -> None:
        """Preview a row by its key, whether it is a source or a folded run."""
        if key in self.groups:
            self.preview(None, group=self.groups[key])
        else:
            self.preview(self.items.get(key))

    def _fit_columns(self, table: RiggerTable) -> None:
        """Give the Evidence column whatever the two fixed columns leave.

        DataTable sizes a column to its longest cell, so a wide evidence row
        pushes Type — which carries the kind colour — off the pane behind a
        horizontal scrollbar instead of truncating.
        """
        available = self.query_one("#evidence-list-pane").content_size.width
        if available <= 0:
            return
        source = table.columns.get("source")
        if source is not None:
            # 21 columns for Type and Date, plus the cell padding between them.
            source.width = max(12, available - 21 - 6)
            source.auto_width = False
        table.refresh()

    def on_resize(self) -> None:
        if self.ready:
            self.layout_views()
            with suppress(Exception):
                self._fit_columns(self.query_one("#evidence-table", RiggerTable))

    def action_fold_prices(self) -> None:
        """Unfold or refold the highlighted price run."""
        table = self.query_one("#evidence-table", RiggerTable)
        # An empty list has no cell under the cursor: ``coordinate_to_cell_key``
        # raises rather than returning None, and space is pressable there.
        if not table.is_valid_coordinate(table.cursor_coordinate):
            return
        row = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        key = str(row or "")
        if key not in self.groups:
            return
        self.view.expanded ^= {key}
        self.load_evidence()
        with suppress(Exception):
            table.move_cursor(row=table.get_row_index(key))

    def cited_ids(self) -> set[str]:
        return (
            {
                eid
                for field, _ in _SECTIONS
                for claim in getattr(self.report, field)
                for eid in claim.evidence_ids
            }
            if self.report
            else set()
        )

    def preview(self, item: EvidenceItem | None, group: list[EvidenceItem] | None = None) -> None:
        self.can_open = bool(item and item.url and item.url.startswith(("https://", "http://")))
        self.can_view = bool(item and item.id in self.cited_ids())
        hints = [("o", "open link")] if self.can_open else []
        if self.can_view:
            hints.append(("v", "view in report"))
        hints.append(("esc", "back"))
        self.query_one("#evidence-preview-pane", Pane).set_hints(hint_markup(*hints))
        body = self.query_one("#source-body", Static)
        if group is not None:
            first, last = group[-1], group[0]
            body.update(
                Text.assemble(
                    (f"{len(group)} price bars\n", f"bold {token_color(self.app, 'foreground')}"),
                    (
                        f"{first.ts:%-d %b %Y} – {last.ts:%-d %b %Y} · {last.source}\n\n",
                        token_color(self.app, "text-muted"),
                    ),
                    ("press ", token_color(self.app, "text-muted")),
                    ("space", f"bold {token_color(self.app, 'text-primary')}"),
                    (" to unfold them into the list", token_color(self.app, "text-muted")),
                )
            )
            return
        if item is None:
            body.update("select evidence to preview it")
            return
        body.update(self._preview_text(item))

    def _preview_text(self, item: EvidenceItem) -> Text:
        """The evidence as something to read, never a dict dumped at the reader."""
        muted, fg = token_color(self.app, "text-muted"), token_color(self.app, "foreground")
        text = Text()
        text.append(f"{item.title}\n", style=f"bold {fg}")
        text.append(
            item.kind, style=token_color(self.app, KIND_TOKENS.get(item.kind, "text-muted"))
        )
        text.append(f" · {item.source} · {item.ts:%-d %b %Y %H:%M UTC}\n\n", style=muted)
        if item.body:
            text.append(f"{item.body}\n\n", style=fg)
        else:
            text.append(
                "no text for this kind — the stored fields are the evidence\n\n",
                style=muted,
            )
            text.append(self._raw_table(item.raw))
        if self.report is not None:
            cited = item.id in self.cited_ids()
            text.append("cited in report  ", style=muted)
            if cited:
                text.append("yes", style=token_color(self.app, "text-success"))
                text.append(" · press v to jump to it\n", style=muted)
            else:
                text.append("no\n", style=muted)
        if item.url:
            text.append("url  ", style=muted)
            text.append(f"{item.url[:80]}\n", style=fg)
        provenance = " · ".join(
            f"{key} {self._short(value)}" for key in INTERNAL_RAW if (value := item.raw.get(key))
        )
        if provenance:
            text.append(f"\n{provenance}", style=token_color(self.app, "text-disabled"))
        return text

    @staticmethod
    def _short(value: Any) -> str:
        """Hashes and id lists are provenance, not reading: cut them down."""
        if isinstance(value, list | tuple):
            return f"{len(value)} ids"
        text = str(value)
        return f"{text[:8]}…" if len(text) > 12 else text

    def _raw_table(self, raw: dict[str, Any]) -> Text:
        """``raw`` as aligned rows: labels left, numbers on a shared right edge."""
        pairs = [(key, value) for key, value in raw.items() if key not in INTERNAL_RAW]
        if not pairs:
            return Text()
        label_width = max(len(key) for key, _ in pairs)
        rendered = [
            (
                key,
                f"{value:,.2f}"
                if isinstance(value, float)
                else f"{value:,}"
                if isinstance(value, int)
                else self._short(value),
            )
            for key, value in pairs
        ]
        value_width = max(len(value) for _, value in rendered)
        text = Text()
        for key, value in rendered:
            text.append(f"{key:<{label_width}}  ", style=token_color(self.app, "text-muted"))
            text.append(f"{value:>{value_width}}\n", style=token_color(self.app, "foreground"))
        text.append("\n")
        return text

    def start_quotes(self, company: str) -> None:
        """Stream live quotes for every company in the list.

        Purely additive: the stored last close is what the report reads, so
        a feed that never connects costs the reader nothing. Never awaited
        from ``load_company`` — the panel must not block on the network.
        """
        instruments = [instrument for _target, instrument in self.companies()]
        signature = tuple(instrument.id for instrument in instruments)
        if self.feed_company == signature and self.feed_task and not self.feed_task.done():
            return
        self.stop_quotes()
        self.feed_company = signature
        if not instruments:
            return
        suffixes = DEFAULT_SUFFIXES | getattr(self.rig.cfg, "plugins", {}).get("yfinance", {}).get(
            "suffixes", {}
        )
        self.feed = YahooQuotes(instruments, suffixes, self.on_quote_state)
        feed = self.feed
        try:
            self.feed_task = asyncio.create_task(feed.run())
        except RuntimeError:
            # No running loop (standalone mount in a test): stay on stored bars.
            self.feed = None

    def stop_quotes(self) -> None:
        if self.feed_task:
            self.feed_task.cancel()
        self.feed_task = None
        self.feed = None
        self.feed_company = ()
        self.quote_state = ""

    def on_quote_state(self, state: str) -> None:
        self.quote_state = state

    def live_quote(self, company: str | None = None) -> str:
        """The streamed price, when one has arrived; silent otherwise."""
        company = company or self.state.company
        quote = self.feed.quotes.get(company) if self.feed else None
        if quote is None:
            return ""
        move = f" {quote.change_pct:+.2f}%" if quote.change_pct is not None else ""
        age, _state = age_text(datetime.now(UTC) - quote.received_at)
        return f"live: {quote.price:,.2f} {quote.currency}{move} ({age})"

    def render_status(self) -> None:
        """Live cells on the company rows; job progress in the pane's border."""
        if not self.is_mounted:
            return
        header = self.query_one("#research-header", Pane)
        header.set_hints(
            f"[$text-warning]{self.state.activity}[/]"
            if self.state.activity
            else hint_markup(("↑↓", "select"), ("enter", "open"))
        )
        if not self.feed:
            return
        table = self.query_one("#research-companies", RiggerTable)
        for company, quote in self.feed.quotes.items():
            if company not in table.rows:
                continue
            pct = quote.change_pct
            colour = (
                token_color(self.app, "text-success")
                if pct and pct > 0
                else token_color(self.app, "text-error")
                if pct and pct < 0
                else token_color(self.app, "text-muted")
            )
            cell = Text.assemble(
                f"{quote.price:,.2f} {quote.currency} ",
                ("—" if pct is None else f"{pct:+.2f}%", colour),
            )
            table.update_cell(company, "live", cell, update_width=True)

    def show_audit(self, company: str) -> None:
        """Surface evidence gaps without blocking research or asserting a conclusion."""
        line = self.query_one("#research-audit", Static)
        if not company:
            line.update("")
            return
        try:
            audit = review.evidence_audit(
                self.rig.engine,
                company,
                primary_sources=review.primary_sources_for(self.rig, company),
            )
        except Exception:
            line.update("")
            return
        line.update(" · ".join(audit.warnings))

    def reports_dir(self) -> Path:
        return Path(getattr(self.rig.cfg, "reports_dir", "reports"))

    def currency(self, company: str) -> str:
        return next((i.currency for i in self.rig.universe() if i.id == company), "")

    def last_close(self, company: str) -> str:
        """The newest stored bar as a price and an age, never a bare timestamp.

        ``evidence()`` documents ts-descending order, so ``limit=1`` is the
        newest bar. A date on its own under a "price" label reads as a price;
        the number is the point.
        """
        bars = evidence(self.rig.engine, target=company, kind="bar", limit=1) if company else []
        if not bars:
            return "last close: none"
        close = bars[0].raw.get("close")
        age, _state = age_text(datetime.now(UTC) - bars[0].ts)
        if close is None:
            return f"last close: unavailable ({age})"
        return f"last close: {close:,.2f} {self.currency(company)} ({age})".replace("  ", " ")

    def report_age(self, path: Path | None) -> tuple[str, str]:
        """(label, dot state) for how stale the displayed report is.

        Prefers the sidecar's generation time; a legacy markdown-only report
        falls back to the date in its filename, which is all it records.
        """
        if self.report is not None:
            as_of = self.report.as_of
        elif path is not None:
            try:
                as_of = datetime.strptime(path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                return "age unknown", "warn"
        else:
            return "", "warn"
        label, state = age_text(datetime.now(UTC) - as_of)
        return ("just now" if label == "live" else f"{label} old"), state

    def show_report_meta(self, company: str, path: Path | None) -> None:
        """Surface the two figures a reader acts on: staleness and sentiment."""
        meta = self.query_one("#report-meta")
        meta.display = path is not None
        if path is None:
            self.query_one("#report-history", Static).display = False
            return
        label, state = self.report_age(path)
        self.query_one("#report-age-dot", StatusDot).set_state(state)
        self.query_one("#report-age", Static).update(label)
        pill = self.query_one("#report-sentiment", Pill)
        delta = self.query_one("#report-sentiment-delta", Static)
        if self.report is None:
            pill.update("sentiment —")
            pill.set_variant("dim")
            delta.update("")
            self.query_one("#report-history", Static).display = False
            return
        pill.update(f"sentiment {self.report.sentiment:+.2f}")
        pill.set_variant(sentiment_variant(self.report.sentiment))
        delta.update(self.sentiment_delta(company))
        self.show_history(company)

    def show_history(self, company: str) -> None:
        """List prior runs with their sentiment, so the trend is readable.

        Only shown once a company has more than one run: a single report has no
        trend, and an empty label would just be clutter.
        """
        line = self.query_one("#report-history", Static)
        history = report_history(self.reports_dir(), company)
        if len(history) < 2:
            line.display = False
            return
        line.display = True
        runs = " ← ".join(
            f"{report.as_of:%d %b %H:%M} {report.sentiment:+.2f}" for report in history[:5]
        )
        line.update(f"runs: {runs}")

    def sentiment_delta(self, company: str) -> str:
        """How sentiment moved against the previous run — the change is the signal."""
        history = [
            report
            for report in report_history(self.reports_dir(), company)
            if self.report is None or report.as_of < self.report.as_of
        ]
        if not history:
            return ""
        previous = history[0].sentiment
        assert self.report is not None
        move = self.report.sentiment - previous
        return f"was {previous:+.2f} ({move:+.2f})"

    async def show_latest(self, target_id: str) -> None:
        company = target_id
        if company != self.state.company:
            company = next(
                (i.id for i in self.rig.universe() if target_id in i.watchlists), company
            )
        paths = sorted((self.reports_dir() / company).glob("*.md")) if company else []
        self.report = None
        legacy = ""
        body = "# Report\n\nNo report yet — press n to generate one."
        path = paths[-1] if paths else None
        if path is not None:
            body = path.read_text(encoding="utf-8")
            report = read_report(path.with_suffix(".json"))
            if (
                report is not None
                and report.target_id == company
                and render_markdown(report) == body
            ):
                self.report = report
                body = render_markdown(report, interactive=True)
            else:
                legacy = "press n to regenerate this report and enable interactive citations"
        self.query_one("#report-legacy", Static).update(legacy)
        await self.query_one("#report-view", MarkdownViewer).document.update(body)
        badge = "no report"
        if path is not None:
            counts = section_counts(self.report) if self.report else ""
            badge = " · ".join(part for part in (path.stem, counts) if part)
        self.query_one("#report-doc", Pane).set_badge(badge)
        companies = self.query_one("#research-companies", RiggerTable)
        if company in companies.rows:
            companies.update_cell(company, "report", self.company_report_age(company))
        self.show_report_meta(company, path)
        age_label = self.report_age(path)[0] if path else ""
        self.status_text = " · ".join(
            part
            for part in (
                company or "select a company",
                f"report: {path.stem} ({age_label})" if path else "report: none",
                self.last_close(company),
            )
            if part
        )
        self.render_status()

    async def inspect_evidence(self, evidence_id: str, *, save: bool = True) -> None:
        if save:
            self.save_position()
        self.view.inspected = evidence_id
        found = evidence_by_ids(self.rig.engine, [evidence_id])
        if save:
            self.show_tab("evidence")
        self.detail_open = True
        self.layout_views()
        if found:
            self.items[found[0].id] = found[0]
            self.view.selected = found[0].id
            self.preview(found[0])
        else:
            self.preview(None)
            self.query_one("#source-body", Static).update(
                "this evidence is no longer available\n\n"
                f"{self.report.citations.get(evidence_id, evidence_id) if self.report else evidence_id}"
            )
        self.query_one("#evidence-preview", VerticalScroll).scroll_home(animate=False)

    def show_tab(self, tab: str) -> None:
        self.tab = tab
        self.query_one("#tab-evidence", Button).set_class(tab == "evidence", "-tab-active")
        self.query_one("#tab-report", Button).set_class(tab == "report", "-tab-active")
        self.layout_views()

    def layout_views(self) -> None:
        narrow = self.size.width < 100
        self.query_one("#evidence-layout").display = self.tab == "evidence"
        self.query_one("#report-doc").display = self.tab == "report"
        self.query_one("#evidence-list-pane").display = not (narrow and self.detail_open)
        self.query_one("#evidence-preview-pane").display = not narrow or self.detail_open
        # Narrow: two company rows instead of four, and shorter chip labels.
        self.query_one("#research-header", Pane).set_class(narrow, "-narrow")
        for chip, short, long in (
            ("#research-refresh", "gather", "gather company"),
            ("#research-gather", "gather all", "gather all"),
            ("#report-generate", "generate", "generate report"),
        ):
            self.query_one(chip, ActionChip).set_text(short if narrow else long)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dispatch(event.button.id or "")

    # Keys and buttons run the same branch: the screen has one set of
    # behaviours, reachable either way.
    def action_show_evidence(self) -> None:
        self.dispatch("tab-evidence")

    def action_show_report(self) -> None:
        self.dispatch("tab-report")

    def action_generate_report(self) -> None:
        self.dispatch("report-generate")

    def action_update_evidence(self) -> None:
        self.dispatch("research-refresh")

    def action_gather_all(self) -> None:
        self.dispatch("research-gather")

    def action_load_more(self) -> None:
        self.dispatch("evidence-more")

    def action_focus_targets(self) -> None:
        self.query_one("#research-companies", RiggerTable).focus()

    def action_open_source(self) -> None:
        self.dispatch("evidence-open")

    def action_view_in_report(self) -> None:
        # An uncited source has nowhere to go: the key must not jump to the
        # report tab and land nowhere.
        if self.can_view:
            self.dispatch("evidence-report")

    def action_back(self) -> None:
        # Escape cancels a running generation or gather first: that is the
        # thing you most want to stop. Otherwise it backs out of the
        # narrow-mode detail view, and never silently resets the selection.
        if self._job is not None:
            self._job.cancel()
            return
        if self.detail_open:
            self.dispatch("evidence-back")

    def action_focus_search(self) -> None:
        self.show_tab("evidence")
        self.query_one("#evidence-search", Input).focus()

    def dispatch(self, action: str) -> None:
        if action in ("tab-evidence", "tab-report"):
            self.save_position()
            self.show_tab(action.removeprefix("tab-"))
            self.call_after_refresh(self.restore_position)
        elif action == "evidence-more":
            if self.more_available:
                self.view.limit += 200
                self.load_evidence()
        elif action == "evidence-back":
            self.view.inspected = ""
            self.load_evidence()
            self.detail_open = False
            self.layout_views()
        elif action == "evidence-report":
            self._claim_to_reveal = self.view.selected
            self.show_tab("report")
        elif action == "evidence-open":
            item = self.items.get(self.view.selected)
            if self.can_open and item and item.url:
                self.app.open_url(item.url)
        elif action == "report-generate":
            if not self.state.company:
                self.notify("select a company first", severity="error")
            elif not self.state.busy:
                self.generate(self.state.company)
        elif action == "research-gather" and not self.state.busy:
            self.gather_all()
        elif action == "research-refresh" and not self.state.busy:
            if self.state.company:
                self.gather_all(company=self.state.company)
            else:
                self.notify("select a company first", severity="error")

    def restore_report_position(self) -> None:
        # Show arrives after layout gives the previously hidden document a region.
        if not self.ready or self.tab != "report":
            return
        if self._claim_to_reveal:
            self.scroll_to_claim()
        else:
            self.query_one("#report-view", MarkdownViewer).scroll_to(
                y=self.view.report_y, animate=False, immediate=True
            )

    def scroll_to_claim(self) -> None:
        """Locate the first citing claim by its rendered source line."""
        document = self.query_one("#report-view", MarkdownViewer).document
        lines = document.source.splitlines()
        marker = f"(evidence:{self._claim_to_reveal or self.view.selected})"
        citation_line = next((i for i, line in enumerate(lines) if marker in line), None)
        if citation_line is None:
            return
        claim_line = next(
            (i for i in range(citation_line - 1, -1, -1) if lines[i].startswith("- ")),
            citation_line,
        )
        blocks = [
            block
            for block in document.query(MarkdownBlock)
            if block.source_range[0] <= claim_line < block.source_range[1]
        ]
        if blocks:
            min(blocks, key=lambda b: b.source_range[1] - b.source_range[0]).scroll_visible(
                top=True, animate=False, immediate=True
            )
            self._claim_to_reveal = ""

    def promote_claim(self, ref: str) -> None:
        """Turn a report claim into a thesis, carrying its evidence across.

        The report says what the evidence supports; a thesis is what you decide
        to hold. This is the step between the two, so the claim's sources stay
        attached rather than being re-found by hand.
        """
        from rigger.tui.screens.theses import ThesisForm

        field, _, index = ref.partition(":")
        claims = getattr(self.report, field, []) if self.report else []
        if not index.isdigit() or int(index) >= len(claims):
            self.notify("that claim is no longer in the report", severity="error")
            return
        claim = claims[int(index)]

        def created(fields: dict[str, Any] | None) -> None:
            if fields is None:
                return
            text = fields.pop("claim")
            fields["targets"] = fields["targets"] or (self.state.company,)
            try:
                thesis = theses.create_thesis(self.rig.engine, text, **fields)
            except ValueError as exc:
                self.notify(str(exc), severity="error")
                return
            for evidence_id in claim.evidence_ids:
                # Accepted, not queued as a candidate: the citation contract
                # already proved these support the claim, and the reader just
                # read them in context. Re-reviewing them would be busywork.
                theses.add_evidence(
                    self.rig.engine,
                    thesis.id,
                    evidence_id,
                    "support",
                    f"from report {self.state.company}",
                    accepted=True,
                )
            self.notify(
                f"thesis created with {len(claim.evidence_ids)} linked evidence items", timeout=6
            )

        self.app.push_screen(ThesisForm(claim=claim.text, targets=self.state.company), created)

    def research_screens(self) -> list[Research]:
        """Every mounted Research panel, so Reports and Research stay in sync.

        Reads the app's public registry; falls back to this screen alone when
        mounted standalone (tests, a single-panel host).
        """
        screens = [
            screen
            for screen in getattr(self.app, "screens_by_name", {}).values()
            if isinstance(screen, Research) and screen.ready
        ]
        return screens or [self]

    def set_busy(self, busy: bool, label: str = "") -> None:
        self.state.busy = busy
        self.state.activity = label if busy else ""
        for screen in self.research_screens():
            for button in ("#report-generate", "#research-gather", "#research-refresh"):
                screen.query_one(button, Button).disabled = busy
            screen.state.activity = self.state.activity
            screen.render_status()

    def spend(self) -> float:
        """Total LLM spend so far; the delta across a run is what it cost."""
        try:
            return sum(row.cost_usd for row in services.llm_costs(self.rig.engine))
        except Exception:
            return 0.0

    def report_failure(self, prefix: str, exc: BaseException) -> None:
        """Surface a failure without dumping a provider's raw payload in a toast.

        ``build_report`` raises ValueError with a message written to be read;
        anything else (auth, timeout, a provider error body) is summarised and
        the detail goes to the log.
        """
        if isinstance(exc, ValueError) and exc.args:
            self.notify(str(exc.args[0]), severity="error")
        else:
            self.notify(
                f"{prefix}: {type(exc).__name__} — see the log for detail", severity="error"
            )
        self.log.error(f"{prefix}: {exc!r}")

    @work
    async def generate(self, company: str) -> None:
        self._job = get_current_worker()
        model = model_for(self.rig.cfg, "report")
        self.set_busy(True, f"generating report for {company} via {model} — esc to cancel…")
        before = self.spend()
        try:
            report = await build_report(self.rig, company)
            write_report(report, self.reports_dir())
            cost = self.spend() - before
            self.notify(f"report written for {company} (${cost:.2f})")
            for screen in self.research_screens():
                if screen._rendered_company == company and self.state.company == company:
                    await screen.show_latest(company)
                    screen.preview(screen.items.get(screen.view.selected))
        except CancelledError:
            self.notify(f"report for {company} cancelled")
            raise
        except Exception as exc:
            self.report_failure("report failed", exc)
        finally:
            self._job = None
            self.set_busy(False)

    @work
    async def gather_all(self, company: str = "") -> None:
        """Gather evidence for one company, or for every target.

        The scoped form exists because wanting fresher data on the company you
        are reading should not cost a full-universe ingest and extraction.
        """
        self._job = get_current_worker()
        instruments = [i for i in self.rig.universe() if i.id == company] if company else []
        scope = company or "all targets"
        self.set_busy(True, f"gathering evidence for {scope} — esc to cancel…")
        try:
            await services.ingest(self.rig, tickers=instruments[0].symbol if instruments else None)
            self.set_busy(True, f"extracting events for {scope} — esc to cancel…")
            await services.extract(self.rig, instruments=instruments or None)
            for screen in self.research_screens():
                if screen._rendered_company == self.state.company:
                    if screen.is_current:
                        screen.save_position()
                    await screen.show_latest(self.state.company)
                    screen.load_evidence()
                    screen.call_after_refresh(screen.restore_position)
            self.notify(f"evidence gathered for {scope}")
        except CancelledError:
            self.notify("gather cancelled")
            raise
        except Exception as exc:
            self.report_failure("gather failed", exc)
        finally:
            self._job = None
            self.set_busy(False)
