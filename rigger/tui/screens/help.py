"""Help screen: a getting-started tutorial plus a keymap generated from BINDINGS."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Markdown, Static, TabbedContent, TabPane

from rigger.tui.shell import ALL_ITEMS
from rigger.tui.widgets import (
    MODAL_WIDTH_WIDE,
    Dialog,
    KeyGrid,
    binding_key,
    hint_markup,
    shown_bindings,
)


def _own_bindings(screen_type: type) -> list:
    """The nearest class in ``screen_type``'s MRO that declares its own BINDINGS.

    Not ``vars(screen_type)``: ``Data`` and ``Reports`` are thin subclasses of
    ``Research`` that only set ``name`` and ``initial_tab``, so reading the
    class dict alone left the whole research keymap out of this list. Stops at
    the Textual base classes, whose bindings are the stock focus keys and are
    not news to anyone.
    """
    for klass in screen_type.__mro__:
        if klass.__module__.startswith("textual."):
            break
        own = vars(klass).get("BINDINGS")
        if own:
            return list(own)
    return []


# Plain-language tour. Kept as a module constant so the README can
# reuse the exact same words the TUI shows.
TUTORIAL = """\
# Welcome to Rigger

Rigger reads for you. It collects facts about the companies you care about,
keeps them in one place, and writes summaries where **every claim points back
to the fact it came from**.

It does not trade, it does not size positions, and it will not tell you what
to buy or sell. The point is clarity, not tips.

---

## 1. Tell it what you care about

Press 1 for Watchlist.

A watchlist entry is anything you want watched — one company, a whole
sector, or a theme. Press a to open the entry form, fill in the fields,
and press enter to save. Press esc to close the form.

Use / to filter, up/down to select, and enter to refresh metrics for
the selected target. The metrics inspector stays open beside the watchlist.
Press space on an asset-class header to expand or collapse its targets, and
press r to cycle the chart range. Press d to remove a selected target.

Prices stream from Yahoo while Watchlist is open. **DAY %** is Yahoo’s
daily percentage change: **▲** up, **▼** down, **─** unchanged. Quote age
and connection state are separate; delivery may vary by market. Missing
quotes show **—**. Streaming quotes do not replace gathered historical bars.

| Field | Example |
| --- | --- |
| name | `iron-ore` |
| kind | `industry` |
| market | `asx` |
| tickers | `BHP,RIO,FMG` |

## 2. Let it go collect

Open the command palette (ctrl+p) and run **Gather evidence**.

`ingest` pulls prices, news, filings and earnings dates into a local database,
and `extract` turns the news into structured facts. Gather runs both.

Press 2 for Research. Choose a watch target and company, then search and
filter its evidence. Database counts and model spend are under Settings → Diagnostics.

## 3. Read what it found

Press 3 for the Research report view.

Pick a company, press n to generate a report, and wait. You get a written summary with a
citation on every claim. If a sentence could not be traced back to something
collected in step 2, it gets dropped rather than guessed.

The contents panel on the left jumps between sections. In Evidence, press o to
open a source and v to view it in the report; switch back to Report to resume reading.

## 4. Ask it questions

Press 5 for Ask.

Tick the watchlist entries you want in scope, then type a question. Answers come only
from the evidence collected in step 2 — not from the model's own memory.

Web search is a toggle, it is **off by default**, and anything it returns is
used once and never saved.

## 5. Track an idea over time

Press 4 for Theses.

Write down something you believe — *"iron ore volumes hold up through 2027"* —
and Rigger proposes evidence for and against it as new facts arrive.

You accept or reject each piece yourself. The model only ever *suggests*; it
never decides what counts, and the health badge is calculated from what you
accepted, not from an opinion.

## 6. Keep a decision journal

Press **6** for Decisions. Record your rationale, valuation or price context,
time horizon, review date, and what would invalidate the idea. Later reviews
are appended to the original entry, so you can see what you knew at the time.
This is a journal for your process, not a buy or sell recommendation.

---

## Worth knowing

- **Everything is cited.** You can always check the AI's working.
- **Nothing is a recommendation.** No buy, sell or position sizing, by design.
- **Your data stays local.** Evidence lives in a SQLite file in the project.
- Press m to change which model is used on the current screen.
- Press f2 to switch between the dark and light palettes.

Press ? or esc to close this help. Press k for the full keymap.
"""


class HelpScreen(Dialog):
    """The tutorial and the whole keymap, as a ``Dialog`` like every other modal."""

    name = "help"

    BINDINGS = [
        # A modal screen stops keys reaching the app, so ``?`` has to close the
        # help itself rather than falling through to the app's toggle.
        Binding("question_mark", "dismiss_dialog", "Close help", show=False),
        Binding("t", "show_tab('help-tour')", "Getting started"),
        Binding("k", "show_tab('help-keys')", "Keys"),
    ]

    dialog_title = "rigger help"
    dialog_hint = hint_markup(("t", "tour"), ("k", "keys"), ("esc", "close"))
    dialog_width = MODAL_WIDTH_WIDE

    DEFAULT_CSS = """
    /* The tutorial is long: the frame takes a fixed share of the screen and
       each tab scrolls inside it, rather than growing to fit and being
       clipped by the auto-height cap. */
    HelpScreen > #dialog-frame {
        height: 90%;
    }
    HelpScreen #help-tabs {
        height: 1fr;
    }
    HelpScreen Markdown {
        background: transparent;
    }
    HelpScreen .help-group {
        height: 1;
        margin: 1 0 0 0;
        color: $text-primary;
        text-style: bold;
    }
    """

    def compose_dialog(self) -> ComposeResult:
        with TabbedContent(id="help-tabs"):
            with TabPane("Getting started", id="help-tour"):
                yield Markdown(TUTORIAL)
            with TabPane("Keys", id="help-keys"):
                yield VerticalScroll(*self._key_widgets(), id="help-keymap")

    def action_show_tab(self, tab: str) -> None:
        self.query_one("#help-tabs", TabbedContent).active = tab

    def _key_widgets(self) -> list[Widget]:
        """The whole keymap: app keys, then every installed screen's own keys.

        Generated from the live ``BINDINGS``, so it cannot drift from what the
        app binds. Per-screen keys are the bulk of the app and used to be
        invisible here, because only ``app.BINDINGS`` was listed.
        """
        widgets: list[Widget] = []
        seen: list[list[tuple[str, str]]] = []
        for title, bindings in self._binding_groups():
            # Description only: a KeyGrid column is ~28 columns wide, and
            # appending the tooltip truncated every entry mid-word.
            items = [
                (binding_key(binding), binding.description) for binding in shown_bindings(bindings)
            ]
            # Evidence and Report are two views of one screen class, so their
            # keymaps are the same list: print it once, under the first.
            if not items or items in seen:
                continue
            seen.append(items)
            widgets.append(Static(title, classes="help-group", markup=False))
            widgets.append(KeyGrid(items))
        return widgets

    def _binding_groups(self) -> list[tuple[str, list]]:
        """``(group title, BINDINGS)`` for the app and each installed screen."""
        groups: list[tuple[str, list]] = [("Anywhere", list(self.app.BINDINGS))]
        labels = {name: label for _key, name, label in ALL_ITEMS}
        # ``screens_by_name`` is the app's public registry; a HelpScreen mounted
        # under a bare App (as the tests do) simply has no screens to list.
        screens = getattr(self.app, "screens_by_name", {}) or {}
        for name, screen in sorted(screens.items()):
            groups.append((labels.get(name, name.capitalize()), _own_bindings(type(screen))))
        return groups
