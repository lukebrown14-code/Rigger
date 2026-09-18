"""SEC EDGAR data plugin: recent filings as news and XBRL company facts as fundamentals.

Compliance with SEC fair-access rules (https://www.sec.gov/os/accessing-edgar-data):

* every request carries ``User-Agent: Rigger/0.1 (<contact email>)``;
* requests are throttled to at most 10 per second;
* ``company_tickers.json`` is fetched once per plugin instance and cached.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from typing import Any

import httpx

from rigger.core.http import user_agent
from rigger.core.ids import stable_id
from rigger.core.models import Bar, Event, Fundamental, Instrument, NewsItem
from rigger.core.plugin import DataPlugin, DataProviderField, DataProviderSpec

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{document}"

#: Filing forms surfaced as NewsItem rows.
FORMS: frozenset[str] = frozenset({"8-K", "10-Q", "10-K", "4"})

#: XBRL tags to extract, as (namespace, primary tag, fallback tags).
FACT_TAGS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("us-gaap", "Revenues", ("RevenueFromContractWithCustomerExcludingAssessedTax",)),
    ("us-gaap", "NetIncomeLoss", ()),
    ("us-gaap", "EarningsPerShareDiluted", ()),
    ("dei", "CommonStockSharesOutstanding", ("EntityCommonStockSharesOutstanding",)),
)

MAX_REQUESTS_PER_SECOND = 10

# Used when EDGAR gives no primaryDocDescription (routinely the case for Form 4),
# so the brief and the extract model see what the filing is rather than "4: FORM 4".
FORM_LABELS = {
    "4": "insider transaction report (Form 4)",
    "8-K": "current report of a material event",
    "10-Q": "quarterly report",
    "10-K": "annual report",
}


class SECEdgar(DataPlugin):
    name = "sec_edgar"
    market = "us"
    provider_spec = DataProviderSpec(
        label="SEC EDGAR",
        fields=(DataProviderField("contact", "Contact email", required=True, placeholder="you@example.com"),),
        primary_disclosure=True,
        notice="SEC requires a real contact address in the User-Agent.",
    )

    def __init__(self) -> None:
        self.contact = "you@example.com"
        self._cik_by_symbol: dict[str, str] | None = None
        self._semaphore = asyncio.Semaphore(MAX_REQUESTS_PER_SECOND)

    def configure(self, cfg: dict[str, Any]) -> None:
        self.contact = str(cfg.get("contact", self.contact))

    @property
    def user_agent(self) -> str:
        return user_agent(self.contact)

    # ------------------------------------------------------------------ #
    # DataPlugin
    # ------------------------------------------------------------------ #
    async def fetch(
        self, instruments: list[Instrument], since: datetime
    ) -> list[Bar | NewsItem | Fundamental | Event]:
        since_date = since.date()
        out: list[Bar | NewsItem | Fundamental | Event] = []
        async with httpx.AsyncClient(
            headers={"User-Agent": self.user_agent}, timeout=30.0
        ) as client:
            ciks = await self._company_tickers(client)
            targets: list[tuple[Instrument, str]] = []
            for inst in instruments:
                if inst.market != self.market:
                    continue
                cik = ciks.get(inst.symbol.upper())
                if cik is None:
                    log.warning("sec_edgar: no CIK for %s, skipping", inst.symbol)
                    continue
                targets.append((inst, cik))
            # The semaphore in _get_json enforces the SEC rate cap, so fan out freely.
            per_instrument = await asyncio.gather(
                *(self._fetch_one(client, inst, cik, since_date) for inst, cik in targets)
            )
        for rows in per_instrument:
            out.extend(rows)
        return out

    async def _fetch_one(
        self, client: httpx.AsyncClient, inst: Instrument, cik: str, since: date
    ) -> list[NewsItem | Fundamental]:
        submissions, facts = await asyncio.gather(
            self._get_json(client, SUBMISSIONS_URL.format(cik=cik)),
            self._get_json(client, COMPANYFACTS_URL.format(cik=cik)),
        )
        rows: list[NewsItem | Fundamental] = []
        rows.extend(self._filings_to_news(inst, cik, submissions, since))
        rows.extend(self._facts_to_fundamentals(inst, facts))
        return rows

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    async def _get_json(self, client: httpx.AsyncClient, url: str) -> Any:
        # Each of the MAX_REQUESTS_PER_SECOND slots is held for a full second
        # after its request, so at most that many requests start per second.
        async with self._semaphore:
            resp = await client.get(url)
            resp.raise_for_status()
            await asyncio.sleep(1.0)
            return resp.json()

    async def _company_tickers(self, client: httpx.AsyncClient) -> dict[str, str]:
        if self._cik_by_symbol is None:
            payload = await self._get_json(client, TICKERS_URL)
            rows = payload.values() if isinstance(payload, dict) else payload
            self._cik_by_symbol = {
                str(row["ticker"]).upper(): f"{int(row['cik_str']):010d}" for row in rows
            }
        return self._cik_by_symbol

    # ------------------------------------------------------------------ #
    # Filings -> NewsItem
    # ------------------------------------------------------------------ #
    def _filings_to_news(
        self, inst: Instrument, cik: str, submissions: dict[str, Any], since: date
    ) -> list[NewsItem]:
        recent = submissions.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        documents = recent.get("primaryDocument", [])
        descriptions = recent.get("primaryDocDescription", [])
        items: list[NewsItem] = []
        for form, filed_raw, accession, document, description in zip(
            forms, dates, accessions, documents, descriptions, strict=True
        ):
            if form not in FORMS:
                continue
            filed = date.fromisoformat(filed_raw)
            if filed < since:
                continue
            items.append(
                NewsItem(
                    id=stable_id(accession),
                    instrument_ids=[inst.id],
                    published=datetime(filed.year, filed.month, filed.day, tzinfo=UTC),
                    title=f"{form}: {description or FORM_LABELS.get(form, form)}",
                    url=FILING_URL.format(
                        cik_int=int(cik),
                        accession=accession.replace("-", ""),
                        document=document,
                    ),
                    body=None,
                    source=self.name,
                )
            )
        return items

    # ------------------------------------------------------------------ #
    # Company facts -> Fundamental
    # ------------------------------------------------------------------ #
    def _facts_to_fundamentals(
        self, inst: Instrument, payload: dict[str, Any]
    ) -> list[Fundamental]:
        facts = payload.get("facts", {})
        out: list[Fundamental] = []
        for namespace, tag, fallbacks in FACT_TAGS:
            series = _find_series(facts, namespace, (tag, *fallbacks))
            if not series:
                continue
            for form in ("10-K", "10-Q"):
                fact = _latest_fact(series, form)
                if fact is None:
                    continue
                out.append(
                    Fundamental(
                        instrument_id=inst.id,
                        as_of=date.fromisoformat(fact["end"]),
                        metric=f"{tag}_{fact['fp']}",
                        value=float(fact["val"]),
                        source=self.name,
                    )
                )
        return out


def _find_series(
    facts: dict[str, Any], namespace: str, tags: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Facts for every listed tag, searching ``namespace`` first.

    Filers switch tags over time (Apple stopped reporting ``Revenues`` in 2018
    in favour of ``RevenueFromContractWithCustomerExcludingAssessedTax``), so
    the series are merged and the caller's "latest by end date" pick decides.
    """
    series: list[dict[str, Any]] = []
    for ns in (namespace, *(n for n in facts if n != namespace)):
        concepts = facts.get(ns, {})
        for tag in tags:
            for values in concepts.get(tag, {}).get("units", {}).values():
                series.extend(values)
    return series


def _latest_fact(series: list[dict[str, Any]], form: str) -> dict[str, Any] | None:
    """Latest fact for ``form`` by ``end`` date (then ``filed``).

    Duration facts are filtered so a 10-K yields the full-year figure and a
    10-Q the single-quarter figure rather than the year-to-date comparative.
    """
    candidates: list[dict[str, Any]] = []
    for fact in series:
        if fact.get("form") != form or not fact.get("fp"):
            continue
        start = fact.get("start")
        if start:
            days = (date.fromisoformat(fact["end"]) - date.fromisoformat(start)).days
            if form == "10-K" and days < 300:
                continue
            if form == "10-Q" and days > 120:
                continue
        candidates.append(fact)
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f["end"], f.get("filed", "")))
