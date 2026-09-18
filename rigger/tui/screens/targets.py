"""Keyboard-first watchlist ledger with ephemeral streaming quotes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from rigger import services
from rigger.asset_metrics import AssetMetrics, chart_window, fetch_asset_metrics
from rigger.core.models import Instrument
from rigger.plugins.data.yfinance import DEFAULT_SUFFIXES
from rigger.quotes import SearchResult, YahooQuotes, canonical_symbol, yahoo_search
from rigger.tui.shell import RiggerScreen, age_text
from rigger.tui.widgets import (
    MODAL_WIDTH,
    BrailleGraph,
    Dialog,
    Pane,
    PaneRow,
    hint_markup,
    token_color,
)


class WatchlistList(OptionList):
    """Keyboard list with the old table row-count compatibility surface."""

    @property
    def row_count(self) -> int:
        return sum(
            1 for option in self.options if option.id and str(option.id).startswith("target:")
        )


ASSET_CLASS_ORDER = ("equity", "etf", "bond", "commodity", "fx", "crypto", "cash", "other")


def _friendly_date_range(start: str | None, end: str | None) -> str:
    """Format provider timestamps as a compact date range for the inspector."""
    if not start or not end:
        return "no historical dates"
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return f"{start} → {end}"
    if first.date() == last.date():
        return first.strftime("%-d %b %Y")
    if first.year == last.year:
        return f"{first.day} {first.strftime('%b')} – {last.day} {last.strftime('%b')} {last.year}"
    return f"{first.day} {first.strftime('%b %Y')} – {last.day} {last.strftime('%b %Y')}"


class TargetAddModal(Dialog):
    """Centred terminal form for adding one target to the watchlist."""

    dialog_title = "add to watchlist"
    dialog_hint = hint_markup(("enter", "save"), ("esc", "cancel"))
    dialog_width = MODAL_WIDTH

    DEFAULT_CSS = """
    TargetAddModal #tg-form { height: auto; }
    TargetAddModal .tg-field { height: 3; }
    TargetAddModal .tg-field Label { width: 10; padding: 1 0; color: $text-muted; }
    TargetAddModal .tg-field Input { width: 1fr; margin: 0; }
    TargetAddModal #tg-network { height: 1; color: $text-muted; content-align-horizontal: center; }
    TargetAddModal #tg-network.-online { color: $text-success; }
    TargetAddModal #tg-network.-offline { color: $text-warning; }
    TargetAddModal #tg-suggestions { display: none; height: auto; max-height: 6; margin: 0 0 1 10; background: $panel; }
    TargetAddModal #tg-modal-actions { height: 1; margin-top: 1; }
    TargetAddModal #tg-modal-actions Button { height: 1; min-width: 0; border: none; padding: 0 1; margin: 0 1 0 0; }
    """

    def __init__(self, rig: Any) -> None:
        super().__init__()
        self.rig = rig
        self._search_task: asyncio.Task | None = None
        self._search_generation = 0
        self._results_by_symbol: dict[str, SearchResult] = {}
        self._suppress_name_search = False
        self._suffixes = DEFAULT_SUFFIXES | getattr(self.rig.cfg, "plugins", {}).get(
            "yfinance", {}
        ).get("suffixes", {})
        self._suffixes.update(
            {
                name: profile.yahoo_suffix
                for name, profile in getattr(self.rig.cfg, "markets", {}).items()
            }
        )

    def _currency(self, market: str) -> str:
        profile = getattr(self.rig.cfg, "markets", {}).get(market.lower())
        return profile.currency if profile else ""

    def compose_dialog(self) -> ComposeResult:
        fields = []
        for field, label, placeholder in (
            ("name", "Name", "company or ticker"),
            ("kind", "Kind", "company"),
            ("asset-class", "Asset", "equity / crypto / etf"),
            ("market", "Market", "us / asx"),
            ("tickers", "Tickers", "BHP,RIO"),
            ("tags", "Tags", "resources,income"),
        ):
            fields.append(
                Horizontal(
                    Label(label),
                    Input(placeholder=placeholder, id=f"tg-{field}"),
                    classes="tg-field",
                )
            )
            if field == "name":
                fields.append(OptionList(id="tg-suggestions"))
        yield Static("◌ Yahoo lookup ready", id="tg-network", markup=False)
        yield Vertical(*fields, id="tg-form")
        yield Horizontal(
            Button("Save", id="tg-add", variant="primary"),
            Button("Cancel", id="tg-close"),
            id="tg-modal-actions",
        )

    def _value(self, field: str) -> str:
        return self.query_one(f"#tg-{field}", Input).value.strip()

    def on_mount(self) -> None:
        self.query_one("#tg-name", Input).focus()

    def _set_network(self, text: str, state: str) -> None:
        indicator = self.query_one("#tg-network", Static)
        indicator.remove_class("-online", "-offline", "-pending")
        indicator.add_class(f"-{state}")
        indicator.update(text)

    def _local_results(self, query: str) -> list[SearchResult]:
        needle = query.casefold()
        results: list[SearchResult] = []
        seen: set[tuple[str, str]] = set()
        instruments = list(self.rig.universe())
        with suppress(Exception):
            for target in services.target_specs().values():
                for market in target.markets:
                    for symbol in target.tickers:
                        instruments.append(
                            Instrument(
                                id=f"{market.upper()}:{symbol}",
                                market=market,
                                symbol=symbol,
                                currency=self._currency(market),
                                asset_class=getattr(target, "asset_class", "equity"),
                            )
                        )
        for inst in instruments:
            haystack = " ".join((inst.symbol, inst.name or "", inst.market)).casefold()
            key = (inst.market.casefold(), inst.symbol.casefold())
            if needle in haystack and key not in seen:
                seen.add(key)
                results.append(
                    SearchResult(
                        inst.symbol,
                        inst.name or inst.symbol,
                        inst.market,
                        inst.currency,
                        asset_class=getattr(inst, "asset_class", "equity"),
                    )
                )
        return results[:8]

    def _show_results(self, results: list[SearchResult]) -> None:
        options = self.query_one("#tg-suggestions", OptionList)
        options.clear_options()
        self._results_by_symbol = {result.symbol: result for result in results}
        for result in results:
            exchange = f" · {result.exchange}" if result.exchange else ""
            options.add_option(
                Option(
                    f"{result.symbol} — {result.name} · {result.market.upper()}{exchange}",
                    id=result.symbol,
                )
            )
        options.display = bool(results)

    async def _search(self, query: str, generation: int) -> None:
        self._show_results(self._local_results(query))
        try:
            results = await yahoo_search(query)
        except Exception:
            if generation == self._search_generation:
                self._set_network("○ offline · local search", "offline")
            return
        if generation != self._search_generation:
            return
        self._set_network("● online · Yahoo", "online")
        supported = [result for result in results if result.market in self._suffixes]
        self._show_results(supported or self._local_results(query))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "tg-name":
            return
        if self._suppress_name_search:
            self._suppress_name_search = False
            return
        self._search_generation += 1
        if self._search_task:
            self._search_task.cancel()
        query = event.value.strip()
        if not query:
            self.query_one("#tg-suggestions", OptionList).display = False
            self._set_network("◌ Yahoo lookup ready", "pending")
            return
        if len(query) < 2:
            self.query_one("#tg-suggestions", OptionList).display = False
            self._set_network("◌ type 2+ characters", "pending")
            return
        # Show local matches immediately while the debounced Yahoo lookup runs.
        self._show_results(self._local_results(query))
        self._set_network("◌ searching Yahoo…", "pending")
        self._search_task = asyncio.create_task(self._search(query, self._search_generation))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        symbol = event.option.id
        if not symbol:
            return
        selected = self._results_by_symbol.get(str(symbol))
        if selected is None:
            # A remote-only result is represented by its symbol; the market
            # defaults to US until the user changes it.
            self.query_one("#tg-tickers", Input).value = str(symbol)
            self.query_one("#tg-market", Input).value = "us"
            self.query_one("#tg-asset-class", Input).value = "equity"
        else:
            self._suppress_name_search = True
            self.query_one("#tg-name", Input).value = selected.name
            self.query_one("#tg-tickers", Input).value = selected.symbol
            self.query_one("#tg-market", Input).value = selected.market
            self.query_one("#tg-asset-class", Input).value = selected.asset_class
        self.query_one("#tg-suggestions", OptionList).display = False
        self.query_one("#tg-kind", Input).focus()

    def on_key(self, event: Any) -> None:
        control = getattr(event, "control", None) or self.focused
        if (
            event.key in {"down", "up"}
            and getattr(control, "id", None) == "tg-name"
            and self.query_one("#tg-suggestions", OptionList).display
        ):
            event.stop()
            options = self.query_one("#tg-suggestions", OptionList)
            options.highlighted = 0 if event.key == "down" else max(0, len(options.options) - 1)
            options.focus()

    async def on_unmount(self) -> None:
        if self._search_task:
            self._search_task.cancel()

    def _save(self) -> None:
        name = self._value("name")
        market = self._value("market")
        asset_class = self._value("asset-class").casefold() or "equity"
        if not name or not market:
            self.notify("name and market are required", severity="error")
            return
        try:
            tickers = [
                canonical_symbol(s.strip(), market, self._suffixes)
                for s in self._value("tickers").split(",")
                if s.strip()
            ]
            services.add_target(
                name,
                kind=self._value("kind") or "company",
                market=market,
                tickers=tickers,
                tags=[s.strip() for s in self._value("tags").split(",") if s.strip()],
                asset_class=asset_class,
            )
        except (ValueError, KeyError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.dismiss(name)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._save()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tg-add":
            self._save()
        elif event.button.id == "tg-close":
            self.dismiss(None)

    def on_click(self, event: Any) -> None:
        control = getattr(event, "control", None)
        if getattr(control, "id", None) == "tg-add":
            self._save()
        elif getattr(control, "id", None) == "tg-close":
            self.dismiss(None)

    def action_dismiss_dialog(self) -> None:
        suggestions = self.query_one("#tg-suggestions", OptionList)
        focused = self.focused
        if suggestions.display or getattr(focused, "id", None) == "tg-name":
            suggestions.display = False
            self.query_one("#tg-name", Input).focus()
            return
        self.dismiss(None)


class Targets(RiggerScreen):
    name = "targets"
    BINDINGS = [
        ("enter", "inspect", "refresh metrics"),
        ("r", "cycle_range", "range"),
        ("a", "add", "add"),
        ("d", "remove", "remove"),
        ("slash", "filter", "filter"),
        ("space", "toggle_group", "fold"),
        ("left", "member(-1)", "member"),
        ("right", "member(1)", "member"),
        ("escape", "cancel", "back"),
    ]
    #: Below this terminal width the list takes the whole screen and the
    #: metrics pane opens on enter — the two do not fit side by side.
    NARROW_WIDTH = 100
    #: Column widths of a watchlist row: name, last, change, age.
    COLUMNS = (12, 10, 8, 4)
    CSS = """
    #target-list-pane { width: 46; min-width: 46; }
    #target-inspector-pane { width: 1fr; min-width: 0; }
    Targets.-narrow #target-list-pane, Targets.-narrow #target-inspector-pane { width: 1fr; }
    #target-table { height: 1fr; margin: 0; border: none; background: transparent; }
    #target-table > .option-list--option { padding: 0 1; }
    #target-table > .option-list--option-disabled { color: $text-muted; text-style: none; }
    #target-table .tg-group { color: $text-primary; text-style: bold; }
    #tg-filter { margin: 0 0 0 0; }
    #tg-empty { height: auto; padding: 0 1; color: $text-muted; }
    #target-inspector-content { width: 1fr; height: 1fr; padding: 0 1; overflow-y: auto; }
    #target-inspector-title { width: 1fr; height: 1; }
    #target-inspector-empty { width: 1fr; height: auto; color: $text-muted; }
    #target-inspector-hero { width: 1fr; height: 1; margin: 1 0 0 0; }
    #target-inspector-status { width: 1fr; height: auto; color: $text-muted; }
    #target-chart-header { width: 1fr; height: 1; margin: 1 0 0 0; }
    #target-chart-label { width: 1fr; color: $text-muted; text-style: bold; }
    #target-chart-change { width: auto; text-style: bold; }
    #target-chart-change.-up { color: $text-success; }
    #target-chart-change.-down { color: $text-error; }
    #target-chart { width: 1fr; height: 6; padding: 0 1; background: $panel; }
    #target-chart-axis { width: 1fr; height: 1; color: $text-muted; margin: 0 0 1 0; }
    #target-metric-grid { width: 1fr; height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 0 1; }
    Targets.-narrow #target-metric-grid { grid-size: 1; grid-columns: 1fr; }
    .metric-card { height: auto; padding: 0 1; }
    .metric-card-body { width: 1fr; height: auto; color: $foreground; }
    #target-inspector-source { width: 1fr; height: auto; margin: 1 0 0 0; color: $text-muted; }
    """

    def __init__(self, rig: Any) -> None:
        super().__init__(rig)
        self.rows: dict[str, tuple[str, str | None]] = {}
        self.feed: YahooQuotes | None = None
        self.feed_task: asyncio.Task | None = None
        self.active = False
        self.signature: tuple = ()
        self.specs = {}
        self._metrics: dict[str, AssetMetrics] = {}
        self._selected_instrument: Instrument | None = None
        self._collapsed_groups: set[str] = set()
        self._option_indices: dict[str, int] = {}
        self._collect_task: asyncio.Task | None = None
        self._range = "month"
        self._members_by_target: dict[str, list[str]] = {}
        self._selected_member: dict[str, str] = {}
        self.detail_open = False

    def compose_content(self) -> ComposeResult:
        with PaneRow(id="target-split"):
            with Pane(
                title="watchlist",
                hints=hint_markup(
                    ("a", "add"), ("d", "remove"), ("/", "filter"), ("space", "fold")
                ),
                id="target-list-pane",
            ):
                yield Input(
                    placeholder="/ filter names, tickers, markets, kinds or tags", id="tg-filter"
                )
                yield WatchlistList(id="target-table")
                yield Static("no targets yet — press a to add one", id="tg-empty")
            with Pane(title="metrics", hints=self._metric_hints(), id="target-inspector-pane"):
                with Vertical(id="target-inspector-content"):
                    yield Static("", id="target-inspector-title", markup=False)
                    yield Static(
                        "no target selected — ↑↓ picks one",
                        id="target-inspector-empty",
                        markup=False,
                    )
                    yield Static("", id="target-inspector-hero", markup=False)
                    yield Static("", id="target-inspector-status", markup=False)
                    with Horizontal(id="target-chart-header"):
                        yield Static("price · month", id="target-chart-label", markup=False)
                        yield Static("", id="target-chart-change", markup=False)
                    yield BrailleGraph([], id="target-chart")
                    yield Static("", id="target-chart-axis", markup=False)
                    with Vertical(id="target-metric-grid"):
                        for index in range(4):
                            with Pane(classes="metric-card -auto", id=f"metric-card-{index}"):
                                yield Static(
                                    "",
                                    classes="metric-card-body",
                                    id=f"metric-card-body-{index}",
                                    markup=False,
                                )
                    yield Static("", id="target-inspector-source", markup=False)

    def _colours(self) -> dict[str, str]:
        """Theme tokens as colours Rich can parse — never a raw theme value."""
        return {
            token: token_color(self.app, token)
            for token in (
                "foreground",
                "text-muted",
                "text-primary",
                "text-success",
                "text-error",
                "text-warning",
            )
        }

    def _metric_hints(self) -> str:
        pairs = [("enter", "refresh"), ("r", f"range: {self._range_label()}")]
        target_key = self._selected() if self.is_mounted else None
        if target_key and len(self._members_by_target.get(self.rows[target_key][0], [])) > 1:
            pairs.append(("←→", "member"))
        if self.has_class("-narrow"):
            pairs.append(("esc", "back"))
        return hint_markup(*pairs)

    def layout_views(self) -> None:
        """Wide: both panes. Narrow: the list, or the metrics after enter."""
        narrow = self.size.width < self.NARROW_WIDTH
        self.set_class(narrow, "-narrow")
        self.query_one("#target-list-pane").display = not (narrow and self.detail_open)
        self.query_one("#target-inspector-pane").display = not narrow or self.detail_open
        self.query_one("#target-inspector-pane", Pane).set_hints(self._metric_hints())

    def on_resize(self) -> None:
        if self.is_mounted:
            self.layout_views()

    def on_mount(self) -> None:
        self.query_one("#tg-filter").display = False
        self.layout_views()
        self.refresh_view()
        self.query_one("#target-table").focus()
        self.set_interval(0.5, self._paint_quotes)

    def _selected(self) -> str | None:
        key = self._highlighted_key()
        return key if key in self.rows else None

    def refresh_view(self) -> None:
        table = self.query_one("#target-table", WatchlistList)
        selected = self._selected()
        table.clear_options()
        self.rows.clear()
        self._option_indices.clear()
        name_w, last_w, chg_w, age_w = self.COLUMNS
        table.add_option(
            Option(
                f"  {'Name':<{name_w}}{'Last':>{last_w}}  {'Chg%':>{chg_w}}  {'Age':>{age_w}}",
                disabled=True,
            )
        )
        self.specs = services.target_specs()
        specs = sorted(self.specs.values(), key=lambda t: t.id)
        known = {inst.id: inst for inst in self.rig.universe()}
        self.query_one("#target-list-pane", Pane).set_badge(str(len(specs)))
        query = self.query_one("#tg-filter", Input).value.casefold().strip()
        instruments: dict[str, Instrument] = {}
        grouped: dict[str, list[Any]] = {}
        self._members_by_target.clear()
        for target in specs:
            members = []
            for market in target.markets:
                for symbol in target.tickers:
                    ident = f"{market.upper()}:{symbol}"
                    instruments[ident] = Instrument(
                        id=ident,
                        market=market,
                        symbol=symbol,
                        currency=getattr(self.rig.cfg, "markets", {}).get(market).currency
                        if market in getattr(self.rig.cfg, "markets", {})
                        else "",
                        asset_class=getattr(target, "asset_class", "equity"),
                    )
                    if ident in known:
                        instruments[ident] = known[ident]
                    members.append(ident)
            if (
                query
                and query
                not in " ".join(
                    [
                        target.id,
                        target.name,
                        target.kind,
                        *target.markets,
                        *target.tickers,
                        *target.tags,
                    ]
                ).casefold()
            ):
                continue
            asset_class = str(getattr(target, "asset_class", "equity")).casefold()
            if asset_class not in ASSET_CLASS_ORDER:
                asset_class = "other"
            grouped.setdefault(asset_class, []).append((target, members))
            self._members_by_target[target.id] = members
        order = ASSET_CLASS_ORDER
        for asset_class in order + tuple(sorted(set(grouped) - set(order))):
            entries = grouped.get(asset_class)
            if not entries:
                continue
            group_key = f"group:{asset_class}"
            expanded = group_key not in self._collapsed_groups
            table.add_option(
                Option(self._group_prompt(asset_class, entries, expanded), id=group_key)
            )
            if not expanded:
                continue
            for target, members in entries:
                key = f"target:{target.id}"
                inst = members[0] if len(members) == 1 else None
                self.rows[key] = (target.id, inst)
                table.add_option(Option(self._row_prompt(target, members), id=key))
        empty = self.query_one("#tg-empty", Static)
        empty.display = not table.row_count
        empty.update(
            "no targets match the filter — press esc to clear it"
            if specs
            else "no targets yet — press a to add one"
        )
        if selected:
            with suppress(Exception):
                table.highlighted = table.get_option_index(selected)
        elif self.rows:
            table.highlighted = table.get_option_index(next(iter(self.rows)))
        self._instruments = list(instruments.values())
        if self.active:
            self._sync_feed()
        self._paint_quotes()
        self._select_instrument()

    def _select_instrument(self, force: bool = False) -> None:
        key = self._selected()
        if key is None or key not in self.rows:
            self._selected_instrument = None
            self._render_metrics()
            return
        target_id, ident = self.rows[key]
        target = self.specs.get(target_id)
        members = self._members_by_target.get(target_id, []) if target else []
        if members:
            ident = self._selected_member.get(target_id, ident or members[0])
            if ident not in members:
                ident = members[0]
            self._selected_member[target_id] = ident
        instrument = (
            next((item for item in self.rig.universe() if item.id == ident), None)
            if ident
            else None
        )
        if instrument is None and ident:
            market, symbol = ident.split(":", 1)
            instrument = Instrument(
                id=ident,
                market=market.lower(),
                symbol=symbol,
                currency="",
                asset_class=getattr(target, "asset_class", "equity"),
            )
        self._selected_instrument = instrument
        if instrument:
            if force or instrument.id not in self._metrics:
                self.fetch_metrics(instrument)
            else:
                self._render_metrics()

    @work(exclusive=True, thread=False)
    async def fetch_metrics(self, instrument: Instrument) -> None:
        # Paint the loading state first so the pane never shows a stale card.
        self._render_metrics()
        metric = await asyncio.to_thread(
            fetch_asset_metrics, instrument, self._range, self.rig.engine
        )
        self._metrics[instrument.id] = metric
        if self._selected_instrument and self._selected_instrument.id == instrument.id:
            self._render_metrics()

    def _render_metrics(self) -> None:
        instrument = self._selected_instrument
        metric = self._metrics.get(instrument.id) if instrument else None
        empty = self.query_one("#target-inspector-empty", Static)
        title = self.query_one("#target-inspector-title", Static)
        status = self.query_one("#target-inspector-status", Static)
        hero = self.query_one("#target-inspector-hero", Static)
        chart_change = self.query_one("#target-chart-change", Static)
        axis = self.query_one("#target-chart-axis", Static)
        source = self.query_one("#target-inspector-source", Static)
        cards = [
            (
                self.query_one(f"#metric-card-{i}", Pane),
                self.query_one(f"#metric-card-body-{i}", Static),
            )
            for i in range(4)
        ]
        self.query_one("#target-inspector-pane", Pane).set_hints(self._metric_hints())

        def clear_cards() -> None:
            for card, body in cards:
                card.display = False
                body.update("")

        def clear_chart() -> None:
            chart_change.update("")
            chart_change.set_classes("")
            self.query_one("#target-chart", BrailleGraph).data = []
            axis.update("")
            source.update("")

        selected = self._selected()
        target = self.specs.get(self.rows[selected][0]) if selected in self.rows else None
        members = self._members_by_target.get(target.id, []) if target else []
        if not instrument:
            empty.display = True
            if target and not target.tickers:
                message = "no tickers on this target — metrics need at least one ticker"
            elif not self.rows:
                message = "no targets yet — press a to add one"
            else:
                message = "no target selected — ↑↓ picks one"
            empty.update(message)
            title.update(f"{target.id} · {target.kind}" if target else "")
            hero.update("")
            status.update("")
            clear_chart()
            clear_cards()
            return
        empty.display = False
        name = getattr(instrument, "name", "") or instrument.symbol
        member_note = (
            f" · member {members.index(instrument.id) + 1}/{len(members)}"
            if instrument.id in members and len(members) > 1
            else ""
        )
        tags = f" · tags: {', '.join(sorted(target.tags))}" if target and target.tags else ""
        tokens = self._colours()
        title.update(
            Text.assemble(
                (name, f"bold {tokens['foreground']}"),
                (
                    f"  {instrument.id} · {instrument.market.upper()} · {instrument.asset_class}{tags}{member_note}",
                    tokens["text-muted"],
                ),
            )
        )
        if metric is None:
            hero.update("")
            status.update("loading metrics…")
            clear_chart()
            clear_cards()
            return
        current_label = "Current yield" if metric.profile == "bond" else "Current price"
        current = metric.values.get(current_label, "—")
        quote = self._quote_for(instrument.id)
        up, down, flat = tokens["text-success"], tokens["text-error"], tokens["text-muted"]
        hero_text = Text()
        hero_text.append(current, style=f"bold {tokens['foreground']}")
        hero_text.append(f" {instrument.currency}" if instrument.currency else "", style=flat)
        hero_text.append("   ")
        if quote is not None and quote.change_pct is not None:
            pct = quote.change_pct
            arrow = "▲" if pct > 0 else "▼" if pct < 0 else "─"
            hero_text.append(
                f"{arrow} {pct:+.2f}%", style=up if pct > 0 else down if pct < 0 else flat
            )
            hero_text.append("  today", style=flat)
        else:
            hero_text.append("— today", style=flat)
        hero.update(hero_text)
        if metric.error:
            status.update(f"metrics unavailable: {metric.error} — press enter to retry")
        else:
            range_context = (
                f"high {metric.period_high:,.2f} · low {metric.period_low:,.2f}"
                if metric.period_high is not None and metric.period_low is not None
                else "range unavailable"
            )
            volatility = (
                f"vol {metric.volatility * 100:.1f}%"
                if metric.volatility is not None
                else "vol unavailable"
            )
            status.update(f"{range_context} · {volatility}")
        chart_change.update(metric.change_label or "—")
        chart_change.set_class(metric.change_label.startswith("+"), "-up")
        chart_change.set_class(metric.change_label.startswith("-"), "-down")
        self.query_one("#target-chart", BrailleGraph).data = chart_window(
            metric.series, None if self._range == "all" else 30
        )
        history = _friendly_date_range(metric.history_start, metric.history_end)
        axis.update(
            history.replace(" – ", " " * 20 + "→" + " " * 20) if " – " in history else history
        )
        groups = metric.groups or ({"Available Metrics": metric.values} if metric.values else {})
        clear_cards()
        for index, (card, body) in enumerate(cards):
            if index >= len(groups):
                continue
            group, values = list(groups.items())[index]
            card.display = True
            card.set_title(group.casefold())
            grid = Table.grid(expand=True, padding=(0, 1))
            grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
            grid.add_column(justify="right", no_wrap=True)
            for label, value in values.items():
                grid.add_row(label, Text(value, style=tokens["foreground"]))
            body.update(grid)
        quote_stamp = quote.timestamp.strftime("%H:%M:%S UTC") if quote else "—"
        source.update(f"{metric.source} · live {quote_stamp} · history {history}")

    def _sync_feed(self) -> None:
        suffixes = DEFAULT_SUFFIXES | getattr(self.rig.cfg, "plugins", {}).get("yfinance", {}).get(
            "suffixes", {}
        )
        signature = (
            tuple(sorted(i.id for i in self._instruments)),
            tuple(sorted(suffixes.items())),
        )
        if self.feed_task and not self.feed_task.done() and signature == self.signature:
            return
        old_task = self.feed_task
        if old_task:
            old_task.cancel()
        old_quotes = self.feed.quotes if self.feed else {}
        self.signature = signature
        self.feed = YahooQuotes(self._instruments, suffixes, self._quote_state)
        self.feed.quotes.update({k: v for k, v in old_quotes.items() if k in signature[0]})
        feed = self.feed

        async def start() -> None:
            if old_task:
                with suppress(asyncio.CancelledError):
                    await old_task
            await feed.run()

        self.feed_task = asyncio.create_task(start())

    def _quote_state(self, state: str) -> None:
        self._feed_state = state

    def _quote_for(self, ident: str | None) -> Any:
        return self.feed.quotes.get(ident) if self.feed and ident else None

    def _row_prompt(self, target: Any, members: list[str]) -> Text:
        """One watchlist row: name, last, change, quote age — columns, not prose.

        Only the change cell carries colour, so the cursor row stays readable
        and the eye scans one column for what moved.
        """
        name_w, last_w, chg_w, age_w = self.COLUMNS
        tokens = self._colours()
        ident = members[0] if len(members) == 1 else self._selected_member.get(target.id)
        if len(members) == 1:
            name = members[0].split(":", 1)[1]
        elif members:
            name = f"{target.id} ▸{len(members)}"
        else:
            name = target.id
        quote = self._quote_for(ident)
        row = Text(f"  {name[:name_w]:<{name_w}}")
        if quote is None:
            row.append(
                f"{'—':>{last_w}}  {'—':>{chg_w}}  {'':>{age_w}}", style=tokens["text-muted"]
            )
            return row
        pct = quote.change_pct
        row.append(f"{quote.price:,.2f}"[:last_w].rjust(last_w), style=tokens["foreground"])
        row.append("  ")
        row.append(
            f"{'—' if pct is None else f'{pct:+.2f}%':>{chg_w}}",
            style=tokens["text-success"]
            if pct and pct > 0
            else tokens["text-error"]
            if pct and pct < 0
            else tokens["text-muted"],
        )
        age, state = age_text(datetime.now(UTC) - quote.received_at)
        row.append("  ")
        row.append(
            f"{age:>{age_w}}",
            style=tokens["text-muted"] if state == "ok" else tokens["text-warning"],
        )
        return row

    def _group_prompt(self, asset_class: str, entries: list[Any], expanded: bool) -> Text:
        """Group header with the mean move of its members that have a quote."""
        name_w, last_w, chg_w, _age_w = self.COLUMNS
        tokens = self._colours()
        arrow = "▾" if expanded else "▸"
        row = Text(f"{arrow} {asset_class} ", style=f"bold {tokens['text-primary']}")
        row.append(f"({len(entries)})", style=tokens["text-muted"])
        moves = [
            quote.change_pct
            for _target, members in entries
            for quote in (self._quote_for(members[0] if len(members) == 1 else None),)
            if quote is not None and quote.change_pct is not None
        ]
        if moves:
            mean = sum(moves) / len(moves)
            pad = 2 + name_w + last_w + 2 - len(row.plain)
            row.append(" " * max(1, pad))
            row.append(
                f"{mean:+.2f}%".rjust(chg_w),
                style=tokens["text-success"]
                if mean > 0
                else tokens["text-error"]
                if mean < 0
                else tokens["text-muted"],
            )
        return row

    def _paint_quotes(self) -> None:
        if not self.is_mounted:
            return
        try:
            table = self.query_one("#target-table", WatchlistList)
        except Exception:
            return
        grouped: dict[str, list[Any]] = {}
        for key, (target_id, _ident) in self.rows.items():
            target = self.specs.get(target_id)
            if target is None:
                continue
            members = self._members_by_target.get(target_id, [])
            asset_class = str(getattr(target, "asset_class", "equity")).casefold()
            grouped.setdefault(
                asset_class if asset_class in ASSET_CLASS_ORDER else "other", []
            ).append((target, members))
            with suppress(Exception):
                table.replace_option_prompt(key, self._row_prompt(target, members))
        for asset_class, entries in grouped.items():
            group_key = f"group:{asset_class}"
            with suppress(Exception):
                table.replace_option_prompt(
                    group_key,
                    self._group_prompt(
                        asset_class, entries, group_key not in self._collapsed_groups
                    ),
                )
        live = self.feed is not None and any(
            self._quote_for(ident) for _t, ident in self.rows.values() if ident
        )
        self.query_one("#target-list-pane", Pane).set_badge(
            f"{'● live' if live else '○ idle'} · {len(self.rows)}"
        )

    def on_option_list_option_highlighted(self, event: Any) -> None:
        if getattr(event.option_list, "id", None) != "target-table":
            return
        self._select_instrument()

    def on_option_list_option_selected(self, event: Any) -> None:
        self.action_inspect()

    def action_toggle_group(self) -> None:
        self._toggle_group()

    def action_member(self, delta: int) -> None:
        """Step through the tickers of a multi-ticker target."""
        key = self._selected()
        if key is None:
            return
        target_id = self.rows[key][0]
        members = self._members_by_target.get(target_id, [])
        if len(members) < 2:
            return
        current = self._selected_member.get(target_id, members[0])
        index = members.index(current) if current in members else 0
        self._selected_member[target_id] = members[(index + delta) % len(members)]
        self._select_instrument(force=True)
        self._paint_quotes()

    def _highlighted_key(self) -> str:
        """The highlighted option's id, group headers included.

        ``_selected`` answers "which target row?" and so drops the group
        headers; folding is the one action that wants them.
        """
        option = self.query_one("#target-table", WatchlistList).highlighted_option
        return str(option.id) if option and option.id else ""

    def _toggle_group(self, key: str | None = None) -> None:
        key = key or self._highlighted_key()
        if not key or not key.startswith("group:"):
            return
        if key in self._collapsed_groups:
            self._collapsed_groups.remove(key)
        else:
            self._collapsed_groups.add(key)
        self.refresh_view()
        # ``refresh_view`` restores the highlight from ``_selected``, which only
        # knows target rows — leave it to that and a collapsed group could never
        # be reopened, because the cursor would have jumped off its header.
        with suppress(Exception):
            table = self.query_one("#target-table", WatchlistList)
            table.highlighted = table.get_option_index(key)

    def action_inspect(self) -> None:
        """Refresh live metrics for the highlighted instrument; narrow: open them."""
        if self.has_class("-narrow") and not self.detail_open:
            self.detail_open = True
            self.layout_views()
        self._select_instrument(force=True)

    def _range_label(self) -> str:
        return {"day": "day", "month": "month", "all": "all time"}[self._range]

    def action_cycle_range(self) -> None:
        self._range = {"month": "all", "all": "day", "day": "month"}[self._range]
        self.query_one("#target-inspector-pane", Pane).set_hints(self._metric_hints())
        self.query_one("#target-chart-label", Static).update(f"price · {self._range_label()}")
        self._metrics.clear()
        self._select_instrument(force=True)

    def action_filter(self) -> None:
        if self.detail_open:
            self.detail_open = False
            self.layout_views()
        self.query_one("#tg-filter").display = True
        self.query_one("#tg-filter").focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tg-filter":
            self.refresh_view()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in {
            "tg-name",
            "tg-kind",
            "tg-asset-class",
            "tg-market",
            "tg-tickers",
            "tg-tags",
        }:
            event.stop()
            self.action_add()

    def action_cancel(self) -> None:
        """Escape: close the filter, or back out of the narrow metrics view."""
        self.query_one("#tg-filter", Input).value = ""
        self.query_one("#tg-filter").display = False
        if self.detail_open:
            self.detail_open = False
            self.layout_views()
        self.query_one("#target-table").focus()

    def action_add(self) -> None:
        self.app.push_screen(TargetAddModal(self.rig), self._target_added)

    def _target_added(self, name: str | None) -> None:
        if not name:
            return
        self.refresh_view()
        key = f"target:{name}"
        if key in self.rows:
            with suppress(Exception):
                table = self.query_one("#target-table", WatchlistList)
                table.highlighted = table.get_option_index(key)
        self.notify(f"added {name} to the watchlist")
        target = self.specs.get(name)
        if target and target.tickers and target.markets:
            instruments = [
                Instrument(
                    id=f"{target.markets[0].upper()}:{symbol}",
                    market=target.markets[0],
                    symbol=symbol,
                    currency={"us": "USD", "asx": "AUD"}.get(target.markets[0], ""),
                    asset_class=target.asset_class,
                )
                for symbol in target.tickers
            ]
            if self._collect_task and not self._collect_task.done():
                self._collect_task.cancel()
            self._collect_task = asyncio.create_task(self._collect_target(name, instruments))

    async def _collect_target(self, name: str, instruments: list[Instrument]) -> None:
        self.notify(f"gathering evidence for {name}…")
        try:
            result = await services.ingest(self.rig, instruments=instruments)
            self.notify(f"gathered {sum(result.counts.values())} records for {name}")
            self._metrics.clear()
            self._select_instrument(force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.notify(
                f"could not gather evidence for {name}: {exc} — press c to check the provider",
                severity="error",
            )

    def action_remove(self) -> None:
        key = self._selected()
        if key is None or key.startswith("child:"):
            self.notify("select a target first", severity="error")
            return
        name = self.rows[key][0]
        try:
            services.remove_target(name)
        except (ValueError, KeyError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.refresh_view()
        self.notify(f"removed {name} from the watchlist")

    async def on_screen_resume(self) -> None:
        self.active = True
        await super().on_screen_resume()

    async def _stop_feed(self) -> None:
        self.active = False
        if self.feed_task:
            self.feed_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.feed_task
            self.feed_task = None

    async def on_screen_suspend(self) -> None:
        await self._stop_feed()

    async def on_unmount(self) -> None:
        await self._stop_feed()
        if self._collect_task:
            self._collect_task.cancel()
