"""A country-specific source only receives instruments from its assigned markets."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rigger import services
from rigger.core.models import Instrument
from rigger.core.plugin import DataPlugin


class ScopedSource(DataPlugin):
    name = "scoped"

    def __init__(self) -> None:
        self.enabled = True
        self.seen: list[str] = []

    async def fetch(self, instruments, since):
        self.seen = [instrument.id for instrument in instruments]
        return []


def test_ingest_applies_data_plugin_market_scope(tmp_engine):
    source = ScopedSource()
    source.configure({"scope": {"markets": ["lse"]}})
    rig = SimpleNamespace(
        engine=tmp_engine,
        plugins={"scoped": source},
        universe=lambda: [
            Instrument(id="LSE:HL", market="lse", symbol="HL", currency="GBP"),
            Instrument(id="ASX:BHP", market="asx", symbol="BHP", currency="AUD"),
        ],
    )

    asyncio.run(services.ingest(rig, since="2026-01-01"))

    assert source.seen == ["LSE:HL"]
