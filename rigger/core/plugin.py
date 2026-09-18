"""Plugin base classes, registry, and entry-point discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

from rigger.core.models import Bar, Event, Fundamental, Instrument, NewsItem

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class Scope:
    """Declarative filter over watch targets.

    Four axes — ``targets``, ``asset_classes``, ``markets``, ``tags`` — are
    ANDed together; within a single axis the values are ORed. A ``None`` axis
    means "no restriction", so the default ``Scope()`` matches everything.
    """

    targets: frozenset[str] | None = None
    asset_classes: frozenset[str] | None = None
    markets: frozenset[str] | None = None
    tags: frozenset[str] | None = None

    def matches(self, inst: Instrument) -> bool:
        if self.targets is not None and not (self.targets & set(inst.watchlists)):
            return False
        if self.asset_classes is not None and inst.asset_class not in self.asset_classes:
            return False
        if self.markets is not None and inst.market not in self.markets:
            return False
        if self.tags is not None and not (self.tags & inst.tags):
            return False
        return True

    def filter(self, instruments: list[Instrument]) -> list[Instrument]:
        return [inst for inst in instruments if self.matches(inst)]


@dataclass(frozen=True)
class DataProviderField:
    """One setting an adapter safely exposes to the source-setup UI."""

    name: str
    label: str
    required: bool = False
    secret: bool = False
    env_var: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class DataProviderSpec:
    """An adapter-owned setup contract, not a generic HTTP connector."""

    label: str
    fields: tuple[DataProviderField, ...] = ()
    primary_disclosure: bool = False
    notice: str = ""


def _freeze(values: Any) -> frozenset[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    return frozenset(str(v) for v in values) or None


def parse_scope(raw: Any, *, market: str | None = None) -> Scope:
    """Build a Scope from a ``scope`` table, folding ``DataPlugin.market`` in.

    The ``targets`` axis also answers to its legacy ``watchlists`` key.
    """
    if isinstance(raw, str):
        raw = {"markets": raw}
    table = {} if not isinstance(raw, dict) else raw
    markets: Any = table.get("markets")
    if markets is None and market:
        markets = [market]
    if markets is not None:
        markets = frozenset(
            str(v).lower() for v in (markets if isinstance(markets, (list, tuple)) else [markets])
        )
    raw_targets = table.get("targets")
    if raw_targets is None:
        raw_targets = table.get("watchlists")
    return Scope(
        targets=_freeze(raw_targets),
        asset_classes=_freeze(table.get("asset_classes")),
        markets=markets or None,
        tags=_freeze(table.get("tags")),
    )


class Plugin:
    name: str  # unique, snake_case
    version: str = "0.1.0"
    enabled: bool = True
    shared_config: str | None = None

    def configure(self, cfg: dict[str, Any]) -> None:
        """Receives its [plugins.<name>] TOML table."""


class TargetPlugin(Plugin):
    """A watch target: anything the user follows, not just a share."""

    kind: str = ""
    name: str = ""
    label: str = ""

    def instruments(self) -> list[Instrument]:
        raise NotImplementedError


class MarketPlugin(Plugin):
    currency: str

    def universe(self) -> list[Instrument]:
        raise NotImplementedError

    def is_open(self, ts: datetime) -> bool:
        raise NotImplementedError

    def next_open(self, ts: datetime) -> datetime:
        raise NotImplementedError


class DataPlugin(Plugin):
    market: str | None = None  # None = works for any market
    scope: Scope = Scope()
    provider_spec: DataProviderSpec | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        configure = cls.__dict__.get("configure")
        if configure is not None:

            def configured(self: DataPlugin, cfg: dict[str, Any]) -> None:
                DataPlugin.configure(self, cfg)
                configure(self, cfg)

            cls.configure = configured  # type: ignore[method-assign]

    def configure(self, cfg: dict[str, Any]) -> None:
        self.scope = parse_scope(cfg.get("scope"), market=self.market)

    def universe(self, ctx: Context) -> list[Instrument]:
        return self.scope.filter(ctx.universe)

    async def fetch(
        self, instruments: list[Instrument], since: datetime
    ) -> list[Bar | NewsItem | Fundamental | Event]:
        raise NotImplementedError


@dataclass
class Context:
    """Read access to db, llm client, config, and the current instrument universe."""

    engine: Engine
    settings: Any
    config: Any
    llm: Any = None
    universe: list[Instrument] = field(default_factory=list)
    plugins: dict[str, Plugin] = field(default_factory=dict)


PLUGIN_GROUP = "rigger.plugins"
TARGET_GROUP = "rigger.targets"
LEGACY_WATCHLIST_GROUP = "rigger.watchlists"


def discover_plugins() -> dict[str, Plugin]:
    """Load all registered plugins from entry points, keyed by name."""
    discovered: dict[str, Plugin] = {}
    eps = entry_points()
    group = eps.select(group=PLUGIN_GROUP)
    for ep in group:
        cls = ep.load()
        instance = cls()
        discovered[instance.name] = instance
    return discovered


def discover_targets() -> dict[str, type[TargetPlugin]]:
    """Load target kinds from the ``rigger.targets`` group, keyed by kind.

    The legacy ``rigger.watchlists`` group is still honoured so third-party
    watchlist plugins keep working. Returns the class (a factory), not an
    instance, because one kind backs many differently-named targets.
    """
    kinds: dict[str, type[TargetPlugin]] = {}
    eps = entry_points()
    for group_name in (LEGACY_WATCHLIST_GROUP, TARGET_GROUP):
        for ep in eps.select(group=group_name):
            cls = ep.load()
            kinds[cls.kind] = cls
    return kinds


def apply_config(plugins: dict[str, Plugin], plugin_cfg: dict[str, dict[str, Any]]) -> None:
    for name, plugin in plugins.items():
        table = dict(plugin_cfg.get(name, {}))
        if "enabled" in table:
            plugin.enabled = bool(table["enabled"])
        shared = plugin.shared_config
        if shared and shared != name:
            defaults = {k: v for k, v in plugin_cfg.get(shared, {}).items() if k != "enabled"}
            table = {**defaults, **table}
        plugin.configure(table)
