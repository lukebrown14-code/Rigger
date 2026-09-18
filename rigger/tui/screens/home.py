"""Home: the desk overview — six btop-style boxes on a 2×3 grid.

A ``RiggerScreen`` like every other panel, so the status bar is there where
a new user lands and ``h`` round-trips cleanly. One header row (an inked
``RIGGER`` chip, "overview", the live clock) sits above three ``PaneRow``s:

* top — ``watchlist`` (live quotes when the feed is up, last closes
  otherwise, 40-close sparks) and ``since you last looked`` (new evidence,
  the pulse histogram, stale bars and stale reports);
* middle — ``upcoming`` (calendar events) and ``theses`` (fleet health);
* bottom — ``go`` (every app hotkey) and ``system`` (plugins, data, spend).

Below 40 rows the middle row goes first and its content folds into two
summary lines under the watchlist. Below 100 columns only the watchlist and
go boxes survive, with a one-line system summary inside ``go``.

Every letter key on Home is an app-level binding; the screen only adds the
arrows, enter and tab, so nothing here can shadow navigation.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.message import Message
from textual.widgets import DataTable, OptionList, Static

from rigger import decisions, review, services
from rigger.core.models import Instrument
from rigger.core.state import read_last_seen
from rigger.core.time import to_utc
from rigger.plugins.data.yfinance import DEFAULT_SUFFIXES
from rigger.quotes import Quote, YahooQuotes
from rigger.tui.shell import RiggerScreen, age_text
from rigger.tui.widgets import (
    BrailleGraph,
    Pane,
    PaneRow,
    RiggerTable,
    binding_key,
    hint_markup,
    shown_bindings,
    token_color,
)

PULSE_DAYS = 30
WATCH_ROWS = 7
UPCOMING_ROWS = 5
THESES_ROWS = 6
SPARK_CLOSES = 40
#: A report older than this is called out beside the stale-bar warnings.
REPORT_STALE = timedelta(days=7)
#: How often the watchlist repaints from the quote feed.
QUOTE_PAINT_SECONDS = 0.5

#: Below this width only the watchlist and go boxes are shown.
NARROW_WIDTH = 100
#: Below this height the middle row folds into the watchlist summary lines.
TALL_HEIGHT = 40

#: Short labels for the go grid, keyed by the Textual key name; anything not
#: listed falls back to the binding's own description.
GO_LABELS: dict[str, str] = {
    "1": "watchlist",
    "2": "evidence",
    "3": "report",
    "4": "theses",
    "5": "ask",
    "c": "settings",
    "m": "model",
    "p": "provider",
    "g": "go to…",
    "question_mark": "help",
    "q": "quit",
    "f2": "theme",
}
#: Home itself: you are here, so the grid drops it and the border hint keeps it.
GO_SKIP = {"h"}

#: Health state -> theme text token for its dot.
STATE_TOKEN: dict[str, str] = {
    "building": "text-success",
    "mixed": "text-warning",
    "weakening": "text-warning",
    "challenged": "text-error",
    "emerging": "text-muted",
    "idle": "text-muted",
}


def go_items(bindings: Any) -> list[tuple[str, str, str]]:
    """``(key, label, action)`` for every shown app binding except Home.

    Derived from the bindings rather than written out, so the grid cannot
    drift from the keys the app actually answers to.
    """
    items = []
    for binding in shown_bindings(bindings):
        if binding.key in GO_SKIP:
            continue
        label = GO_LABELS.get(binding.key, binding.description.casefold())
        items.append((binding_key(binding), label, binding.action))
    return items


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _theses(count: int) -> str:
    return "1 thesis" if count == 1 else f"{count} theses"


def _compact(count: int) -> str:
    """41k / 1.9k / 96: table counts at the width the system box allows."""
    if count >= 10_000:
        return f"{count / 1000:.0f}k"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _when(ts: datetime, now: datetime) -> str:
    """Relative distance: what you react to, beside the date you diarise."""
    days = (ts.date() - now.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 14:
        return f"in {days}d"
    return f"in {days // 7}w"


def _price(value: float) -> str:
    return f"{value:,.2f}" if value >= 10 else f"{value:,.4f}"


def _watch_head(*, live: bool) -> str:
    """The watchlist column header, which names the price column it describes.

    ``last`` plus a live dot once a quote has arrived; ``close`` and no dot
    while the rows are still showing the newest stored bar.
    """
    price = "last" if live else "close"
    head = f"{' Symbol':<11}{price:>11}{'chg%':>9}{'age':>6}  {SPARK_CLOSES} closes"
    return escape(head) + ("  [$text-success]● live[/]" if live else "")


def _symbol(watched: list[tuple[str, Instrument]], instrument_id: str) -> str:
    for _target, instrument in watched:
        if instrument.id == instrument_id:
            return str(instrument.symbol)
    return instrument_id


class FocusBox(Vertical):
    """A pane body that takes focus so tab can land on it (no keys of its own)."""

    can_focus = True


class WatchRow(Horizontal):
    """One instrument: symbol, price, change, quote age, spark.

    The price cell is the live quote when one has arrived and the last stored
    close otherwise — the age cell says which, so a price is never passed off
    as fresher than it is. Cells are updated in place by ``paint`` rather than
    remounted, because the quote feed repaints twice a second.
    """

    def __init__(self, index: int, target_id: str, instrument: Instrument, closes: list[float]):
        super().__init__(classes="w-row")
        self.index = index
        self.target_id = target_id
        self.instrument = instrument
        self.closes = closes
        self.quote: Quote | None = None

    def compose(self) -> ComposeResult:
        yield Static(self.instrument.symbol, classes="w-sym", markup=False)
        yield Static("", classes="w-close", markup=False)
        yield Static("", classes="w-chg", markup=False)
        yield Static("", classes="w-age", markup=False)
        if self.closes:
            yield BrailleGraph(self.closes, fill=True, classes="w-spark")
        else:
            yield Static("no bars", classes="w-none", markup=False)

    def on_mount(self) -> None:
        self.paint()

    def set_quote(self, quote: Quote | None) -> None:
        """Adopt the newest quote (or its absence) and repaint the three cells."""
        self.quote = quote
        self.paint()

    def paint(self) -> None:
        price, pct, age, state = self._cells()
        with suppress(Exception):
            self.query_one(".w-close", Static).update("—" if price is None else _price(price))
            chg = self.query_one(".w-chg", Static)
            chg.update("" if price is None else "—" if pct is None else f"{pct:+.2f}%")
            chg.set_class(bool(pct and pct > 0), "-up")
            chg.set_class(bool(pct and pct < 0), "-down")
            cell = self.query_one(".w-age", Static)
            cell.update(age)
            cell.set_class(state == "warn" or state == "error", "-stale")

    def _cells(self) -> tuple[float | None, float | None, str, str]:
        """``(price, change %, age label, age state)`` for whatever evidence exists."""
        if self.quote is not None:
            pct = self.quote.change_pct
            if pct is None and self.closes and self.closes[-1]:
                pct = (self.quote.price - self.closes[-1]) / self.closes[-1] * 100
            age, state = age_text(datetime.now(UTC) - self.quote.received_at)
            return self.quote.price, pct, age, state
        if self.closes:
            last = self.closes[-1]
            prior = self.closes[-2] if len(self.closes) > 1 else None
            return last, ((last - prior) / prior * 100) if prior else None, "", "ok"
        return None, None, "", "ok"

    def on_click(self) -> None:
        rows = self.parent
        if isinstance(rows, WatchRows):
            rows.select(self.index)
            rows.focus()


class WatchRows(Vertical):
    """The watchlist body: a cursor over ``WatchRow``s, arrows and enter."""

    can_focus = True

    BINDINGS = [
        Binding("up", "cursor(-1)", "Up", show=False),
        Binding("down", "cursor(1)", "Down", show=False),
        Binding("enter", "open", "Open", show=False),
    ]

    class Open(Message):
        """Enter on a row: open that target on the Watchlist screen."""

        def __init__(self, target_id: str, instrument: Instrument) -> None:
            super().__init__()
            self.target_id = target_id
            self.instrument = instrument

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self.cursor = 0

    @property
    def rows(self) -> list[WatchRow]:
        return list(self.query(WatchRow))

    async def set_rows(self, rows: list[WatchRow]) -> None:
        await self.remove_children()
        if rows:
            await self.mount(*rows)
        self.cursor = min(self.cursor, max(len(rows) - 1, 0))
        self._paint()

    def select(self, index: int) -> None:
        rows = self.rows
        if rows:
            self.cursor = max(0, min(index, len(rows) - 1))
        self._paint()

    def action_cursor(self, delta: int) -> None:
        self.select(self.cursor + delta)

    def action_open(self) -> None:
        rows = self.rows
        if rows:
            row = rows[self.cursor]
            self.post_message(self.Open(row.target_id, row.instrument))

    def _paint(self) -> None:
        for row in self.rows:
            row.set_class(row.index == self.cursor, "-cursor")


class GoCell(Horizontal):
    """One ``▸ key label`` row in the go grid. Clickable: runs the app action."""

    def __init__(self, key: str, label: str, action: str) -> None:
        super().__init__(classes="go-cell")
        self.key = key
        self.label = label
        self.action = action

    def compose(self) -> ComposeResult:
        yield Static("▸", classes="go-glyph", markup=False)
        yield Static(self.key, classes="go-key", markup=False)
        yield Static(self.label, classes="go-label", markup=False)

    def retune(self, key: str, label: str, action: str) -> None:
        """Repoint an existing cell, so the grid never remounts its children."""
        self.key, self.label, self.action = key, label, action
        self.query_one(".go-key", Static).update(key)
        self.query_one(".go-label", Static).update(label)

    async def on_click(self) -> None:
        await self.app.run_action(self.action)


#: The system box's rows, in order. Mounted once in ``compose_content`` and
#: updated in place, so their ids stay unique across refreshes.
SYSTEM_ROWS = ("plugins", "data", "llm", "spend", "setup", "db")


class Home(RiggerScreen):
    name = "home"

    DEFAULT_CSS = """
    Home #home-header { height: 1; margin: 0 0 0 0; }
    Home #home-brand {
        width: auto;
        background: $primary;
        color: $block-cursor-foreground;
        text-style: bold;
    }
    Home #home-sub { width: auto; margin-left: 2; color: $text-muted; }
    Home #home-clock { width: 1fr; text-align: right; color: $text-muted; }
    Home #home-grid { height: 1fr; }

    /* Wide and tall: 13 / 13 / 12 at 40 rows; the top two rows share what a
       taller terminal adds. Folded (short or narrow): the middle row is gone
       and the go row is as tall as its grid needs. */
    Home #home-top { height: 1fr; }
    Home #home-mid { height: 1fr; }
    Home #home-bottom { height: 12; }
    Home.-folded #home-bottom { height: 8; }
    Home.-narrow #home-bottom { height: 9; }

    /* watchlist */
    Home #watch-head { height: 1; color: $text-muted; }
    Home #watch-rows { height: auto; }
    Home .w-row { height: 1; }
    Home .w-sym { width: 11; padding-left: 1; color: $foreground; text-style: bold; }
    Home .w-close { width: 11; text-align: right; color: $foreground; }
    Home .w-chg { width: 9; text-align: right; color: $text-muted; }
    Home .w-chg.-up { color: $text-success; }
    Home .w-chg.-down { color: $text-error; }
    Home .w-age { width: 6; text-align: right; color: $text-muted; }
    Home .w-age.-stale { color: $text-warning; }
    Home .w-none { width: 1fr; margin-left: 2; color: $text-muted; }
    Home .w-spark { width: 1fr; height: 1; margin: 0 1 0 2; }
    Home .w-spark > .braille-graph--low-color { color: $text-primary 45%; }
    Home .w-spark > .braille-graph--high-color { color: $text-primary; }
    Home .w-row.-cursor { background: $block-cursor-blurred-background; }
    Home WatchRows:focus .w-row.-cursor { background: $primary; }
    Home .w-row.-cursor Static { color: $block-cursor-foreground; }
    Home .w-row.-cursor .w-spark > .braille-graph--low-color { color: $block-cursor-foreground 60%; }
    Home .w-row.-cursor .w-spark > .braille-graph--high-color { color: $block-cursor-foreground; }
    Home #watch-note, Home #watch-since, Home #watch-next {
        height: 1;
        padding-left: 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    Home #watch-note, Home #watch-since { margin-top: 1; }

    /* since you last looked */
    Home #since-body { height: 1fr; padding: 0 1; }
    Home .since-line { height: 1; }
    Home .since-left { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    Home .since-right { width: auto; margin-left: 2; color: $text-muted; }
    Home #since-head { margin-bottom: 1; }
    Home #since-spark { width: 1fr; height: 2; }
    Home #since-spark > .braille-graph--low-color { color: $text-primary 45%; }
    Home #since-spark > .braille-graph--high-color { color: $text-primary; }
    Home #since-axis { margin-bottom: 1; color: $text-muted; }
    Home #since-newest { margin-bottom: 1; }
    Home #since-stale { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    Home #since-review { height: 1; color: $warning; text-wrap: nowrap; text-overflow: ellipsis; }

    /* upcoming / theses */
    Home #upcoming-table, Home #theses-table { height: 1fr; overflow-x: hidden; }
    Home #upcoming-empty, Home #theses-empty {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    Home #upcoming-note { height: 1; padding: 0 1; color: $text-muted; }
    Home #theses-summary { height: 1; padding: 0 1; margin-bottom: 1; text-wrap: nowrap; text-overflow: ellipsis; }

    /* go */
    Home #go-grid {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        layout: grid;
        grid-size: 3;
        grid-rows: 1;
        grid-gutter: 0 1;
    }
    Home.-narrow #go-grid { grid-size: 4; margin-top: 0; }
    Home .go-cell { height: 1; }
    Home .go-glyph { width: 2; color: $text-primary; }
    Home .go-key { width: 3; color: $text-primary; text-style: bold; }
    Home .go-label { width: 1fr; color: $text-muted; }
    Home .go-cell:hover .go-label { color: $foreground; }
    Home #go-system {
        height: 1;
        margin-top: 1;
        padding: 0 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }

    /* system */
    Home #system-rows { height: auto; padding: 0 1; }
    Home .sys-row { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    Home #system-db { margin-top: 1; }
    Home.-short #system-db { display: none; }
    """

    def __init__(self, rig: Any, last_seen: datetime | None = None) -> None:
        super().__init__(rig)
        # Captured once for the session. Mounted standalone (no app), fall back
        # to the stored value so the panel still has a reference point.
        self.last_seen = last_seen or read_last_seen(rig.cfg)
        # Cached between refreshes so a resize can re-lay the tables.
        self._watched_rows: list[tuple[str, Instrument]] = []
        self._events: list[services.Upcoming] | None = None
        self._fleet: list[services.ThesisHealth] = []
        # Ephemeral quotes, exactly as the Watchlist runs them: one task owned
        # by the screen, started on resume and cancelled on suspend/unmount.
        self.feed: YahooQuotes | None = None
        self.feed_task: asyncio.Task | None = None
        self.active = False
        self.signature: tuple = ()
        #: Test seam: an alternative websocket client for ``YahooQuotes``.
        self.quote_client_factory: Any = None

    # ----- layout ---------------------------------------------------------

    def compose_content(self) -> ComposeResult:
        with Horizontal(id="home-header"):
            yield Static(" RIGGER ", id="home-brand", markup=False)
            yield Static("overview", id="home-sub", markup=False)
            yield Static("", id="home-clock", markup=False)
        with Vertical(id="home-grid"):
            with PaneRow(id="home-top"):
                with Pane(
                    title="watchlist",
                    hints=hint_markup(("↑↓", "select"), ("enter", "open"), ("tab", "next box")),
                    id="watch-pane",
                ):
                    yield Static(_watch_head(live=False), id="watch-head")
                    yield WatchRows(id="watch-rows")
                    yield Static("", id="watch-note", markup=False)
                    yield Static("", id="watch-since")
                    yield Static("", id="watch-next")
                with Pane(
                    title="since you last looked",
                    hints=hint_markup(("2", "evidence")),
                    id="since-pane",
                ):
                    with FocusBox(id="since-body"):
                        with Horizontal(id="since-head", classes="since-line"):
                            yield Static("", classes="since-left")
                            yield Static("", classes="since-right", markup=False)
                        yield BrailleGraph([], fill=True, id="since-spark")
                        with Horizontal(id="since-axis", classes="since-line"):
                            yield Static(
                                f"{PULSE_DAYS} days ago", classes="since-left", markup=False
                            )
                            yield Static("today", classes="since-right", markup=False)
                        yield Static("", id="since-rank", classes="since-line")
                        with Horizontal(id="since-newest", classes="since-line"):
                            yield Static("", classes="since-left")
                            yield Static("", classes="since-right", markup=False)
                        yield Static("", id="since-stale")
                        yield Static("", id="since-review", markup=False)
            with PaneRow(id="home-mid"):
                with Pane(
                    title="upcoming",
                    hints=hint_markup(("enter", "open evidence")),
                    id="upcoming-pane",
                ):
                    yield RiggerTable(id="upcoming-table")
                    yield Static("", id="upcoming-empty", markup=False)
                    yield Static(
                        "calendar plugin: earnings & dividends only",
                        id="upcoming-note",
                        markup=False,
                    )
                with Pane(
                    title="theses",
                    hints=hint_markup(("enter", "open thesis"), ("4", "all")),
                    id="theses-pane",
                ):
                    yield Static("", id="theses-summary")
                    yield RiggerTable(id="theses-table")
                    yield Static("", id="theses-empty", markup=False)
            with PaneRow(id="home-bottom"):
                with Pane(
                    title="go", hints=hint_markup(("g", "palette"), ("h", "home")), id="go-pane"
                ):
                    yield Vertical(id="go-grid")
                    yield Static("", id="go-system")
                with Pane(
                    title="system",
                    hints=hint_markup(("c", "settings"), ("p", "provider"), ("m", "model")),
                    id="system-pane",
                ):
                    with Vertical(id="system-rows"):
                        for row in SYSTEM_ROWS:
                            yield Static("", classes="sys-row", id=f"system-{row}")

    async def on_mount(self) -> None:
        self.layout_views()
        await self.refresh_view()
        self.set_interval(1, self._tick)
        self.set_interval(QUOTE_PAINT_SECONDS, self._paint_quotes)

    def on_resize(self) -> None:
        if self.is_mounted:
            self.layout_views()

    def layout_views(self) -> None:
        """Wide+tall: six boxes. Short: no middle row. Narrow: watchlist and go only."""
        narrow = self.size.width < NARROW_WIDTH
        short = self.size.height < TALL_HEIGHT
        folded = narrow or short
        self.set_class(narrow, "-narrow")
        self.set_class(short, "-short")
        self.set_class(folded, "-folded")
        self.query_one("#home-mid").display = not folded
        self.query_one("#since-pane").display = not narrow
        self.query_one("#system-pane").display = not narrow
        self.query_one("#watch-note").display = not folded
        self.query_one("#watch-since").display = folded
        self.query_one("#watch-next").display = folded
        self.query_one("#go-system").display = narrow
        # Table columns are sized to the box, so a resize re-lays them.
        if self._events is not None and not folded:
            self._refresh_upcoming(self._watched_rows, self._events)
            self._refresh_theses(self._fleet)

    # ----- data -----------------------------------------------------------

    def _tick(self) -> None:
        now = datetime.now().astimezone()
        self.query_one("#home-clock", Static).update(
            f"{now:%A %d %B %Y} · {now:%H:%M:%S} {now:%Z}".rstrip()
        )

    def _token(self, name: str) -> str:
        """A theme colour Rich can parse; empty (default colour) when unknown.

        Never the raw ``theme_variables`` value: Textual writes ``auto 87%``
        for background-dependent tokens and Rich raises ``MissingStyle`` on it.
        """
        return token_color(self.app, name)

    # ----- quotes ---------------------------------------------------------

    def _sync_feed(self) -> None:
        """Point the quote feed at the watched instruments, restarting if they changed."""
        suffixes = DEFAULT_SUFFIXES | getattr(self.rig.cfg, "plugins", {}).get("yfinance", {}).get(
            "suffixes", {}
        )
        instruments = [instrument for _target, instrument in self._watched_rows[:WATCH_ROWS]]
        signature = (
            tuple(sorted(i.id for i in instruments)),
            tuple(sorted(suffixes.items())),
        )
        if self.feed_task and not self.feed_task.done() and signature == self.signature:
            return
        old_task = self.feed_task
        if old_task:
            old_task.cancel()
        old_quotes = self.feed.quotes if self.feed else {}
        self.signature = signature
        self.feed = YahooQuotes(instruments, suffixes, self._quote_state, self.quote_client_factory)
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

    def _quote_for(self, ident: str) -> Quote | None:
        return self.feed.quotes.get(ident) if self.feed else None

    def _paint_quotes(self) -> None:
        """Repaint the watchlist from the feed. Reads cached quotes only — never the network."""
        if not self.is_mounted:
            return
        try:
            rows = self.query_one("#watch-rows", WatchRows).rows
            head = self.query_one("#watch-head", Static)
        except Exception:
            return
        live = False
        for row in rows:
            quote = self._quote_for(row.instrument.id)
            row.set_quote(quote)
            live = live or quote is not None
        head.update(_watch_head(live=live))

    async def _stop_feed(self) -> None:
        self.active = False
        if self.feed_task:
            self.feed_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.feed_task
            self.feed_task = None

    async def on_screen_resume(self) -> None:
        self.active = True
        await super().on_screen_resume()
        self._sync_feed()

    async def on_screen_suspend(self) -> None:
        await self._stop_feed()

    async def on_unmount(self) -> None:
        await self._stop_feed()

    def _watched(self) -> list[tuple[str, Instrument]]:
        """Every ticker of every watch target, keyed by the target it belongs to.

        Prefers the universe instrument (it carries the bars); a ticker the
        universe does not know is synthesised so the row still shows, as
        "no bars" — the honest reading, and the same rule the Watchlist uses.
        """
        known = {instrument.id: instrument for instrument in self.rig.universe()}
        by_symbol = {instrument.symbol.upper(): instrument for instrument in known.values()}
        watched: list[tuple[str, Instrument]] = []
        seen: set[str] = set()
        for target in sorted(services.target_specs().values(), key=lambda t: t.id):
            for market in target.markets:
                for symbol in target.tickers:
                    ident = f"{market.upper()}:{symbol}"
                    instrument = known.get(ident) or by_symbol.get(symbol.upper())
                    if instrument is None:
                        instrument = Instrument(
                            id=ident,
                            market=market,
                            symbol=symbol,
                            currency={"us": "USD", "asx": "AUD"}.get(market, ""),
                            asset_class=getattr(target, "asset_class", "equity"),
                        )
                    if instrument.id in seen:
                        continue
                    seen.add(instrument.id)
                    watched.append((target.id, instrument))
        return watched

    async def refresh_view(self) -> None:
        self._tick()
        engine = self.rig.engine
        watched = self._watched()
        ids = [instrument.id for _target, instrument in watched]
        health = services.data_health(self.rig)
        pulse = services.pulse(engine, instrument_ids=ids, since=self.last_seen, days=PULSE_DAYS)
        events = services.upcoming_events(engine, instrument_ids=ids, limit=UPCOMING_ROWS)
        try:
            fleet = services.thesis_fleet(engine)
        except Exception:
            fleet = []
        checks = services.setup_checks(self.rig)
        closes = {
            instrument.id: services.recent_closes(engine, instrument.id, limit=SPARK_CLOSES)
            for _target, instrument in watched
        }
        stale = self._stale(watched, closes, health)
        self._watched_rows, self._events, self._fleet = watched, events, fleet

        await self._refresh_watchlist(watched, closes)
        self._refresh_since(watched, pulse, stale)
        self._refresh_review([instrument.id for _target, instrument in watched])
        self._refresh_upcoming(watched, events)
        self._refresh_theses(fleet)
        self._refresh_go()
        self._refresh_system(health, checks)
        self._refresh_summary(watched, pulse, events, fleet, stale)

    def _stale(
        self,
        watched: list[tuple[str, Instrument]],
        closes: dict[str, list[float]],
        health: Any,
    ) -> list[str]:
        """Warnings worth a ⚠: watched instruments with old or missing bars, plugins off.

        Bar age is known only for universe instruments (``data_health`` walks
        the universe); a ticker outside it with bars is left alone rather than
        called stale on no evidence. A company with no report at all is not a
        warning — nothing is out of date until something has been written.
        """
        now = datetime.now(UTC)
        reports_dir = str(getattr(self.rig.cfg, "reports_dir", "reports") or "reports")
        notes: list[str] = []
        for _target, instrument in watched:
            newest = health.latest_bar.get(instrument.id)
            if newest is None:
                if not closes.get(instrument.id):
                    notes.append(f"{instrument.symbol} no bars")
            else:
                age = now - to_utc(newest)
                if age > timedelta(days=1):
                    notes.append(f"{instrument.symbol} {age_text(age)[0]} old")
            as_of = services.latest_report_age(reports_dir, instrument.id)
            if as_of is not None:
                report_age = now - to_utc(as_of)
                if report_age > REPORT_STALE:
                    notes.append(f"{instrument.symbol} report {age_text(report_age)[0]} old")
        plugins = getattr(self.rig, "plugins", {}) or {}
        notes.extend(
            f"{name} off"
            for name, plugin in plugins.items()
            if not getattr(plugin, "enabled", False)
        )
        return notes

    async def _refresh_watchlist(
        self, watched: list[tuple[str, Instrument]], closes: dict[str, list[float]]
    ) -> None:
        rows = [
            WatchRow(index, target_id, instrument, closes.get(instrument.id, []))
            for index, (target_id, instrument) in enumerate(watched[:WATCH_ROWS])
        ]
        await self.query_one("#watch-rows", WatchRows).set_rows(rows)
        if self.active:
            self._sync_feed()
        self._paint_quotes()
        self.query_one("#watch-pane", Pane).set_badge(str(len(watched)) if watched else "")
        hidden = len(watched) - WATCH_ROWS
        note = self.query_one("#watch-note", Static)
        if not watched:
            note.update("nothing yet — 1 builds the watchlist")
        elif hidden > 0:
            note.update(f"+{hidden} more · 1 watchlist")
        else:
            note.update(f"spark: {SPARK_CLOSES} daily closes · chg%: move on the day")

    def _refresh_since(
        self, watched: list[tuple[str, Instrument]], pulse: services.Pulse, stale: list[str]
    ) -> None:
        counts = [
            f"[b]{_plural(n, noun)}[/b]"
            for n, noun in (
                (pulse.articles, "article"),
                (pulse.filings, "filing"),
                (pulse.events, "event"),
            )
            if n
        ]
        head = self.query_one("#since-head")
        head.query_one(".since-left", Static).update(
            "   ".join(counts) if counts else "[$text-muted]nothing new since your last visit[/]"
        )
        head.query_one(".since-right", Static).update(
            f"since {self.last_seen.astimezone():%a %H:%M}"
        )
        self.query_one("#since-pane", Pane).set_badge(f"{pulse.total} new" if pulse.total else "")

        spark = self.query_one("#since-spark", BrailleGraph)
        spark.data = pulse.daily
        spark.display = any(pulse.daily)
        self.query_one("#since-axis").display = any(pulse.daily)

        if pulse.busiest:
            quiet = (
                f"   [$text-muted]quietest[/]  {escape(_symbol(watched, pulse.quietest[0]))}"
                f" [$text-muted]({pulse.quietest[1]})[/]"
                if pulse.quietest
                else ""
            )
            rank = (
                f"[$text-muted]busiest[/]  {escape(_symbol(watched, pulse.busiest[0]))}"
                f" [$text-muted]({pulse.busiest[1]})[/]{quiet}"
            )
        else:
            rank = f"[$text-muted]no activity in the last {PULSE_DAYS} days[/]"
        self.query_one("#since-rank", Static).update(rank)

        newest = self.query_one("#since-newest")
        ids = [instrument.id for _target, instrument in watched]
        headline = services.latest_headline(self.rig.engine, instrument_ids=ids)
        if headline:
            newest.query_one(".since-left", Static).update(
                f"[$text-muted]newest[/]   {escape(headline.title)}"
            )
            newest.query_one(".since-right", Static).update(
                age_text(datetime.now(UTC) - headline.ts)[0]
            )
        else:
            newest.query_one(".since-left", Static).update(
                "[$text-muted]newest   no articles yet[/]"
            )
            newest.query_one(".since-right", Static).update("")

        self.query_one("#since-stale", Static).update(
            f"[$text-warning]⚠ {escape(' · '.join(stale))}[/]"
            if stale
            else "[$text-muted]✓ bars and reports fresh · all plugins on[/]"
        )

    def _refresh_review(self, instrument_ids: list[str]) -> None:
        """Expose evidence and journal prompts without making an investment call."""
        line = self.query_one("#since-review", Static)
        try:
            prompts = review.review_queue(
                self.rig, instrument_ids=instrument_ids, since=self.last_seen
            )
            due = decisions.due_reviews(self.rig.engine)
        except Exception:
            line.update("")
            return
        if due:
            line.update(f"⚠ {len(due)} decision review{'s' if len(due) != 1 else ''} due · 6 decisions")
        elif prompts:
            line.update(f"⚠ {len(prompts)} evidence prompt{'s' if len(prompts) != 1 else ''} · 2 evidence")
        else:
            line.update("✓ no decision or evidence reviews due")

    def _refresh_upcoming(
        self, watched: list[tuple[str, Instrument]], events: list[services.Upcoming]
    ) -> None:
        table = self.query_one("#upcoming-table", DataTable)
        table.clear(columns=True)
        widths = (7, 11, 6, 7)
        detail = self._table_width("#upcoming-pane") - sum(widths) - 2 * (len(widths) + 1)
        for label, width in zip(("Symbol", "Event", "Date", "When"), widths, strict=True):
            table.add_column(label, width=width)
        table.add_column("Detail", width=max(detail, 4))
        now = datetime.now(UTC)
        muted = self._token("text-muted")
        accent = self._token("text-primary")
        for item in events:
            table.add_row(
                Text(_symbol(watched, item.instrument_id), style="bold", no_wrap=True),
                Text(item.kind, style=muted, no_wrap=True, overflow="ellipsis"),
                f"{item.ts:%d %b}",
                Text(_when(item.ts, now), style=accent),
                Text(item.summary, style=muted, no_wrap=True, overflow="ellipsis"),
            )
        table.display = bool(events)
        empty = self.query_one("#upcoming-empty", Static)
        empty.display = not events
        empty.update("nothing scheduled — press 2, then U to gather evidence")
        self.query_one("#upcoming-pane", Pane).set_badge(str(len(events)) if events else "")

    def _refresh_theses(self, fleet: list[services.ThesisHealth]) -> None:
        summary = self.query_one("#theses-summary", Static)
        table = self.query_one("#theses-table", DataTable)
        empty = self.query_one("#theses-empty", Static)
        table.clear(columns=True)
        ratio_w = len("for/against")
        table.add_column("", width=1)
        table.add_column(
            "Most at risk", width=max(self._table_width("#theses-pane") - ratio_w - 7, 8)
        )
        table.add_column("for/against", width=ratio_w)
        if not fleet:
            summary.update("[$text-muted]no theses yet[/]")
            table.display = False
            empty.display = True
            empty.update("4 opens the theses desk — a tracks a claim")
            self.query_one("#theses-pane", Pane).set_badge("")
            return
        counts: dict[str, int] = {}
        for entry in fleet:
            counts[entry.state] = counts.get(entry.state, 0) + 1
        summary.update(
            "   ".join(
                f"[${STATE_TOKEN.get(state, 'text-muted')}]● {n} {state}[/]"
                for state, n in sorted(
                    counts.items(), key=lambda kv: services.RISK_ORDER.get(kv[0], 99)
                )
            )
        )
        for entry in fleet[:THESES_ROWS]:
            result = entry.result
            ratio = f"{result.support} / {result.against}" if result else "—"
            table.add_row(
                Text("●", style=self._token(STATE_TOKEN.get(entry.state, "text-muted"))),
                Text(entry.thesis.claim, no_wrap=True, overflow="ellipsis"),
                Text(ratio, style=self._token("text-muted"), justify="right"),
            )
        table.display = True
        empty.display = False
        challenged = counts.get("challenged", 0)
        badge = str(len(fleet)) + (f" · {challenged} challenged" if challenged else "")
        self.query_one("#theses-pane", Pane).set_badge(badge)

    def _table_width(self, pane_id: str) -> int:
        """Columns a table inside ``pane_id`` may use: the box interior, or a guess before layout."""
        width = self.query_one(pane_id).content_size.width
        if width <= 0:
            width = max(self.size.width - 2, NARROW_WIDTH) // 2 - 2
        return width

    def _refresh_go(self) -> None:
        grid = self.query_one("#go-grid", Vertical)
        items = go_items(getattr(self.app, "BINDINGS", []))
        cells = list(grid.query(GoCell))
        if [(cell.key, cell.label, cell.action) for cell in cells] == items:
            return
        # Mount once; afterwards retune the existing cells rather than
        # remove-then-mount, which races the async removal.
        if not cells:
            grid.mount(*(GoCell(key, label, action) for key, label, action in items))
            return
        for cell, (key, label, action) in zip(cells, items, strict=False):
            cell.retune(key, label, action)

    def _refresh_system(self, health: Any, checks: list[services.Check]) -> None:
        plugins = getattr(self.rig, "plugins", {}) or {}
        on = [name for name, plugin in plugins.items() if getattr(plugin, "enabled", False)]
        off = [name for name in plugins if name not in on]
        now = datetime.now(UTC)

        if health.latest_bar:
            bars_label, bars_state = age_text(now - to_utc(max(health.latest_bar.values())))
            bars = f"bars {bars_label} old" if bars_label != "live" else "bars live"
        else:
            bars_label, bars_state, bars = "none", "error", "no bars"
        counts = health.counts
        provider = str(getattr(self.rig.cfg, "llm_provider", "") or "—")
        try:
            from rigger.llm.router import model_for

            model = model_for(self.rig.cfg, "report")
        except Exception:
            model = "no route"
        last_llm = (
            f" · last call {age_text(now - to_utc(health.last_llm))[0]}" if health.last_llm else ""
        )
        rows = services.llm_costs(self.rig.engine)
        total = sum(row.cost_usd for row in rows)
        calls = sum(row.calls for row in rows)
        today = sum(
            row.cost_usd
            for row in services.llm_costs(self.rig.engine, since=now.strftime("%Y-%m-%d"))
        )
        failing = [check.name for check in checks if not check.ok]

        def dot(state: str) -> str:
            token = {"ok": "text-success", "warn": "text-warning"}.get(state, "text-error")
            return f"[${token}]●[/]"

        def label(text: str) -> str:
            return f"[$text-muted]{text:<9}[/]"

        lines: list[tuple[str, str]] = [
            (
                "plugins",
                f"{dot('ok' if not off else 'warn')} {len(on)}/{len(plugins)} on"
                + (f"[$text-muted] · {escape(' '.join(off))} off[/]" if off else "")
                + (f"[$text-muted] · {escape(' '.join(on))}[/]" if on else ""),
            ),
            (
                "data",
                f"{dot(bars_state)} {bars}[$text-muted] · {_compact(counts.get('bar', 0))} bars"
                f" · {_compact(counts.get('newsitem', 0))} news"
                f" · {_compact(counts.get('event', 0))} events[/]",
            ),
            ("llm", f"{escape(provider)}[$text-muted] · {escape(model)}{escape(last_llm)}[/]"),
            (
                "spend",
                f"${total:.2f} total[$text-muted] · ${today:.2f} today · {_plural(calls, 'call')}[/]",
            ),
            (
                "setup",
                f"[$text-warning]⚠ {len(failing)} failing: {escape(', '.join(failing))}[/]"
                if failing
                else f"[$text-success]✓ {len(checks)} checks passed[/]",
            ),
        ]
        # Update in place: ``remove_children`` is async, so a remove-then-mount
        # here would race the next refresh and duplicate the row ids.
        for name, text in lines:
            self.query_one(f"#system-{name}", Static).update(label(name) + text)
        db = self._db_line()
        db_row = self.query_one("#system-db", Static)
        db_row.display = bool(db)
        db_row.update(label("db") + db if db else "")
        self.query_one("#system-pane", Pane).set_badge(
            "ok" if not failing else f"{len(failing)} to fix"
        )
        summary = (
            f"{len(on)}/{len(plugins)} plugins · {bars} · {escape(provider)} · ${total:.2f} · "
            + (
                f"[$text-warning]⚠ {len(failing)} to fix[/]"
                if failing
                else "[$text-success]✓ setup ok[/]"
            )
        )
        self.query_one("#go-system", Static).update(summary)

    def _db_line(self) -> str:
        path = Path(str(getattr(self.rig.cfg, "db_path", "") or ""))
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        mb = size / 1_048_576
        shown = f"{mb:.1f} MB" if mb >= 1 else f"{size / 1024:.0f} KB"
        return f"[$text-muted]{escape(path.name)} · {shown}[/]"

    def _refresh_summary(
        self,
        watched: list[tuple[str, Instrument]],
        pulse: services.Pulse,
        events: list[services.Upcoming],
        fleet: list[services.ThesisHealth],
        stale: list[str],
    ) -> None:
        """The two lines that stand in for the middle row when it is folded away."""
        counts = (
            " ".join(
                f"[b]{_plural(n, noun)}[/b]"
                for n, noun in (
                    (pulse.articles, "article"),
                    (pulse.filings, "filing"),
                    (pulse.events, "event"),
                )
                if n
            )
            or "nothing new"
        )
        since = f"[$text-muted]since {self.last_seen.astimezone():%a %H:%M}[/]  {counts}"
        if stale:
            since += f"   [$text-warning]⚠ {escape(stale[0])}[/]"
            if len(stale) > 1:
                since += f"[$text-muted] +{len(stale) - 1}[/]"
        self.query_one("#watch-since", Static).update(since)

        upcoming = (
            " · ".join(
                f"{escape(_symbol(watched, item.instrument_id))} {escape(item.kind)} {item.ts:%d %b}"
                for item in events[:2]
            )
            or "nothing scheduled"
        )
        line = f"[$text-muted]next[/]  {upcoming}"
        challenged = sum(1 for entry in fleet if entry.state == "challenged")
        weakening = sum(1 for entry in fleet if entry.state == "weakening")
        if challenged:
            line += f"   [$text-error]● {_theses(challenged)} challenged[/]"
        elif weakening:
            line += f"   [$text-warning]● {_theses(weakening)} weakening[/]"
        elif fleet:
            line += f"   [$text-muted]● {_theses(len(fleet))} tracked[/]"
        self.query_one("#watch-next", Static).update(line)

    # ----- keys -----------------------------------------------------------

    def on_watch_rows_open(self, event: WatchRows.Open) -> None:
        """Enter on the watchlist: open that target on the Watchlist screen."""
        switch = getattr(self.app, "action_switch_screen", None)
        if not callable(switch):
            return
        switch("targets")
        self.app.call_later(self._highlight_target, f"target:{event.target_id}")

    def _highlight_target(self, option_id: str, attempts: int = 5) -> None:
        """Move the Watchlist cursor to the row once that screen has built it."""
        targets = getattr(self.app, "screens_by_name", {}).get("targets")
        if targets is None:
            return
        try:
            table = targets.query_one("#target-table", OptionList)
            table.highlighted = table.get_option_index(option_id)
        except Exception:
            if attempts > 0:
                self.app.set_timer(0.05, lambda: self._highlight_target(option_id, attempts - 1))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on the upcoming or theses table: open the matching desk."""
        screen = {"upcoming-table": "data", "theses-table": "theses"}.get(event.data_table.id or "")
        switch = getattr(self.app, "action_switch_screen", None)
        if screen and callable(switch):
            switch(screen)
