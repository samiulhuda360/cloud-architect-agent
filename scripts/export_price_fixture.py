"""Export the price rows the catalogue uses (NZ North, Australia East, Front Door zones) from the local
cache into tests/fixtures/prices.json, so the test suite runs offline with real Azure prices.

    python scripts/export_price_fixture.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud_architect.catalogue import SERVICES  # noqa: E402
from cloud_architect.config import settings  # noqa: E402
from cloud_architect.pricing import FX_FILTER, Meter, PriceBook  # noqa: E402

REGIONS = ("newzealandnorth", "australiaeast")


def main() -> None:
    book = PriceBook(settings().price_cache)
    fixture: dict[str, list[dict]] = {}

    def keep(filt: str, currency: str = "USD", match=None):
        items = book.items(filt, currency)
        rows = [i for i in items if match is None or match(Meter.from_item(i))]
        fixture[f"{currency}|{filt}"] = rows

    for svc in SERVICES.values():
        for tier in svc.tiers.values():
            for u in tier.usages:
                service = u.service or svc.api_service
                for region in [u.region] if u.region else REGIONS:
                    filt = f"serviceName eq '{service}' and armRegionName eq '{region}' and priceType eq 'Consumption'"
                    extra = u.extra or svc.api_extra
                    filt += f" and {extra}" if extra else ""
                    key = f"USD|{filt}"
                    prev = fixture.get(key, [])
                    items = book.items(filt)
                    rows = [i for i in items if u.match(Meter.from_item(i))]
                    seen = {json.dumps(r, sort_keys=True) for r in prev}
                    fixture[key] = prev + [r for r in rows if json.dumps(r, sort_keys=True) not in seen]
    keep(FX_FILTER)
    keep(FX_FILTER, "NZD")
    out = ROOT / "tests" / "fixtures" / "prices.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=0, sort_keys=True), encoding="utf-8")
    print(f"{sum(len(v) for v in fixture.values())} rows in {len(fixture)} queries -> {out}")


if __name__ == "__main__":
    main()
