"""Runtime wiring shared by the services and TUI."""

from __future__ import annotations

from rigger.core import config as config_mod
from rigger.core.db import init_engine
from rigger.core.models import Instrument
from rigger.core.plugin import (
    Context,
    MarketPlugin,
    TargetPlugin,
    apply_config,
    discover_plugins,
    discover_targets,
)
from rigger.llm.client import LLMClient, build_client
from rigger.llm.providers import PROVIDERS
from rigger.plugins.data.yfinance import YFinanceSymbols
from rigger.plugins.targets.tickers import (
    CompanyTarget,
    IndustryTarget,
    LegacyTickersTarget,
    MarketTarget,
    SectorTarget,
    ThemeTarget,
)
from rigger.targets import LEGACY_KIND

_BUILTIN_KINDS: tuple[type[TargetPlugin], ...] = (
    CompanyTarget,
    SectorTarget,
    IndustryTarget,
    ThemeTarget,
    MarketTarget,
    LegacyTickersTarget,
)


class Rigger:
    def __init__(self) -> None:
        self.settings, self.cfg = config_mod.load_config()
        self.engine = init_engine(self.cfg.db_path)

        self.plugins = discover_plugins()
        apply_config(self.plugins, self.cfg.plugins)
        self._apply_market_profiles()

        for market_name, tickers in self.cfg.universe.items():
            market_plugin = self.plugins.get(market_name)
            if isinstance(market_plugin, MarketPlugin):
                table = dict(self.cfg.plugins.get(market_name, {}))
                table["tickers"] = list(tickers)
                market_plugin.configure(table)

        self.targets = self._build_targets()

        self.llm = self._build_llm()

    def _build_llm(self) -> LLMClient:
        """Compose the provider credentials map and build the LLM client.

        Fixed providers read their Settings fields; the custom provider reads
        its ``[llm] api_key_env`` variable straight from .env so any variable
        name works without a Settings field.
        """
        keys = {
            "OPENROUTER_API_KEY": self.settings.openrouter_api_key,
            "OPENAI_API_KEY": self.settings.openai_api_key,
            "ANTHROPIC_API_KEY": self.settings.anthropic_api_key,
        }
        custom_env = self.cfg.llm_api_key_env or "CUSTOM_API_KEY"
        keys[custom_env] = config_mod.read_env_value(custom_env)
        provider = self.cfg.llm_provider
        if provider not in PROVIDERS:
            # Legacy configs may still name a removed provider (e.g. "litellm");
            # fall back to the default so the app starts and the Config screen's
            # unknown-provider check can guide the fix.
            provider = "openrouter"
        return build_client(
            provider=provider,
            engine=self.engine,
            api_keys=keys,
            max_output_tokens=self.cfg.llm_max_output_tokens,
            custom_base_url=self.cfg.llm_base_url,
            custom_api_key_env=self.cfg.llm_api_key_env,
        )

    def reload_llm(self) -> None:
        """Re-read .env + config.toml and rebuild the LLM client in place.

        Provider, keys, routes, and token caps hot-swap without a restart;
        the plugin and target registries are untouched.
        """
        self.settings, self.cfg = config_mod.load_config()
        self.llm = self._build_llm()

    def reload_data_sources(self) -> None:
        """Re-read source configuration without disturbing the LLM client."""
        self.settings, self.cfg = config_mod.load_config()
        apply_config(self.plugins, self.cfg.plugins)
        self._apply_market_profiles()

    def reload_markets(self) -> None:
        """Re-read exchange definitions and rebuild targets using their currency profiles."""
        self.settings, self.cfg = config_mod.load_config()
        apply_config(self.plugins, self.cfg.plugins)
        self._apply_market_profiles()
        self.targets = self._build_targets()

    def _apply_market_profiles(self) -> None:
        suffixes = {name: profile.yahoo_suffix for name, profile in self.cfg.markets.items()}
        for plugin in self.plugins.values():
            if isinstance(plugin, YFinanceSymbols):
                plugin.set_market_suffixes(suffixes)

    def universe(self) -> list[Instrument]:
        merged: dict[str, Instrument] = {}
        market_defaults = {
            inst.id: inst
            for plugin in self.plugins.values()
            if plugin.enabled and isinstance(plugin, MarketPlugin)
            for inst in plugin.universe()
        }
        for target in self.targets.values():
            for instrument in target.instruments():
                default = market_defaults.get(instrument.id)
                if default is not None:
                    instrument = instrument.model_copy(
                        update={
                            "name": default.name or instrument.name,
                            "sector": instrument.sector or default.sector,
                            "currency": instrument.currency or default.currency,
                        }
                    )
                existing = merged.get(instrument.id)
                if existing is None:
                    merged[instrument.id] = instrument
                    continue
                merged[instrument.id] = existing.model_copy(
                    update={
                        "watchlists": tuple(
                            dict.fromkeys(existing.watchlists + instrument.watchlists)
                        ),
                        "tags": existing.tags | instrument.tags,
                        "sector": existing.sector or instrument.sector,
                        "asset_class": (
                            instrument.asset_class
                            if instrument.asset_class != "equity"
                            else existing.asset_class
                        ),
                    }
                )
        return list(merged.values())

    def _build_targets(self) -> dict[str, TargetPlugin]:
        kinds = discover_targets()
        for cls in _BUILTIN_KINDS:
            kinds.setdefault(cls.kind, cls)

        targets: dict[str, TargetPlugin] = {}
        for name, spec in self.cfg.targets.items():
            kind_name = spec.get("kind", LEGACY_KIND)
            kind_cls = kinds.get(kind_name)
            if kind_cls is None:
                available = ", ".join(sorted(kinds))
                raise KeyError(
                    f"target {name!r} names unknown kind {kind_name!r}; known kinds are {available}"
                )
            instance = kind_cls()
            market_name = str(spec.get("market", "")).lower()
            profile = self.cfg.markets.get(market_name)
            instance.configure(
                {
                    "name": name,
                    "label": spec.get("label", name),
                    "market_currency": profile.currency if profile else "",
                    **spec,
                }
            )
            market = getattr(instance, "market", None)
            if market is not None and market not in self.known_markets():
                known = ", ".join(self.known_markets()) or "none"
                raise KeyError(
                    f"target {name!r} names market {market!r}; known markets are {known}"
                )
            targets[name] = instance

        return targets

    def known_markets(self) -> list[str]:
        plugin_markets = {name for name, plugin in self.plugins.items() if isinstance(plugin, MarketPlugin)}
        return sorted(plugin_markets | set(self.cfg.markets))

    def context(self, universe: list[Instrument] | None = None) -> Context:
        return Context(
            engine=self.engine,
            settings=self.settings,
            config=self.cfg,
            llm=self.llm,
            universe=self.universe() if universe is None else universe,
            plugins=self.plugins,
        )


__all__ = ["Rigger"]
