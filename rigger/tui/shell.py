"""Persistent shell: the footer status bar and shared screen base.

There is no nav rail and no top bar. Navigation is keyboard-driven (number
keys, ``g``, the command palette) and the chrome is a single docked row:
every panel with its hotkey on the left, the current one highlighted, then
the live status cells, then the chrome actions and the help/palette hint
flush right. Shell styling lives in ``DEFAULT_CSS`` so the screens that
double as standalone panels (reports, theses, chat) keep the shell when
mounted under any App, and inherit theme tokens when a Rigger theme is
active.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Static

from rigger import services
from rigger.tui.widgets import StatusDot

NAV_ITEMS: list[tuple[str, str, str]] = [
    ("1", "targets", "Watchlist"),
    ("2", "data", "Research"),
    ("4", "theses", "Theses"),
    ("5", "chat", "Ask"),
    ("6", "decisions", "Decisions"),
]

# Chrome actions, rendered flush-right on the status bar. Same 3-tuple shape as
# NAV_ITEMS so GoPicker / the command palette can iterate NAV_ITEMS + CHROME_ITEMS.
CHROME_ITEMS: list[tuple[str, str, str]] = [
    ("c", "config", "Settings"),
]

# Reachable by hotkey, ``g`` and the palette, but not shown on the bar: Home is
# the screen you land on, so a permanent entry pointing at it earns no columns.
# Same 3-tuple shape again — ALL_ITEMS is what navigation should iterate.
OFF_BAR_ITEMS: list[tuple[str, str, str]] = [
    ("h", "home", "Home"),
    ("3", "reports", "Research · Report"),
]

ALL_ITEMS: list[tuple[str, str, str]] = NAV_ITEMS + CHROME_ITEMS + OFF_BAR_ITEMS

#: The bar's trailing hint. ``g`` opens the Go picker; the caret notation this
#: used to carry pointed at ctrl+p, which is Textual's command palette, not Go.
CHROME_HINT = "? help · g go"


def age_text(age: timedelta) -> tuple[str, str]:
    """Humanise a data age into (label, dot-state)."""
    seconds = age.total_seconds()
    if seconds < 300:
        return "live", "ok"
    if seconds < 3600:
        return f"{int(seconds // 60)}m", "ok"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h", "warn" if seconds > 86400 / 2 else "ok"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d", "warn"
    return f"{int(seconds // 86400)}d", "error"


class NavKey(Static):
    """One footer entry: hotkey plus panel name, clickable."""

    class Selected(Message):
        """Posted when a footer entry is clicked."""

        def __init__(self, screen_name: str) -> None:
            super().__init__()
            self.screen_name = screen_name

    DEFAULT_CSS = """
    NavKey {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    NavKey.-active {
        background: $primary;
        color: $block-cursor-foreground;
        text-style: bold;
    }
    NavKey:hover {
        background: $surface;
    }
    """

    def __init__(self, key: str, screen_name: str, label: str) -> None:
        super().__init__(id=f"nav-{screen_name}")
        self.screen_name = screen_name
        self._key = key
        self._label = label
        self.render_label(compact=False, active=False)

    def render_label(self, *, compact: bool, active: bool) -> None:
        """Show the label unless the strip is too narrow to fit every name.

        The active entry always keeps its label: it is the only thing naming
        the panel you are on.
        """
        show_label = active or not compact
        self.update(f"[b]{self._key}[/b] {self._label}" if show_label else f"[b]{self._key}[/b]")

    def on_click(self) -> None:
        self.post_message(NavKey.Selected(self.screen_name))


class StatusBar(Horizontal):
    """The whole footer on one row: panels, live status cells, chrome.

    Left to right: every panel with its hotkey (current one highlighted), a
    ``1fr`` context cell that doubles as the spacer, the live status cells,
    then ``c Config`` and the help/palette hint flush right. The panel name
    is not repeated among the status cells — the highlight already names it.
    """

    #: Everything labelled needs 109 columns: five panel entries (53), the
    #: status cells (30), ``c Config`` (10) and the chrome hint (16). Below
    #: that every panel entry but the active one drops to its hotkey alone,
    #: which buys back up to 28.
    COMPACT_WIDTH = 109

    #: Even fully compacted the row still wants 81 columns. Below that the
    #: hint and the provider name give up theirs, in that order: the hint is a
    #: reminder you need once, and the provider is the least urgent cell.
    MINIMAL_WIDTH = 81

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $panel;
    }
    StatusBar #sl-context {
        width: 1fr;
        padding: 0 1;
        color: $text-muted;
    }
    StatusBar .sl-value {
        width: auto;
        padding: 0 1;
        color: $foreground;
    }
    StatusBar #sl-dot {
        width: 2;
        padding: 0 0 0 1;
    }
    StatusBar #sl-keys {
        width: auto;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, rig: Any, active: str = "", context: str = "") -> None:
        super().__init__()
        self.rig = rig
        # Chrome entries live in the same map so the current screen still picks
        # up its ``-active`` highlight when it is a chrome screen (Config).
        self._items = {
            name: NavKey(key, name, label) for key, name, label in NAV_ITEMS + CHROME_ITEMS
        }
        self._active = active
        self._compact = False
        # NB: not ``self._context`` — that name shadows a Textual MessagePump
        # attribute and silently deadlocks the widget's message loop.
        self._ctx = Static(context, id="sl-context", markup=False)
        self._dot = StatusDot("warn", id="sl-dot")
        self._freshness = Static("data —", markup=False, classes="sl-value")
        self._model = Static("", markup=False, classes="sl-value")
        self._spend = Static("", markup=False, classes="sl-value")
        self._keys = Static(CHROME_HINT, id="sl-keys", markup=False)

    def compose(self) -> ComposeResult:
        # Flat children only: auto-width Horizontals nested inside a docked
        # auto row deadlock Textual's layout pass. The 1fr context cell — not
        # a nested container — is what pushes everything after it flush right.
        for _key, name, _label in NAV_ITEMS:
            yield self._items[name]
        yield self._ctx
        yield self._dot
        yield self._freshness
        yield self._model
        yield self._spend
        for _key, name, _label in CHROME_ITEMS:
            yield self._items[name]
        yield self._keys

    def on_mount(self) -> None:
        self.set_active(self._active)
        self._refresh()
        self.set_interval(30, self._refresh)

    def on_resize(self, event: Any) -> None:
        width = event.size.width
        self._compact = width < self.COMPACT_WIDTH
        minimal = width < self.MINIMAL_WIDTH
        self._keys.display = not minimal
        self._model.display = not minimal
        self._render_items()

    def set_active(self, screen_name: str) -> None:
        self._active = screen_name
        self._render_items()

    def set_context(self, text: str) -> None:
        self._ctx.update(text)

    def _render_items(self) -> None:
        chrome = {name for _key, name, _label in CHROME_ITEMS}
        for name, item in self._items.items():
            active = name == ("data" if self._active == "reports" else self._active)
            item.set_class(active, "-active")
            # The chrome group keeps its labels at every width.
            item.render_label(compact=self._compact and name not in chrome, active=active)

    def _refresh(self) -> None:
        """Update status cells; the status bar must never take a screen down."""
        try:
            health = services.data_health(self.rig)
            if health.latest_bar:
                newest = max(health.latest_bar.values())
                if newest.tzinfo is None:
                    newest = newest.replace(tzinfo=UTC)
                label, state = age_text(datetime.now(UTC) - newest)
                self._freshness.update(f"data {label}")
                self._dot.set_state(state)
            else:
                self._freshness.update("data none")
                self._dot.set_state("error")
            self._model.update(str(getattr(self.rig.cfg, "llm_provider", "") or "—"))
            total = sum(row.cost_usd for row in services.llm_costs(self.rig.engine))
            self._spend.update(f"${total:.2f}")
        except Exception:
            self._freshness.update("data ?")
            self._dot.set_state("warn")


class ScreenFooter(Vertical):
    """The docked chrome row.

    A container rather than docking ``StatusBar`` directly: it keeps the dock
    rule in one place, so a screen never has to know how the shell is pinned.
    """

    DEFAULT_CSS = """
    /* Two rows, the top one padding: the bar needs air under the pane's
       bottom border, or its key hints and the nav row read as one strip. */
    ScreenFooter {
        dock: bottom;
        height: 2;
        padding-top: 1;
    }
    """

    def __init__(self, rig: Any, *, active: str = "") -> None:
        super().__init__()
        self.rig = rig
        self._active = active

    def compose(self) -> ComposeResult:
        yield StatusBar(self.rig, active=self._active)


class RiggerScreen(Screen):
    """Base screen: pane content above the docked status bar.

    Screens implement ``compose_content`` and keep their ``name`` class
    attribute. Content is *not* wrapped in a scroller — a screen that needs
    scrolling puts a ``VerticalScroll`` inside its own ``Pane``, so there is
    never more than one scroll region. ``on_screen_resume`` re-runs
    ``refresh_view`` when defined, fixing data frozen at launch (screens are
    constructed eagerly).
    """

    DEFAULT_CSS = """
    RiggerScreen {
        layout: vertical;
        padding: 0 1;
    }
    """

    def __init__(self, rig: Any) -> None:
        super().__init__()
        self.rig = rig

    def compose(self) -> ComposeResult:
        yield from self.compose_content()
        yield ScreenFooter(self.rig, active=self.name or "")

    def compose_content(self) -> ComposeResult:
        raise NotImplementedError
        yield  # pragma: no cover

    def set_context(self, text: str) -> None:
        """Update the status bar's context cell, if it is mounted."""
        try:
            self.query_one(StatusBar).set_context(text)
        except Exception:
            pass

    def on_nav_key_selected(self, event: NavKey.Selected) -> None:
        switch = getattr(self.app, "action_switch_screen", None)
        if callable(switch):
            switch(event.screen_name)

    async def on_screen_resume(self) -> None:
        self.query_one(StatusBar).set_active(self.name or "")
        refresh = getattr(self, "refresh_view", None)
        if callable(refresh):
            result = refresh()
            if inspect.isawaitable(result):
                await result
