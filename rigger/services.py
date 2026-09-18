"""Pipeline services shared by the TUI."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from rigger.core.db import (
    BarTable,
    EventTable,
    FundamentalTable,
    LLMCallTable,
    NewsItemTable,
    store_items,
)
from rigger.core.json import from_json
from rigger.core.time import parse_date, to_utc
from rigger.evidence import FILING_SOURCE
from rigger.llm.providers import PROVIDERS, ProviderSpec
from rigger.targets import DEFAULT_KIND, KNOWN_KINDS, LEGACY_KIND, WatchTarget, target_from_spec
from rigger.theses import Thesis
from rigger.thesis_health import HealthResult

Log = Callable[[str], None]

#: Settings attribute serving each fixed provider's env var; custom reads .env.
_ENV_TO_ATTR = {
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
}


def _noop_log(_message: str) -> None:
    pass


@dataclass
class IngestResult:
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class ExtractResult:
    events: int
    instruments: int


@dataclass(frozen=True)
class DataProviderStatus:
    name: str
    label: str
    configured: bool
    enabled: bool
    primary_disclosure: bool
    notice: str


def data_provider_status(rig: Any) -> list[DataProviderStatus]:
    """Configured data plugins that declare a safe setup contract."""
    from rigger.core.config import read_env_value
    from rigger.core.plugin import DataPlugin

    result: list[DataProviderStatus] = []
    for name, plugin in rig.plugins.items():
        if not isinstance(plugin, DataPlugin) or plugin.provider_spec is None:
            continue
        spec = plugin.provider_spec
        table = dict(getattr(rig.cfg, "plugins", {}).get(name, {}))
        configured = all(
            not item.required
            or (
                bool(read_env_value(item.env_var))
                if item.secret and item.env_var
                else bool(table.get(item.name))
            )
            for item in spec.fields
        )
        result.append(
            DataProviderStatus(
                name=name,
                label=spec.label,
                configured=configured,
                enabled=bool(plugin.enabled),
                primary_disclosure=spec.primary_disclosure,
                notice=spec.notice,
            )
        )
    return sorted(result, key=lambda item: item.label)


def configure_data_provider(
    rig: Any, name: str, values: dict[str, str], *, markets: list[str] | None = None
) -> None:
    """Persist adapter settings and activate the source.

    A secret field must declare its fixed environment-variable name; it goes
    to `.env`, never to `config.toml`.
    """
    import tomli_w

    from rigger.core import config as config_mod
    from rigger.core.plugin import DataPlugin

    plugin = rig.plugins.get(name)
    if not isinstance(plugin, DataPlugin) or plugin.provider_spec is None:
        raise ValueError(f"unknown configurable data provider: {name}")
    fields = {field.name: field for field in plugin.provider_spec.fields}
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(f"unknown settings for {name}: {', '.join(sorted(unknown))}")
    for field_name, value in values.items():
        field = fields[field_name]
        if field.secret and value.strip() and not field.env_var:
            raise ValueError(f"{field.label} has no declared environment variable")
    raw = config_mod.load_toml()
    if markets is not None:
        known = set(market_profiles())
        unknown_markets = set(markets) - known
        if unknown_markets:
            raise ValueError(f"unknown markets: {', '.join(sorted(unknown_markets))}")
    table = raw.setdefault("plugins", {}).setdefault(name, {})
    for field_name, value in values.items():
        field = fields[field_name]
        value = value.strip()
        if field.secret:
            if field.required and not value:
                raise ValueError(f"{field.label} is required")
            continue
        if field.required and not value:
            raise ValueError(f"{field.label} is required")
        table[field_name] = value
    if markets is not None:
        table.setdefault("scope", {})["markets"] = markets
    table["enabled"] = True
    Path("config.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")
    for field_name, value in values.items():
        field = fields[field_name]
        if field.secret and value.strip():
            config_mod.set_env_value(field.env_var, value.strip())
    reload_sources = getattr(rig, "reload_data_sources", None)
    if callable(reload_sources):
        reload_sources()


async def ingest(
    rig: Any,
    *,
    market: str | None = None,
    tickers: str | None = None,
    instruments: Sequence[Any] | None = None,
    since: str | None = None,
    log: Log = _noop_log,
) -> IngestResult:
    since = since or (datetime.now(UTC) - timedelta(days=365)).strftime("%Y-%m-%d")
    instruments = list(instruments) if instruments is not None else rig.universe()
    if market:
        instruments = [i for i in instruments if i.market == market]
    if tickers:
        wanted = set(tickers.split(","))
        instruments = [i for i in instruments if i.symbol in wanted]
    total: dict[str, int] = {}
    from rigger.core.plugin import DataPlugin

    for name, plugin in rig.plugins.items():
        if not plugin.enabled or not hasattr(plugin, "fetch"):
            continue
        if plugin.market and market and plugin.market != market:
            continue
        target = [i for i in instruments if plugin.market is None or i.market == plugin.market]
        if isinstance(plugin, DataPlugin):
            target = plugin.scope.filter(target)
        if not target:
            continue
        log(f"Ingesting via [bold]{name}[/bold] ({len(target)} instruments)...")
        counts = store_items(rig.engine, await plugin.fetch(target, parse_date(since)))
        for table, count in counts.items():
            total[table] = total.get(table, 0) + count
        log("  stored " + ", ".join(f"{n} {t}" for t, n in counts.items()))
    return IngestResult(total)


async def extract(
    rig: Any,
    *,
    since: str | None = None,
    instruments: list[Any] | None = None,
    log: Log = _noop_log,
) -> ExtractResult:
    """Extract events from stored evidence.

    ``instruments`` narrows the run to a subset of the universe, so a caller
    refreshing one company does not pay for extraction across every target.
    """
    from rigger.extract import extract_events

    since = since or (datetime.now(UTC) - timedelta(days=14)).strftime("%Y-%m-%d")
    events = await extract_events(
        rig.context(rig.universe() if instruments is None else instruments), parse_date(since)
    )
    instruments = len({event.instrument_id for event in events})
    log(
        f"[green]Extracted {len(events)} events across {instruments} instruments since {since}.[/green]"
    )
    return ExtractResult(len(events), instruments)


def set_plugin_enabled(rig: Any, name: str, value: bool) -> None:
    import tomli_w

    from rigger.core import config as config_mod

    raw = config_mod.load_toml()
    raw.setdefault("plugins", {}).setdefault(name, {})["enabled"] = value
    Path("config.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")


_MARKET_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def market_profiles() -> dict[str, Any]:
    """All built-in and user-configured exchange profiles in the active TOML."""
    from rigger.core import config as config_mod

    return config_mod.build_config(config_mod.load_toml()).markets


def save_market(name: str, *, label: str, currency: str, yahoo_suffix: str) -> None:
    """Create or edit a config-backed exchange profile."""
    import tomli_w

    from rigger.core import config as config_mod

    name = name.strip().lower()
    currency = currency.strip().upper()
    if not _MARKET_ID.fullmatch(name):
        raise ValueError("market ID must use lowercase letters, numbers, or underscores")
    if not label.strip():
        raise ValueError("market name is required")
    if not _CURRENCY.fullmatch(currency):
        raise ValueError("currency must be a three-letter ISO code")
    raw = config_mod.load_toml()
    if name in {"us", "asx"} and name not in raw.get("markets", {}):
        raise ValueError(f"{name} is built in and cannot be edited")
    raw.setdefault("markets", {})[name] = {
        "label": label.strip(),
        "currency": currency,
        "yahoo_suffix": yahoo_suffix.strip().upper(),
    }
    Path("config.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")


def remove_market(name: str) -> None:
    """Remove a user-defined market when nothing still depends on it."""
    import tomli_w

    from rigger.core import config as config_mod

    name = name.strip().lower()
    raw = config_mod.load_toml()
    if name not in raw.get("markets", {}):
        raise ValueError("only user-defined markets can be removed")
    targets = {
        target_name
        for section in ("targets", "watchlists")
        for target_name, spec in raw.get(section, {}).items()
        if str(spec.get("market", "")).lower() == name
    }
    sources: set[str] = set()
    for source_name, spec in raw.get("plugins", {}).items():
        scope = spec.get("scope", {})
        values = scope.get("markets", []) if isinstance(scope, dict) else []
        if isinstance(values, str):
            values = [values]
        if name in {str(item).lower() for item in values}:
            sources.add(str(source_name))
    if targets or sources:
        used_by = sorted(targets | sources)
        raise ValueError(f"{name} is still used by: {', '.join(used_by)}")
    del raw["markets"][name]
    Path("config.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")


def target_specs() -> dict[str, WatchTarget]:
    """User-facing watch targets from [targets] and legacy [watchlists] tables.

    The [universe] shim entries stay hidden: only real tables are listed.
    """
    from rigger.core import config as config_mod

    raw = config_mod.load_toml()
    specs: dict[str, tuple[dict[str, Any], bool]] = {}
    for name, values in raw.get("targets", {}).items():
        specs[str(name)] = ({"kind": DEFAULT_KIND, **dict(values)}, False)
    for name, values in raw.get("watchlists", {}).items():
        specs.setdefault(str(name), ({"kind": LEGACY_KIND, **dict(values)}, True))
    return {
        name: target_from_spec(name, spec, legacy=legacy) for name, (spec, legacy) in specs.items()
    }


def add_target(
    name: str,
    *,
    kind: str = DEFAULT_KIND,
    market: str,
    tickers: list[str] | None = None,
    tags: list[str] | None = None,
    notes: str = "",
    label: str | None = None,
    asset_class: str = "equity",
) -> None:
    import tomli_w

    from rigger.core import config as config_mod

    kind = kind.lower()
    if kind not in KNOWN_KINDS:
        raise ValueError(
            f"target {name!r} names unknown kind {kind!r}; known kinds are {', '.join(KNOWN_KINDS)}"
        )
    known = sorted(market_profiles())
    market = market.lower()
    if market not in known:
        raise ValueError(
            f"target {name!r} names market {market!r}; known markets are {', '.join(known)}"
        )
    tickers = [t.upper() for t in (tickers or [])]
    if kind == "market":
        if tickers:
            raise ValueError(f"market target {name!r} takes no tickers")
    elif not tickers:
        raise ValueError(f"{kind} target {name!r} requires tickers")
    raw = config_mod.load_toml()
    if name in raw.get("targets", {}) or name in raw.get("watchlists", {}):
        raise ValueError(f"target {name!r} already exists")
    spec: dict[str, Any] = {"kind": kind, "market": market}
    if asset_class not in {"equity", "etf", "bond", "commodity", "fx", "crypto", "cash", "other"}:
        raise ValueError(f"unknown asset class {asset_class!r}")
    spec["asset_class"] = asset_class
    if kind != "market":
        spec["tickers"] = tickers
    if tags:
        spec["tags"] = list(tags)
    if notes:
        spec["notes"] = notes
    if label:
        spec["label"] = label
    raw.setdefault("targets", {})[name] = spec
    Path("config.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")


def remove_target(name: str) -> None:
    import tomli_w

    from rigger.core import config as config_mod

    raw = config_mod.load_toml()
    for section in ("targets", "watchlists"):
        if name in raw.get(section, {}):
            del raw[section][name]
            Path("config.toml").write_text(tomli_w.dumps(raw), encoding="utf-8")
            return
    raise KeyError(f"unknown target: {name}")


def brief_for(rig: Any, instrument_id: str) -> str | None:
    from rigger.brief import build_brief

    instrument = next((item for item in rig.universe() if item.id == instrument_id), None)
    if instrument is None:
        return None
    brief = build_brief(rig.context(rig.universe()), instrument)
    return brief.render() if brief else None


@dataclass
class DataHealth:
    counts: dict[str, int]
    latest_bar: dict[str, datetime]
    last_llm: datetime | None


def _count(session: Session, table: Any) -> int:
    from sqlmodel import func

    return int(session.exec(select(func.count()).select_from(table)).one())


def data_health(rig: Any) -> DataHealth:
    tables = {
        "bar": BarTable,
        "newsitem": NewsItemTable,
        "event": EventTable,
        "fundamental": FundamentalTable,
        "llmcall": LLMCallTable,
    }
    with Session(rig.engine) as session:
        counts = {name: _count(session, table) for name, table in tables.items()}
        latest: dict[str, datetime] = {}
        for instrument in rig.universe():
            row = session.exec(
                select(BarTable)
                .where(BarTable.instrument_id == instrument.id)
                .order_by(BarTable.ts.desc())  # type: ignore[attr-defined]
            ).first()
            if row:
                latest[instrument.id] = row.ts
        last_llm = session.exec(select(LLMCallTable.ts).order_by(LLMCallTable.ts.desc())).first()  # type: ignore[attr-defined]
    return DataHealth(counts, latest, last_llm)


def recent_closes(engine: Any, instrument_id: str, limit: int = 40) -> list[float]:
    """Most recent close prices, oldest first, for sparklines."""
    with Session(engine) as session:
        rows = session.exec(
            select(BarTable.close)
            .where(BarTable.instrument_id == instrument_id)
            .order_by(BarTable.ts.desc())  # type: ignore[attr-defined]
            .limit(limit)
        ).all()
    return [float(close) for close in reversed(rows)]


@dataclass(frozen=True)
class ReportStamp:
    """The newest report written for a company, and the moment it describes.

    ``as_of`` comes from the ``.json`` sidecar when there is one; without it the
    filename (``YYYY-MM-DD.md``) is the only date on offer, and a name that is
    not a date leaves ``as_of`` None rather than inventing an age.
    """

    path: Path
    as_of: datetime | None


def latest_report(reports_dir: str | Path, company: str) -> ReportStamp | None:
    """The newest report on disk for ``company``, or None when there is none.

    Read-side only: it walks ``<reports_dir>/<company>/*.md`` and never writes.
    Lives here so every screen answers "how old is this company's report?" the
    same way instead of each growing its own copy.
    """
    from rigger.reports import read_report

    try:
        paths = sorted((Path(reports_dir) / company).glob("*.md"))
    except OSError:
        return None
    if not paths:
        return None
    newest = paths[-1]
    report = read_report(newest.with_suffix(".json"))
    if report is not None:
        return ReportStamp(newest, report.as_of)
    try:
        return ReportStamp(newest, datetime.strptime(newest.stem, "%Y-%m-%d").replace(tzinfo=UTC))
    except ValueError:
        return ReportStamp(newest, None)


def latest_report_age(reports_dir: str | Path, company: str) -> datetime | None:
    """When the newest report for ``company`` was written, or None if unknown."""
    stamp = latest_report(reports_dir, company)
    return stamp.as_of if stamp else None


@dataclass
class CostRow:
    task: str
    model: str
    calls: int
    cost_usd: float


def llm_costs(engine: Any, since: str | None = None) -> list[CostRow]:
    with Session(engine) as session:
        query = select(LLMCallTable)
        if since:
            query = query.where(LLMCallTable.ts >= parse_date(since))
        rows = session.exec(query).all()
    grouped: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        grouped.setdefault((row.task, row.model), []).append(row.cost_usd)
    return [
        CostRow(task, model, len(costs), sum(costs))
        for (task, model), costs in sorted(grouped.items())
    ]


@dataclass
class Pulse:
    """What has arrived since the user last looked, plus recent activity."""

    since: datetime
    articles: int
    filings: int
    events: int
    daily: list[int]
    busiest: tuple[str, int] | None
    quietest: tuple[str, int] | None

    @property
    def total(self) -> int:
        return self.articles + self.filings + self.events


def pulse(
    engine: Any,
    *,
    instrument_ids: Sequence[str] = (),
    since: datetime,
    days: int = 30,
) -> Pulse:
    """Counts since ``since``, a daily histogram, and a per-instrument tally.

    Dates are publish/event time, not ingest time — no table records when a row
    was written — so this reports what was *published* since the last visit.

    Future-dated rows are excluded: the calendar plugin writes upcoming earnings
    into ``event.ts``, and those have not happened yet. ``upcoming_events`` is
    the mirror of this and takes exactly those rows — same table, opposite side
    of ``now``. Change one boundary and you must change the other.

    ``busiest``/``quietest`` cover the whole ``days`` window; the counts cover
    only ``since``. One windowed scan per table feeds all three outputs. Do not switch the
    per-instrument tally to a ``LIKE`` query: ``newsitem.instrument_ids`` is an
    unindexed JSON column, so that would be one full scan per instrument.
    """
    now = datetime.now(UTC)
    since = to_utc(since)
    window_start = now - timedelta(days=days - 1)
    floor = min(since, window_start)

    buckets: dict[date, int] = {}
    tally: dict[str, int] = dict.fromkeys(instrument_ids, 0)
    articles = filings = events = 0

    with Session(engine) as session:
        news = session.exec(
            select(
                NewsItemTable.published, NewsItemTable.source, NewsItemTable.instrument_ids
            ).where(NewsItemTable.published >= floor)
        ).all()
        event_rows = session.exec(
            select(EventTable.ts).where(EventTable.ts >= floor).where(EventTable.ts <= now)
        ).all()

    for published, source, raw_ids in news:
        ts = to_utc(published)
        if ts >= window_start:
            buckets[ts.date()] = buckets.get(ts.date(), 0) + 1
            # Tallied over the whole window, not just since the last visit: on a
            # short visit every instrument would otherwise read zero.
            for instrument_id in from_json(raw_ids):
                if instrument_id in tally:
                    tally[instrument_id] += 1
        if ts < since:
            continue
        if source == FILING_SOURCE:
            filings += 1
        else:
            articles += 1

    for value in event_rows:
        ts = to_utc(value)
        if ts >= window_start:
            buckets[ts.date()] = buckets.get(ts.date(), 0) + 1
        if ts >= since:
            events += 1

    daily = [buckets.get((window_start + timedelta(days=n)).date(), 0) for n in range(days)]
    ranked = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    # With nothing at all in the window there is no busiest to name.
    quiet = not ranked or ranked[0][1] == 0
    return Pulse(
        since=since,
        articles=articles,
        filings=filings,
        events=events,
        daily=daily,
        busiest=None if quiet else ranked[0],
        quietest=None if quiet or len(ranked) < 2 else ranked[-1],
    )


@dataclass
class Upcoming:
    """A scheduled event that has not happened yet."""

    instrument_id: str
    ts: datetime
    kind: str
    summary: str


def upcoming_events(
    engine: Any,
    *,
    instrument_ids: Sequence[str] = (),
    limit: int = 3,
) -> list[Upcoming]:
    """The next scheduled events for ``instrument_ids``, soonest first.

    The mirror of :func:`pulse`, which counts what has already happened: this
    takes the rows on the other side of ``now``. The calendar plugin writes
    upcoming earnings and ex-dividend dates into ``event.ts``, and nothing else
    in the app reads them.

    Both columns are indexed, so this stays one cheap query. No ``kind`` filter:
    only earnings and dividends are ever future-dated, and extracted events
    (regulatory, insider_trade) are always in the past.
    """
    ids = list(instrument_ids)
    if not ids:
        return []  # an empty IN () is a SQL error, and there is nothing to ask for

    now = datetime.now(UTC)
    with Session(engine) as session:
        rows = session.exec(
            select(EventTable.ts, EventTable.instrument_id, EventTable.kind, EventTable.summary)
            .where(EventTable.instrument_id.in_(ids))  # type: ignore[attr-defined]
            .where(EventTable.ts > now)
            .order_by(EventTable.ts)  # type: ignore[arg-type]
            .limit(limit)
        ).all()
    return [
        Upcoming(instrument_id=instrument_id, ts=to_utc(ts), kind=kind, summary=summary)
        for ts, instrument_id, kind, summary in rows
    ]


@dataclass
class Check:
    name: str
    ok: bool
    fix: str


def _provider_key(rig: Any, spec: ProviderSpec) -> str:
    """The configured key for ``spec``: Settings field, or .env for custom."""
    from rigger.core.config import read_env_value

    attr = _ENV_TO_ATTR.get(spec.env_var)
    if attr is not None:
        return str(getattr(rig.settings, attr, "") or "")
    return read_env_value(spec.env_var)


def setup_checks(rig: Any) -> list[Check]:
    provider = rig.cfg.llm_provider
    spec = PROVIDERS.get(provider)
    if spec is None:
        checks = [
            Check(
                f"LLM provider ({provider})",
                False,
                f"unknown provider; valid: {', '.join(sorted(PROVIDERS))}",
            )
        ]
    elif spec.name == "custom":
        ok = bool(_provider_key(rig, spec)) and bool(getattr(rig.cfg, "llm_base_url", ""))
        checks = [
            Check(
                f"LLM provider ({provider})",
                ok,
                "Press p on the Config screen to connect a custom endpoint",
            )
        ]
    else:
        checks = [
            Check(
                f"LLM provider ({provider})",
                bool(_provider_key(rig, spec)),
                f"Set {spec.env_var} in .env or press p on the Config screen",
            )
        ]
    checks.append(Check("Config file", Path("config.toml").exists(), "Create config.toml"))
    try:
        with Session(rig.engine) as session:
            session.get(LLMCallTable, "probe")  # never matches; just checks reachability
        reachable = True
    except Exception:
        reachable = False
    checks.append(Check("Database", reachable, "Check db_path in config.toml"))
    checks.append(
        Check(
            "SEC EDGAR contact",
            bool(rig.cfg.plugins.get("sec_edgar", {}).get("contact")),
            "Set [plugins.sec_edgar].contact",
        )
    )
    health = data_health(rig)
    checks.append(Check("Price history", bool(health.latest_bar), "Gather evidence"))
    return checks


@dataclass
class Headline:
    """The newest news title, for the Home overview."""

    title: str
    ts: datetime
    instrument_ids: tuple[str, ...]


def latest_headline(
    engine: Any, *, instrument_ids: Sequence[str] = (), scan: int = 50
) -> Headline | None:
    """Newest news item touching ``instrument_ids`` (any item when empty).

    Reads the newest ``scan`` rows by the indexed ``published`` column and
    filters in Python: ``instrument_ids`` is an unindexed JSON column, so a
    ``LIKE`` per instrument would be a full scan each.
    """
    wanted = set(instrument_ids)
    with Session(engine) as session:
        rows = session.exec(
            select(NewsItemTable.title, NewsItemTable.published, NewsItemTable.instrument_ids)
            .order_by(NewsItemTable.published.desc())  # type: ignore[attr-defined]
            .limit(scan)
        ).all()
    for title, published, raw_ids in rows:
        ids = tuple(from_json(raw_ids))
        if not wanted or wanted.intersection(ids):
            return Headline(title, to_utc(published), ids)
    return None


@dataclass
class ThesisHealth:
    """One thesis with its computed health; ``result`` is None with no accepted evidence."""

    thesis: Thesis
    result: HealthResult | None

    @property
    def state(self) -> str:
        return self.result.state if self.result else "emerging"


#: Most at risk first: the order the Home theses box lists claims in.
RISK_ORDER: dict[str, int] = {
    "challenged": 0,
    "weakening": 1,
    "mixed": 2,
    "idle": 3,
    "emerging": 4,
    "building": 5,
}


def thesis_fleet(engine: Any, *, now: datetime | None = None) -> list[ThesisHealth]:
    """Health of every thesis, most at risk first.

    Two indexed reads per thesis (accepted links, then the items by id) — the
    same path the Theses screen takes for one claim — so the overview stays
    cheap for a desk-sized fleet. Ties break on tilt (most negative first),
    then on the claim.
    """
    from rigger import theses as theses_mod
    from rigger.evidence import evidence_by_ids
    from rigger.thesis_health import compute_health

    now = now or datetime.now(UTC)
    fleet: list[ThesisHealth] = []
    for thesis in theses_mod.list_theses(engine):
        links = theses_mod.evidence_for(engine, thesis.id, accepted_only=True)
        by_id = {
            item.id: item for item in evidence_by_ids(engine, [row.evidence_id for row in links])
        }
        accepted = [(by_id[row.evidence_id], row.side) for row in links if row.evidence_id in by_id]
        result = compute_health(thesis, accepted, now=now) if accepted else None
        fleet.append(ThesisHealth(thesis, result))
    fleet.sort(
        key=lambda entry: (
            RISK_ORDER.get(entry.state, len(RISK_ORDER)),
            entry.result.tilt if entry.result else 0.0,
            entry.thesis.claim,
        )
    )
    return fleet
