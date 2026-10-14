"""Survey which catalogue services and tiers Azure sells in every supported region, from the live Retail
Prices API, and write cloud_architect/data/availability.json. The rule-based architect uses it to choose
regions (for example, where a language model can run inside a jurisdiction).

    python scripts/refresh_availability.py           # about 800 API queries, cached for a week
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud_architect.catalogue import SERVICES  # noqa: E402
from cloud_architect.config import settings  # noqa: E402
from cloud_architect.models import Component  # noqa: E402
from cloud_architect.pricing import PriceBook  # noqa: E402
from cloud_architect.regions import ALL  # noqa: E402

OUT = ROOT / "cloud_architect" / "data" / "availability.json"


def survey(book: PriceBook, region: str) -> list[str]:
    sold = []
    for sid, svc in SERVICES.items():
        for tid, tier in svc.tiers.items():
            comp = Component.model_construct(service=sid, tier=tid, region=region, settings={})
            for u in tier.usages:
                rows = book.meters(u.service or svc.api_service, u.pricing_region(comp), u.extra or svc.api_extra)
                if any(u.match(m) for m in rows):
                    sold.append(f"{sid}/{tid}")
                    break
    return sold


def main() -> None:
    book = PriceBook(settings().price_cache)
    regions = sorted(ALL)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(zip(regions, pool.map(lambda r: survey(book, r), regions), strict=True))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {"source": "Azure Retail Prices API", "surveyed": dt.date.today().isoformat(), "regions": results},
            indent=1,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    total = sum(len(t) for s in SERVICES.values() for t in [s.tiers])
    for r in regions:
        print(f"{r:22s} {len(results[r]):3d}/{total}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
