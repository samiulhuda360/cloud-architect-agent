"""Azure regions, the jurisdictions data may be kept in, and how a region is chosen for a workload.

Region names, pairs and restrictions follow Microsoft's published region list ("Azure geographies"
and "Azure region pairs"). Which catalogue services each region sells comes from the live Retail
Prices API (`scripts/refresh_availability.py` writes `data/availability.json`): Azure publishes a
retail price for every service it sells in a region, so no price means not sold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Region:
    code: str
    name: str
    city: str
    country: str  # ISO 3166 alpha-2
    geography: str  # europe | americas | asia_pacific | middle_east_africa
    pair: str | None = None
    restricted: bool = False  # access by request only (in-country customers): never chosen automatically

    @property
    def label(self) -> str:
        return f"{self.name} ({self.city})"


_R = [
    # Americas
    Region("eastus", "East US", "Virginia", "US", "americas", "westus"),
    Region("eastus2", "East US 2", "Virginia", "US", "americas", "centralus"),
    Region("centralus", "Central US", "Iowa", "US", "americas", "eastus2"),
    Region("northcentralus", "North Central US", "Illinois", "US", "americas", "southcentralus"),
    Region("southcentralus", "South Central US", "Texas", "US", "americas", "northcentralus"),
    Region("westus", "West US", "California", "US", "americas", "eastus"),
    Region("westus2", "West US 2", "Washington", "US", "americas", "westcentralus"),
    Region("westus3", "West US 3", "Arizona", "US", "americas", "eastus"),
    Region("westcentralus", "West Central US", "Wyoming", "US", "americas", "westus2"),
    Region("canadacentral", "Canada Central", "Toronto", "CA", "americas", "canadaeast"),
    Region("canadaeast", "Canada East", "Quebec City", "CA", "americas", "canadacentral"),
    Region("brazilsouth", "Brazil South", "Sao Paulo", "BR", "americas", "southcentralus"),
    # Europe
    Region("uksouth", "UK South", "London", "GB", "europe", "ukwest"),
    Region("ukwest", "UK West", "Cardiff", "GB", "europe", "uksouth"),
    Region("westeurope", "West Europe", "Netherlands", "NL", "europe", "northeurope"),
    Region("northeurope", "North Europe", "Ireland", "IE", "europe", "westeurope"),
    Region("francecentral", "France Central", "Paris", "FR", "europe", "francesouth"),
    Region("francesouth", "France South", "Marseille", "FR", "europe", "francecentral", restricted=True),
    Region("germanywestcentral", "Germany West Central", "Frankfurt", "DE", "europe", "germanynorth"),
    Region("germanynorth", "Germany North", "Berlin", "DE", "europe", "germanywestcentral", restricted=True),
    Region("swedencentral", "Sweden Central", "Gavle", "SE", "europe", "swedensouth"),
    Region("swedensouth", "Sweden South", "Malmo", "SE", "europe", "swedencentral", restricted=True),
    Region("norwayeast", "Norway East", "Oslo", "NO", "europe", "norwaywest"),
    Region("norwaywest", "Norway West", "Stavanger", "NO", "europe", "norwayeast", restricted=True),
    Region("switzerlandnorth", "Switzerland North", "Zurich", "CH", "europe", "switzerlandwest"),
    Region("switzerlandwest", "Switzerland West", "Geneva", "CH", "europe", "switzerlandnorth", restricted=True),
    Region("italynorth", "Italy North", "Milan", "IT", "europe"),
    Region("spaincentral", "Spain Central", "Madrid", "ES", "europe"),
    Region("polandcentral", "Poland Central", "Warsaw", "PL", "europe"),
    # Asia Pacific
    Region("australiaeast", "Australia East", "Sydney", "AU", "asia_pacific", "australiasoutheast"),
    Region("australiasoutheast", "Australia Southeast", "Melbourne", "AU", "asia_pacific", "australiaeast"),
    Region("newzealandnorth", "New Zealand North", "Auckland", "NZ", "asia_pacific"),
    Region("southeastasia", "Southeast Asia", "Singapore", "SG", "asia_pacific", "eastasia"),
    Region("eastasia", "East Asia", "Hong Kong", "HK", "asia_pacific", "southeastasia"),
    Region("japaneast", "Japan East", "Tokyo", "JP", "asia_pacific", "japanwest"),
    Region("japanwest", "Japan West", "Osaka", "JP", "asia_pacific", "japaneast"),
    Region("koreacentral", "Korea Central", "Seoul", "KR", "asia_pacific", "koreasouth"),
    Region("centralindia", "Central India", "Pune", "IN", "asia_pacific", "southindia"),
    Region("southindia", "South India", "Chennai", "IN", "asia_pacific", "centralindia"),
    # Middle East and Africa
    Region("uaenorth", "UAE North", "Dubai", "AE", "middle_east_africa", "uaecentral"),
    Region("southafricanorth", "South Africa North", "Johannesburg", "ZA", "middle_east_africa", "southafricawest"),
    Region("israelcentral", "Israel Central", "Israel", "IL", "middle_east_africa"),
    Region("qatarcentral", "Qatar Central", "Doha", "QA", "middle_east_africa"),
]
ALL: dict[str, Region] = {r.code: r for r in _R}
REGIONS: dict[str, str] = {r.code: r.label for r in _R}  # code -> "UK South (London)"

EEA = {"NL", "IE", "FR", "DE", "SE", "NO", "IT", "ES", "PL"}  # countries in this table inside the EU/EEA

# Where data may live, by jurisdiction. "anz" (New Zealand plus Australia) is kept for older workload files.
JURISDICTIONS: dict[str, dict] = {
    "eu": {"label": "EU / EEA (GDPR)", "countries": EEA, "default": "westeurope", "currency": "EUR"},
    "uk": {"label": "United Kingdom", "countries": {"GB"}, "default": "uksouth", "currency": "GBP"},
    "us": {"label": "United States", "countries": {"US"}, "default": "eastus2", "currency": "USD"},
    "canada": {"label": "Canada", "countries": {"CA"}, "default": "canadacentral", "currency": "CAD"},
    "australia": {"label": "Australia", "countries": {"AU"}, "default": "australiaeast", "currency": "AUD"},
    "nz": {"label": "New Zealand", "countries": {"NZ"}, "default": "newzealandnorth", "currency": "NZD"},
    "anz": {"label": "New Zealand and Australia", "countries": {"NZ", "AU"}, "default": "newzealandnorth", "currency": "NZD"},
    "any": {"label": "Anywhere", "countries": None, "default": "eastus2", "currency": "USD"},
}
RESIDENCY_REGIONS: dict[str, set[str]] = {
    j: {r.code for r in _R if spec["countries"] is None or r.country in spec["countries"]} for j, spec in JURISDICTIONS.items()
}

# Countries the EU recognises as giving adequate protection (adequacy decisions, including the EU-US Data
# Privacy Framework for certified organisations). Personal data can move there without extra safeguards.
GDPR_ADEQUATE = EEA | {"GB", "CH", "CA", "JP", "KR", "IL", "NZ", "US"}

# Azure OpenAI Data Zone deployments keep processing inside a documented zone: the EU or the United States.
DATA_ZONES = {"eu": EEA, "us": {"US"}}

CURRENCIES = ("USD", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "SEK", "NOK", "DKK", "JPY", "SGD", "INR", "BRL")
SYMBOL = {
    "USD": "US$",
    "EUR": "€",
    "GBP": "£",
    "AUD": "A$",
    "NZD": "NZ$",
    "CAD": "C$",
    "CHF": "CHF ",
    "JPY": "¥",
    "SGD": "S$",
    "INR": "₹",
    "BRL": "R$",
}

# Where users are -> the region closest to them. Keys are lower-case names, ISO codes and common short forms.
_COUNTRY_REGION = {
    "uk": "uksouth", "gb": "uksouth", "united kingdom": "uksouth", "great britain": "uksouth", "england": "uksouth", "scotland": "uksouth",
    "wales": "ukwest", "northern ireland": "ukwest",
    "ie": "northeurope", "ireland": "northeurope",
    "nl": "westeurope", "netherlands": "westeurope", "belgium": "westeurope", "be": "westeurope", "luxembourg": "westeurope", "lu": "westeurope",
    "de": "germanywestcentral", "germany": "germanywestcentral", "austria": "germanywestcentral", "at": "germanywestcentral",
    "fr": "francecentral", "france": "francecentral",
    "se": "swedencentral", "sweden": "swedencentral", "finland": "swedencentral", "fi": "swedencentral",
    "denmark": "swedencentral", "dk": "swedencentral",
    "no": "norwayeast", "norway": "norwayeast",
    "it": "italynorth", "italy": "italynorth",
    "es": "spaincentral", "spain": "spaincentral", "portugal": "spaincentral", "pt": "spaincentral",
    "pl": "polandcentral", "poland": "polandcentral", "czechia": "polandcentral", "cz": "polandcentral",
    "ch": "switzerlandnorth", "switzerland": "switzerlandnorth",
    "eu": "westeurope", "europe": "westeurope",
    "us": "eastus2", "usa": "eastus2", "united states": "eastus2", "america": "eastus2", "us east": "eastus2", "east coast": "eastus2",
    "us west": "westus2", "west coast": "westus2", "california": "westus", "texas": "southcentralus", "us central": "centralus",
    "ca": "canadacentral", "canada": "canadacentral", "quebec": "canadaeast",
    "br": "brazilsouth", "brazil": "brazilsouth", "mexico": "southcentralus", "mx": "southcentralus",
    "au": "australiaeast", "australia": "australiaeast", "sydney": "australiaeast", "melbourne": "australiasoutheast",
    "nz": "newzealandnorth", "new zealand": "newzealandnorth", "aotearoa": "newzealandnorth",
    "sg": "southeastasia", "singapore": "southeastasia", "malaysia": "southeastasia", "indonesia": "southeastasia",
    "hk": "eastasia", "hong kong": "eastasia", "jp": "japaneast", "japan": "japaneast", "kr": "koreacentral", "korea": "koreacentral",
    "in": "centralindia", "india": "centralindia",
    "ae": "uaenorth", "uae": "uaenorth", "united arab emirates": "uaenorth", "za": "southafricanorth", "south africa": "southafricanorth",
    "il": "israelcentral", "israel": "israelcentral", "qa": "qatarcentral", "qatar": "qatarcentral",
}  # fmt: skip

# Front Door bills requests and egress by the edge zone users are served from.
FRONT_DOOR_ZONE = {"europe": "Zone 1", "americas": "Zone 1", "middle_east_africa": "Zone 1", "asia_pacific": "Zone 2"}


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def region_code(value):
    """'UK South', 'uk south (London)' or 'uksouth' -> 'uksouth'. Unknown names pass through unchanged."""
    if not isinstance(value, str):
        return value
    key = _squash(value)
    for r in _R:
        if key in (r.code, _squash(r.name), _squash(r.label)):
            return r.code
    return value.strip()


def location_region(location: str) -> str | None:
    """Where a group of users is ('Germany', 'UK', 'US West', 'uksouth') -> the nearest Azure region."""
    code = region_code(location)
    if code in ALL:
        return code
    return _COUNTRY_REGION.get(location.strip().lower().rstrip("."))


def geography(region: str) -> str:
    r = ALL.get(region)
    return r.geography if r else "americas"


def front_door_zone(region: str) -> str:
    r = ALL.get(region)
    if r and r.country in ("AU", "NZ"):
        return "Zone 4"
    if r and r.country == "IN":
        return "Zone 5"
    if r and r.country == "BR":
        return "Zone 3"
    return FRONT_DOOR_ZONE[geography(region)]


def label(region: str | None) -> str:
    return REGIONS.get(region or "", region or "")


# ---------------------------------------------------------------- service availability (from the live price list)
@lru_cache(maxsize=1)
def availability() -> dict[str, list[str]]:
    """region -> catalogue 'service/tier' entries Azure sells there (snapshot from the Retail Prices API)."""
    path = DATA / "availability.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["regions"]


def sold(service: str, tier: str, region: str) -> bool | None:
    """Whether the snapshot shows a price for this tier in this region; None when the region was not surveyed."""
    regions = availability()
    if region not in regions:
        return None
    return f"{service}/{tier}" in regions[region]


def has_zones(region: str) -> bool:
    """Whether a region has availability zones: zone-redundant storage is sold only where zones exist."""
    return sold("blob_storage", "hot_zrs", region) is not False


def sells_all(region: str, wanted: list[tuple[str, str]]) -> bool:
    return all(sold(s, t, region) is not False for s, t in wanted)


# ---------------------------------------------------------------- choosing regions
def allowed_regions(residency: str) -> set[str]:
    return RESIDENCY_REGIONS[residency]


def nearest(regions: set[str] | list[str], to: str) -> list[str]:
    """Regions ordered by closeness to `to`: same country, then same geography, then the rest."""
    home = ALL.get(to)
    return sorted(
        regions,
        key=lambda r: (r != to, not home or ALL[r].country != home.country, not home or ALL[r].geography != home.geography, r),
    )


def primary_region(allowed: set[str], default: str, locations: list[str], preferred: str | None = None, wanted=()) -> str:
    """The region closest to the users that the rules allow and that sells every wanted service."""
    open_ = {r for r in allowed if r in ALL and not ALL[r].restricted}
    candidates = ([preferred] if preferred else []) + [location_region(loc) for loc in locations] + [default]
    anchor = next((c for c in candidates if c in open_), None) or default
    candidates += nearest(open_, anchor)
    for c in candidates:
        if c and c in open_ and sells_all(c, list(wanted)):
            return c
    return next((c for c in candidates if c in open_), default)


def dr_region(primary: str, allowed: set[str], wanted=()) -> str | None:
    """The paired region when the rules allow it and it is open to everyone; otherwise the nearest allowed region."""
    open_ = {r for r in allowed if r in ALL and not ALL[r].restricted and r != primary}
    pair = ALL[primary].pair if primary in ALL else None
    if pair in open_ and sells_all(pair, list(wanted)):
        return pair
    return next((r for r in nearest(open_, primary) if sells_all(r, list(wanted))), None)
