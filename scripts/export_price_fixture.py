"""Export the price rows the tests need from the local price cache into tests/fixtures/prices.json, so the
test suite runs offline with real Azure prices.

It records every query made while designing and pricing the evaluation scenarios and the examples, and
while pricing every catalogue tier in the regions the tests use, plus the exchange-rate meter in every
supported currency. Only rows that match a catalogue recipe are kept.

    python scripts/export_price_fixture.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud_architect.assess import assess  # noqa: E402
from cloud_architect.catalogue import SERVICES  # noqa: E402
from cloud_architect.config import settings  # noqa: E402
from cloud_architect.evaluate import load_examples, load_scenarios  # noqa: E402
from cloud_architect.models import Component, Design, Workload  # noqa: E402
from cloud_architect.pricing import FX_FILTER, Meter, PriceBook  # noqa: E402
from cloud_architect.regions import CURRENCIES  # noqa: E402
from cloud_architect.rules import architect  # noqa: E402

REGIONS = ("newzealandnorth", "australiaeast", "uksouth", "ukwest", "eastus2", "westeurope", "germanywestcentral", "northeurope")


class Recorder(PriceBook):
    def __init__(self, path: Path):
        super().__init__(path)
        self.seen: dict[str, list[dict]] = {}

    def items(self, filt: str, currency: str = "USD") -> list[dict]:
        rows = super().items(filt, currency)
        self.seen[f"{currency}|{filt}"] = rows
        return rows


def main() -> None:
    book = Recorder(settings().price_cache)
    for c in CURRENCIES:
        book.fx(c)
    for _, w, _ in load_scenarios():
        assess(architect(w, book), w, book)
    for _, w in load_examples():
        assess(architect(w, book), w, book)
    w = Workload(name="fixture", needs=["web_app"], peak_rps=20, blob_gb=100, llm_input_mtokens=10, llm_output_mtokens=2)
    for region in REGIONS:
        for sid, svc in SERVICES.items():
            for tier in svc.tiers:
                assess(Design(region=region, components=[Component(service=sid, tier=tier, region=region)]), w, book)
    matchers = [u.match for s in SERVICES.values() for t in s.tiers.values() for u in t.usages]
    fixture = {}
    for key, rows in book.seen.items():
        keep = rows if key.endswith(FX_FILTER) else [r for r in rows if any(m(Meter.from_item(r)) for m in matchers)]
        fixture[key] = keep
    out = ROOT / "tests" / "fixtures" / "prices.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    print(f"{sum(len(v) for v in fixture.values())} rows in {len(fixture)} queries -> {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
