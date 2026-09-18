"""Textual App: screen registry, key bindings, service wiring."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider

from rigger import services
from rigger.core import state
from rigger.llm.catalog import ModelInfo, set_llm_route
from rigger.runtime import Rigger
from rigger.tui.screens.chat import Chat
from rigger.tui.screens.config import Config
from rigger.tui.screens.data import Data
from rigger.tui.screens.decisions import Decisions
from rigger.tui.screens.help import HelpScreen
from rigger.tui.screens.home import Home
from rigger.tui.screens.model_picker import ModelPicker
from rigger.tui.screens.reports import Reports
from rigger.tui.screens.research import ResearchState
from rigger.tui.screens.targets import Targets
from rigger.tui.screens.theses import Theses
from rigger.tui.shell import ALL_ITEMS
from rigger.tui.theme import THEMES
from rigger.tui.widgets import MODAL_WIDTH, Dialog, KeyGrid, PaneRow, hint_markup


async def _gather(rig: Any, app: App) -> None:
    """Command-palette action: ingest then extract, with a toast result."""
    ingested = await services.ingest(rig)
    extracted = await services.extract(rig)
    rows = sum(ingested.counts.values())
    app.notify(f"Gathered {rows} rows, {extracted.events} events")


class RiggerCommands(Provider):
    """Command palette: jump to any screen, gather evidence, flip theme."""

    def __init__(self, screen: Any, match_style: Any = None) -> None:
        super().__init__(screen, match_style)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = self.app
        rig = getattr(self.screen, "rig", None)
        for key, name, label in ALL_ITEMS:
            text = f"Go to {label}"
            score = matcher.match(text)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(text),
                    lambda n=name: app.action_switch_screen(n),
                    text,
                    f"shortcut: {key}",
                )
        text = "Gather evidence"
        score = matcher.match(text)
        if score > 0 and rig is not None:

            async def gather() -> None:
                await _gather(rig, app)

            yield Hit(score, matcher.highlight(text), gather, text, "ingest + extract")
        text = "Toggle light/dark theme"
        score = matcher.match(text)
        if score > 0:
            yield Hit(
                score,
                matcher.highlight(text),
                lambda: app.action_toggle_theme(),
                text,
            )


class GoPicker(Dialog):
    """Centred keymap of every panel: the replacement for the nav rail."""

    dialog_title = "go"
    dialog_hint = hint_markup(("key", "open"), ("esc", "exit"))
    dialog_width = MODAL_WIDTH

    def compose_dialog(self) -> ComposeResult:
        yield KeyGrid([(key, label) for key, _name, label in ALL_ITEMS])

    def on_key(self, event: Any) -> None:
        for key, name, _label in ALL_ITEMS:
            if event.key == key:
                event.stop()
                self.dismiss(None)
                self.app.action_switch_screen(name)
                return


class RiggerApp(App):
    TITLE = "Rigger"
    CSS_PATH = "rigger.tcss"
    BINDINGS = [
        Binding("1", "switch_screen('targets')", "Watchlist", tooltip="Manage what is watched"),
        Binding(
            "2", "switch_screen('data')", "Research · Evidence", tooltip="Browse the evidence pool"
        ),
        Binding(
            "3",
            "switch_screen('reports')",
            "Research · Report",
            tooltip="Generate and read company reports",
        ),
        Binding("4", "switch_screen('theses')", "Theses", tooltip="Track claims and evidence"),
        Binding("5", "switch_screen('chat')", "Ask", tooltip="Grounded Q&A over evidence"),
        Binding("6", "switch_screen('decisions')", "Decisions", tooltip="Record and review decision context"),
        Binding(
            "c",
            "switch_screen('config')",
            "Settings",
            tooltip="Providers, routing, plugins and diagnostics",
        ),
        Binding("h", "switch_screen('home')", "Home", tooltip="The landing dashboard"),
        Binding("m", "show_model_picker", "Model", tooltip="Pick the model for this screen"),
        Binding(
            "p", "show_provider_picker", "Provider", tooltip="Connect or switch the AI provider"
        ),
        Binding("g", "show_go", "Go", tooltip="Jump to a panel"),
        Binding("question_mark", "show_help", "Help", tooltip="Show the keymap"),
        Binding("q", "quit", "Quit", tooltip="Leave Rigger"),
        Binding("f2", "toggle_theme", "Theme", tooltip="Switch light/dark palette"),
    ]
    COMMANDS = App.COMMANDS | {RiggerCommands}

    def __init__(self, rig: Rigger | None = None) -> None:
        super().__init__()
        # Register before the first stylesheet parse so the app never paints
        # a frame in Textual's stock theme.
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = "rigger-dark"
        self.rig = rig if rig is not None else Rigger()
        #: Public registry of the installed screens. Screens that keep each
        #: other in sync read this rather than Textual's private ``_screens``.
        self.screens_by_name: dict[str, Any] = {}
        self._screens: dict[str, Any] = {}
        self.services = services
        self.log_lines: list[str] = []
        self.narrow = False
        # Read once, then stamp this visit immediately: Home renders the whole
        # session against the *previous* value, so re-reading would zero it out.
        self.last_seen = state.read_last_seen(self.rig.cfg)
        state.write_last_seen(self.rig.cfg)

    def on_mount(self) -> None:
        research_state = ResearchState()
        self._screens = {
            "home": Home(self.rig, last_seen=self.last_seen),
            "data": Data(self.rig, research_state),
            "config": Config(self.rig),
            "reports": Reports(self.rig, research_state),
            "theses": Theses(self.rig),
            "decisions": Decisions(self.rig, research_state),
            "chat": Chat(self.rig),
            "targets": Targets(self.rig),
        }
        self.screens_by_name = dict(self._screens)
        for screen in self._screens.values():
            self.install_screen(screen, screen.name)
        self.push_screen("home")

    def on_resize(self, event: Any) -> None:
        """Track the narrow breakpoint; each PaneRow stacks itself on resize."""
        self.narrow = event.size.width < PaneRow.NARROW_WIDTH

    def action_switch_screen(self, name: str) -> None:
        if name in ("data", "reports"):
            self._screens[name].tab = "evidence" if name == "data" else "report"
        self.switch_screen(name)

    def action_toggle_theme(self) -> None:
        self.theme = "rigger-light" if self.theme == "rigger-dark" else "rigger-dark"
        self.notify(f"Theme: {self.theme}")

    def action_show_go(self) -> None:
        self.push_screen(GoPicker())

    def action_show_help(self) -> None:
        if self.screen.name == "help":
            self.pop_screen()
        else:
            self.push_screen(HelpScreen())

    def action_show_model_picker(self, task: str | None = None) -> None:
        """Pick the model for a routing task.

        With no ``task`` the task is derived from the current screen, which is
        what the ``m`` binding wants. Settings passes the task explicitly, so
        it can re-route any row without re-implementing this action.
        """
        task = task or {
            "data": "report",
            "reports": "report",
            "chat": "chat",
            "theses": "thesis",
        }.get(self.screen.name or "", "extract")
        provider = getattr(getattr(self.rig, "llm", None), "provider", None)

        def on_select(model: ModelInfo) -> None:
            set_llm_route(task, model.id)
            self.notify(f"{task} route set to {model.id}")
            if isinstance(self.rig, Rigger):
                self.rig.reload_llm()

        self.push_screen(
            ModelPicker(on_select, provider=provider, provider_name=self.rig.cfg.llm_provider)
        )

    def action_show_provider_picker(self) -> None:
        from rigger.tui.screens.provider_picker import (
            ProviderPicker,
            connect_provider,
            provider_key_status,
        )

        def on_select(name: str) -> None:
            self.run_worker(connect_provider(self, self.rig, name), exclusive=True)

        self.push_screen(ProviderPicker(on_select, key_status=provider_key_status(self.rig)))


def run_tui(rig: Rigger | None = None) -> None:
    app = RiggerApp(rig)
    app.run()
