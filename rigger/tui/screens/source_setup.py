"""Setup dialog for data plugins that declare a :class:`DataProviderSpec`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from rigger.core.plugin import DataProviderSpec


class SourceSetupModal(ModalScreen[tuple[dict[str, str], list[str]] | None]):
    """Collect only the explicitly declared non-secret settings for a source."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    SourceSetupModal > Vertical { width: 68; }
    SourceSetupModal Input { margin: 1 0 0 0; }
    """

    def __init__(self, spec: DataProviderSpec, current: dict[str, Any]) -> None:
        super().__init__()
        self.spec = spec
        self.current = current

    def compose(self) -> ComposeResult:
        scope = self.current.get("scope", {})
        markets = ",".join(scope.get("markets", [])) if isinstance(scope, dict) else ""
        yield Vertical(
            Static(f"[bold]Configure {self.spec.label}[/bold]", markup=True),
            Static(self.spec.notice, markup=False) if self.spec.notice else Static("", markup=False),
            *(
                Input(
                    value=str(self.current.get(field.name, "")),
                    placeholder=field.placeholder or field.label,
                    password=field.secret,
                    id=f"source-{field.name}",
                )
                for field in self.spec.fields
            ),
            Input(value=markets, placeholder="Markets to attach, e.g. lse,asx (optional)", id="source-markets"),
            Horizontal(Button("Save", id="source-save"), Button("Cancel", id="source-cancel")),
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id and event.input.id.startswith("source-"):
            self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "source-save":
            self._save()
        else:
            self.action_cancel()

    def _save(self) -> None:
        values = {
            field.name: self.query_one(f"#source-{field.name}", Input).value
            for field in self.spec.fields
        }
        missing = [field.label for field in self.spec.fields if field.required and not values[field.name].strip()]
        if missing:
            self.notify(f"required: {', '.join(missing)}", severity="warning")
            return
        markets = [
            market.strip().lower()
            for market in self.query_one("#source-markets", Input).value.split(",")
            if market.strip()
        ]
        self.dismiss((values, markets))

    def action_cancel(self) -> None:
        self.dismiss(None)


async def configure_source(app: Any, rig: Any, name: str, refresh: Callable[[], Any]) -> None:
    """Show one adapter's setup form, save it, then refresh Settings."""
    from rigger import services

    plugin = rig.plugins.get(name)
    spec = getattr(plugin, "provider_spec", None)
    if spec is None:
        app.notify(f"{name} has no setup form", severity="warning")
        return
    current = dict(getattr(rig.cfg, "plugins", {}).get(name, {}))
    result = await app.push_screen_wait(SourceSetupModal(spec, current))
    if result is None:
        return
    values, markets = result
    try:
        services.configure_data_provider(rig, name, values, markets=markets)
    except ValueError as exc:
        app.notify(str(exc), severity="error")
        return
    result = refresh()
    if hasattr(result, "__await__"):
        await result
    app.notify(f"{spec.label} configured")
