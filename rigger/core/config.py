"""Configuration: pydantic-settings (.env) + TOML loader (config.toml)."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from rigger.targets import DEFAULT_KIND, LEGACY_KIND

CONFIG_PATH = Path("config.toml")
ENV_PATH = Path(".env")


class Settings(BaseSettings):
    """Secrets and environment overrides, never committed."""

    model_config = SettingsConfigDict(env_file=ENV_PATH, env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""


class MarketConfig(BaseModel):
    """One exchange a user can select for targets and Yahoo-backed data."""

    label: str
    currency: str
    yahoo_suffix: str = ""


DEFAULT_MARKETS = {
    "us": MarketConfig(label="United States", currency="USD"),
    "asx": MarketConfig(label="Australian Securities Exchange", currency="AUD", yahoo_suffix=".AX"),
}


class AppConfig(BaseModel):
    """The merged view of config.toml (loaded lazily)."""

    base_currency: str = "AUD"
    db_path: str = "data/rigger.db"
    reports_dir: str = "reports"

    universe: dict[str, list[str]] = Field(default_factory=dict)
    targets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    llm_provider: str = "openrouter"
    llm_routing: dict[str, str] = Field(default_factory=dict)
    llm_max_output_tokens: int = 4096
    llm_base_url: str = ""
    llm_api_key_env: str = ""
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)
    markets: dict[str, MarketConfig] = Field(default_factory=lambda: dict(DEFAULT_MARKETS))


def load_toml(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _target_tables(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge [targets] and legacy [watchlists] tables, then the [universe] shim.

    Each spec carries an explicit kind: ``company`` for bare [targets] tables,
    the legacy ``tickers`` kind for [watchlists] tables and universe shim
    entries. [targets] wins on name collisions; user entries beat shim names.
    """
    targets: dict[str, dict[str, Any]] = {}
    for name, spec in raw.get("targets", {}).items():
        targets[str(name)] = {"kind": DEFAULT_KIND, **dict(spec)}
    for name, spec in raw.get("watchlists", {}).items():
        targets.setdefault(str(name), {"kind": LEGACY_KIND, **dict(spec)})
    for market, tickers in raw.get("universe", {}).items():
        targets.setdefault(
            f"universe_{market}",
            {"kind": LEGACY_KIND, "market": market, "tickers": list(tickers), "legacy": True},
        )
    return targets


def build_config(raw: dict[str, Any] | None = None) -> AppConfig:
    raw = raw if raw is not None else load_toml()
    cfg = AppConfig()

    cfg.base_currency = raw.get("base_currency", cfg.base_currency)
    cfg.db_path = raw.get("db_path", cfg.db_path)
    cfg.reports_dir = raw.get("reports_dir", cfg.reports_dir)

    cfg.universe = raw.get("universe", {})
    cfg.targets = _target_tables(raw)

    llm = raw.get("llm", {})
    cfg.llm_provider = llm.get("provider", cfg.llm_provider)
    cfg.llm_routing = llm.get("routing", {})
    cfg.llm_max_output_tokens = llm.get("max_output_tokens", cfg.llm_max_output_tokens)
    cfg.llm_base_url = llm.get("base_url", cfg.llm_base_url)
    cfg.llm_api_key_env = llm.get("api_key_env", cfg.llm_api_key_env)

    cfg.plugins = raw.get("plugins", {})
    configured_markets = raw.get("markets", {})
    cfg.markets = {
        **DEFAULT_MARKETS,
        **{
            str(name).lower(): MarketConfig.model_validate(values)
            for name, values in configured_markets.items()
        },
    }
    return cfg


def load_config(path: Path = CONFIG_PATH) -> tuple[Settings, AppConfig]:
    settings = Settings()
    return settings, build_config(load_toml(path))


def read_env_value(name: str) -> str:
    """Value of ``name`` from the environment or .env; '' when unset.

    The process environment wins over .env, matching :class:`Settings`'
    pydantic-settings precedence, so an exported shell variable can never
    shadow a key saved from the app. Unlike :class:`Settings` this reads
    arbitrary variable names, so the ``custom`` provider's
    ``[llm] api_key_env`` works without a Settings field. Never raises.
    """
    if not name:
        return ""
    value = os.environ.get(name, "")
    if value:
        return value
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def set_env_value(name: str, value: str) -> None:
    """Write ``name=value`` to .env, replacing an existing line or appending.

    Unrelated lines (and their order) are preserved; the file is created when
    absent. Secrets stay in .env (gitignored), never in config.toml.
    """
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
