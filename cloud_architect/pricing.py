"""Live Azure prices from the public Retail Prices API (no account or key needed).

Prices are fetched in USD and converted to NZD at Azure's own exchange rate. The API can return
NZD directly, but it rounds to four decimals, which turns per-second meters (Container Apps,
Functions) into zero. Azure prices every currency from USD at a fixed rate it publishes through
the same API, so the rate is derived from a reference meter priced in both currencies.

Every query result is cached on disk for a week (prices change monthly at most), so a design is
priced in well under a second after the first run, and tests run offline from a fixture cache.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API = "https://prices.azure.com/api/retail/prices"
WEEK = 7 * 24 * 3600
# Reference meter for the USD -> NZD rate: a Linux D2s v5 hour in Australia East.
FX_FILTER = (
    "serviceName eq 'Virtual Machines' and armRegionName eq 'australiaeast' and priceType eq 'Consumption' "
    "and armSkuName eq 'Standard_D2s_v5' and productName eq 'Virtual Machines Dsv5 Series'"
)


@dataclass(frozen=True)
class Meter:
    """One priced unit, e.g. 'P1 v3 App, 1 Hour, US$0.169'. Tiered meters come as several rows."""

    product: str
    sku: str
    meter: str
    unit: str
    usd: float
    tier_min: float
    region: str

    @classmethod
    def from_item(cls, i: dict) -> "Meter":
        return cls(
            i["productName"],
            i["skuName"],
            i["meterName"],
            i["unitOfMeasure"],
            float(i["retailPrice"]),
            float(i.get("tierMinimumUnits") or 0),
            i.get("armRegionName", ""),
        )


class PriceBook:
    def __init__(self, cache_path: Path, offline: bool = False, ttl: int = WEEK):
        self.offline, self.ttl = offline, ttl
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(cache_path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS q (filter TEXT PRIMARY KEY, fetched REAL, items TEXT)")
        self._lock = threading.Lock()
        self._fx: float | None = None

    # ---------------------------------------------------------------- raw API
    def items(self, filt: str, currency: str = "USD") -> list[dict]:
        key = f"{currency}|{filt}"
        with self._lock:
            row = self._db.execute("SELECT fetched, items FROM q WHERE filter = ?", (key,)).fetchone()
        if row and (self.offline or time.time() - row[0] < self.ttl):
            return json.loads(row[1])
        if self.offline:
            raise LookupError(f"not in the offline price cache: {filt}")
        params = {"$filter": filt, **({"currencyCode": currency} if currency != "USD" else {})}
        url, out = API + "?" + urllib.parse.urlencode(params), []
        while url:
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(url, timeout=60) as r:
                        data = json.load(r)
                    break
                except Exception:  # noqa: BLE001  transient network or 429: back off and retry
                    if attempt == 3:
                        raise
                    time.sleep(2 * (attempt + 1))
            out += data.get("Items", [])
            url = data.get("NextPageLink")
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO q VALUES (?, ?, ?)", (key, time.time(), json.dumps(out)))
            self._db.commit()
        return out

    def meters(self, service: str, region: str, extra: str = "") -> list[Meter]:
        filt = f"serviceName eq '{service}' and armRegionName eq '{region}' and priceType eq 'Consumption'"
        return [Meter.from_item(i) for i in self.items(filt + (f" and {extra}" if extra else ""))]

    def available(self, service: str, region: str, extra: str = "") -> bool:
        """Azure publishes a retail price for every service it sells in a region; no price, no service."""
        return bool(self.meters(service, region, extra))

    # ---------------------------------------------------------------- currency
    def usd_to_nzd(self) -> float:
        if self._fx is None:
            usd = [i["retailPrice"] for i in self.items(FX_FILTER) if i["skuName"] == "Standard_D2s_v5"]
            nzd = [i["retailPrice"] for i in self.items(FX_FILTER, "NZD") if i["skuName"] == "Standard_D2s_v5"]
            if not usd or not nzd:
                raise LookupError("could not derive Azure's USD to NZD rate")
            self._fx = round(nzd[0] / usd[0], 4)
        return self._fx


def tiered_cost(rows: list[Meter], quantity: float) -> float:
    """Cost in USD of `quantity` units for a meter that may have volume tiers (e.g. first 5 GB free)."""
    if not rows or quantity <= 0:
        return 0.0
    rows = sorted({(m.tier_min, m.usd): m for m in rows}.values(), key=lambda m: m.tier_min)  # Azure repeats some rows
    total = 0.0
    for n, m in enumerate(rows):
        upper = rows[n + 1].tier_min if n + 1 < len(rows) else float("inf")
        band = max(0.0, min(quantity, upper) - m.tier_min)
        total += band * m.usd
        if quantity <= upper:
            break
    return total
