"""Settings dialog for config-backed exchange markets."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class MarketSetupModal(ModalScreen[dict[str, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    MarketSetupModal > Vertical { width: 64; }
    MarketSetupModal Input { margin: 1 0 0 0; }
    """

    def __init__(self, current: dict[str, str] | None = None, *, editable_id: bool = True) -> None:
        super().__init__()
        self.current = current or {}
        self.editable_id = editable_id

    def compose(self) -> ComposeResult:
        title = "Add market" if self.editable_id else "Edit market"
        yield Vertical(
            Static(f"[bold]{title}[/bold]", markup=True),
            Static("Yahoo and RSS work automatically; country-specific sources stay opt-in.", markup=False),
            Input(value=self.current.get("id", ""), placeholder="ID, e.g. lse", id="market-id", disabled=not self.editable_id),
            Input(value=self.current.get("label", ""), placeholder="Exchange name", id="market-label"),
            Input(value=self.current.get("currency", ""), placeholder="Currency, e.g. GBP", id="market-currency"),
            Input(value=self.current.get("yahoo_suffix", ""), placeholder="Yahoo suffix, e.g. .L (optional)", id="market-suffix"),
            Horizontal(Button("Save", id="market-save"), Button("Cancel", id="market-cancel")),
        )

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "market-save":
            self._save()
        else:
            self.dismiss(None)

    def _save(self) -> None:
        values = {
            "id": self.query_one("#market-id", Input).value,
            "label": self.query_one("#market-label", Input).value,
            "currency": self.query_one("#market-currency", Input).value,
            "yahoo_suffix": self.query_one("#market-suffix", Input).value,
        }
        if not all(values[key].strip() for key in ("id", "label", "currency")):
            self.notify("ID, exchange name, and currency are required", severity="warning")
            return
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)
