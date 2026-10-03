"""The Azure building blocks the agent can choose from, and how to price each one.

Every tier lists the exact price meters it consumes (matched against the live Retail Prices API)
and a quantity formula driven by the workload and the component's sizing. Meter names were
confirmed against the API in October 2026; `tests/test_live_prices.py` (weekly in CI) re-checks that every
recipe still matches a real meter, so a renamed meter fails CI instead of silently pricing at zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .models import Component, Workload, _squash
from .pricing import Meter

HOURS = 730  # Azure's monthly hours
SECONDS = HOURS * 3600
DAYS = 30.42


@dataclass(frozen=True)
class Usage:
    label: str
    match: Callable[[Meter], bool]
    quantity: Callable[[Component, Workload], float]
    unit: str
    service: str | None = None  # price from another API service (e.g. AKS nodes are Virtual Machines)
    extra: str = ""  # extra API filter for that service, e.g. one VM size
    free: float = 0  # monthly free grant subtracted before pricing
    region: str | None = None  # fixed pricing region (global services such as Front Door)


@dataclass(frozen=True)
class Tier:
    label: str
    usages: tuple[Usage, ...]
    zone_redundant_ok: bool = False
    production_grade: bool = True
    note: str = ""


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    api_service: str
    roles: tuple[str, ...]
    tiers: dict[str, Tier]
    api_extra: str = ""
    global_service: bool = False
    private_endpoint_ok: bool = True
    terraform: str = ""
    note: str = ""
    residency_note: str = ""
    data_store: bool = False
    meta: dict = field(default_factory=dict)
    zr_min_instances: int = 1  # the floor Azure enforces (and bills) once zone redundancy is on


def billed_instances(c: Component) -> int:
    """Instances Azure actually runs and bills: zone redundancy raises the floor for some services."""
    svc = SERVICES.get(c.service)
    return max(c.instances, svc.zr_min_instances if svc and c.zone_redundant else 1)


def _is(product: str | None = None, sku: str | None = None, meter: str | None = None, region: str | None = None):
    def match(m: Meter) -> bool:
        return (
            (product is None or m.product == product)
            and (sku is None or m.sku == sku)
            and (meter is None or m.meter == meter)
            and (region is None or m.region == region)
        )

    return match


def _s(c: Component, key: str, default: float) -> float:
    return float(c.settings.get(key, default))


def _hours(c: Component, w: Workload) -> float:
    return HOURS * billed_instances(c)


def _app_service(product: str, sku: str, label: str, zr: bool, prod: bool) -> Tier:
    return Tier(label, (Usage(f"{sku} plan instance hours", _is(product, sku), _hours, "hours"),), zone_redundant_ok=zr, production_grade=prod)


FLEX_GB = 2.0  # a 2,048 MB Flex instance bills as 2 GB (Azure divides MB-ms by 1,024,000)


def _flex_gb_seconds(c: Component) -> float:
    return _s(c, "executions_month", 500_000) * _s(c, "avg_seconds", 1) * FLEX_GB


def _flex_executions(c: Component) -> float:
    return _s(c, "executions_month", 500_000) / 10


def _db_storage_gb(c: Component, w: Workload) -> float:
    return max(32.0, _s(c, "storage_gb", w.data_gb * 1.5))


PG_STORAGE_SIZES_GB = (32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768)


def pg_storage_gb(c: Component, w: Workload) -> int:
    """PostgreSQL Flexible Server provisions (and bills) storage in fixed sizes, so round the need up to one."""
    need = _db_storage_gb(c, w)
    return next((s for s in PG_STORAGE_SIZES_GB if s >= need), PG_STORAGE_SIZES_GB[-1])


def _pg_compute(product: str, sku: str, meter: str) -> Usage:
    # General Purpose sizes are separate SKUs ("2 vCore", "4 vCore", ...) that all share the meter name "vCore",
    # each priced per server hour, so the SKU must be matched too.
    return Usage("compute hours", _is(product, sku, meter), lambda c, w: HOURS * (2 if c.zone_redundant else 1), "hours")


PG_STORAGE = Usage(
    "storage",
    _is("Azure Database for PostgreSQL Flex Server Storage", "Storage", "Storage Data Stored"),
    lambda c, w: pg_storage_gb(c, w) * (2 if c.zone_redundant else 1),
    "GB-month",
)


def _front_door(sku: str, label: str) -> Tier:
    # Base fees are global; requests and egress are priced for Zone 4 (Australia and New Zealand edge sites).
    return Tier(
        label,
        (
            Usage("base fee", _is("Azure Front Door", sku, f"{sku} Base Fees", "Zone 6"), lambda c, w: 1, "month", region="Zone 6"),
            Usage(
                "requests",
                _is("Azure Front Door", sku, f"{sku} Requests", "Zone 4"),
                lambda c, w: w.peak_rps * 0.15 * SECONDS / 10_000,
                "10K requests",
                region="Zone 4",
            ),
            Usage(
                "data transfer out (Oceania)",
                _is("Azure Front Door", sku, f"{sku} Data Transfer Out", "Zone 4"),
                lambda c, w: _s(c, "egress_gb", 20 + w.peak_rps * 6),
                "GB",
                region="Zone 4",
            ),
        ),
    )


PG_BURST = "Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute"
PG_GP = "Azure Database for PostgreSQL Flexible Server General Purpose Ddsv5 Series Compute"

SQL_STD = "SQL Database Single Standard"
SQL_GP = "SQL Database Single/Elastic Pool General Purpose - Compute Gen5"
SQL_GP_STORAGE = "SQL Database Single/Elastic Pool General Purpose - Storage"

OPENAI = "Azure OpenAI"


def _ca_tier(vcpu: float, gib: float) -> Tier:
    def active(c: Component, w: Workload) -> float:
        return c.instances * SECONDS * _s(c, "active_share", 0.35)

    def idle(c: Component, w: Workload) -> float:
        return c.instances * SECONDS * (1 - _s(c, "active_share", 0.35))

    return Tier(
        f"{vcpu:g} vCPU / {gib:g} GiB per replica, consumption plan",
        (
            Usage(
                "vCPU active seconds",
                _is("Azure Container Apps", "Standard", "Standard vCPU Active Usage"),
                lambda c, w: vcpu * active(c, w),
                "vCPU-s",
                free=180_000,
            ),
            Usage("vCPU idle seconds", _is("Azure Container Apps", "Standard", "Standard vCPU Idle Usage"), lambda c, w: vcpu * idle(c, w), "vCPU-s"),
            Usage(
                "memory active GiB-seconds",
                _is("Azure Container Apps", "Standard", "Standard Memory Active Usage"),
                lambda c, w: gib * active(c, w),
                "GiB-s",
                free=360_000,
            ),
            Usage(
                "memory idle GiB-seconds",
                _is("Azure Container Apps", "Standard", "Standard Memory Idle Usage"),
                lambda c, w: gib * idle(c, w),
                "GiB-s",
            ),
            Usage(
                "requests",
                _is("Azure Container Apps", "Standard", "Standard Requests"),
                lambda c, w: w.peak_rps * 0.15 * SECONDS / 1e6,
                "million",
                free=2,
            ),
        ),
        zone_redundant_ok=True,
    )


def _vm_usage(size: str, count: Callable[[Component, Workload], float], label: str) -> Usage:
    return Usage(
        label,
        lambda m: m.product.endswith("Series") and "Windows" not in m.product and m.sku == f"Standard_{size}",
        lambda c, w: HOURS * count(c, w),
        "hours",
        service="Virtual Machines",
        extra=f"armSkuName eq 'Standard_{size}'",
    )


def _blob(sku: str) -> Tier:
    """General-purpose v2 block blobs (the account type new deployments use). Volume tiers apply."""
    read = {"Hot LRS": "Hot Read Operations", "Hot ZRS": "Hot ZRS Read Operations", "Hot GRS": "Hot Read Operations"}[sku]
    product = "General Block Blob v2"
    return Tier(
        f"Blob storage, {sku}",
        (
            Usage("data stored", _is(product, sku, f"{sku} Data Stored"), lambda c, w: _s(c, "storage_gb", max(w.blob_gb, 1)), "GB-month"),
            Usage("write operations", _is(product, sku, f"{sku} Write Operations"), lambda c, w: _s(c, "write_10k", 20 + w.peak_rps * 2), "10K ops"),
            Usage("read operations", _is(product, sku, read), lambda c, w: _s(c, "read_10k", 50 + w.peak_rps * 10), "10K ops"),
        ),
        zone_redundant_ok=sku != "Hot LRS",
        note={
            "Hot LRS": "3 copies in one datacentre",
            "Hot ZRS": "3 copies across availability zones",
            "Hot GRS": "6 copies: 3 local + 3 in the paired region (not sold where no pair exists)",
        }[sku],
    )


def _llm(label: str, inp: str, out: str, deployment: str) -> Tier:
    # Matched on meter name only: Azure files models under several product lines
    # ("Azure OpenAI", "Azure OpenAI Reasoning", ...).
    return Tier(
        label,
        (
            Usage("input tokens", _is(None, None, inp), lambda c, w: _s(c, "input_mtokens", w.llm_input_mtokens) * 1000, "1K tokens"),
            Usage("output tokens", _is(None, None, out), lambda c, w: _s(c, "output_mtokens", w.llm_output_mtokens) * 1000, "1K tokens"),
        ),
        note=f"deployment: {deployment}",
    )


SERVICES: dict[str, Service] = {
    s.id: s
    for s in [
        Service(
            "app_service",
            "App Service (Linux)",
            "Azure App Service",
            ("web_app", "api"),
            {
                "B1": _app_service("Azure App Service Basic Plan - Linux", "B1", "Basic B1: 1 core, 1.75 GB, no zone redundancy", False, False),
                "S1": _app_service("Azure App Service Standard Plan - Linux", "S1", "Standard S1: 1 core, 1.75 GB, autoscale", False, True),
                "P0v3": _app_service(
                    "Azure App Service Premium v3 Plan - Linux", "P0v3", "Premium v3 P0v3: 1 vCPU, 4 GB, zone redundancy", True, True
                ),
                "P1v3": _app_service(
                    "Azure App Service Premium v3 Plan - Linux", "P1 v3", "Premium v3 P1v3: 2 vCPU, 8 GB, zone redundancy", True, True
                ),
                "P2v3": _app_service(
                    "Azure App Service Premium v3 Plan - Linux", "P2 v3", "Premium v3 P2v3: 4 vCPU, 16 GB, zone redundancy", True, True
                ),
            },
            terraform="app_service",
            note="Zone redundancy needs Premium v2+ and runs at least 2 instances (3 recommended for production).",
            zr_min_instances=2,
        ),
        Service(
            "container_apps",
            "Container Apps",
            "Azure Container Apps",
            ("web_app", "api", "containers", "background_jobs"),
            {
                "0.5vcpu": _ca_tier(0.5, 1),
                "1vcpu": _ca_tier(1, 2),
                "2vcpu": _ca_tier(2, 4),
            },
            terraform="container_apps",
            note="Instances = minimum replicas kept warm. Free grant per subscription is applied.",
        ),
        Service(
            "functions",
            "Functions (Flex Consumption)",
            "Functions",
            ("background_jobs", "api"),
            {
                # Zone redundancy keeps at least two always-ready instances running, billed around the clock with no
                # free grant, and executions land on them at always-ready rates. Without it, everything is on demand.
                "flex": Tier(
                    "Flex Consumption, 2 GB instances",
                    (
                        Usage(
                            "execution time",
                            _is("Flex Consumption", "On Demand", "On Demand Execution Time"),
                            lambda c, w: 0 if c.zone_redundant else _flex_gb_seconds(c),
                            "GB-s",
                            free=100_000,
                        ),
                        Usage(
                            "executions",
                            _is("Flex Consumption", "On Demand", "On Demand Total Executions"),
                            lambda c, w: 0 if c.zone_redundant else _flex_executions(c),
                            "10 executions",
                            free=25_000,
                        ),
                        Usage(
                            "always-ready baseline",
                            _is("Flex Consumption", "Always Ready", "Always Ready Baseline"),
                            lambda c, w: billed_instances(c) * FLEX_GB * SECONDS if c.zone_redundant else 0,
                            "GB-s",
                        ),
                        Usage(
                            "always-ready execution time",
                            _is("Flex Consumption", "Always Ready", "Always Ready Execution Time"),
                            lambda c, w: _flex_gb_seconds(c) if c.zone_redundant else 0,
                            "GB-s",
                        ),
                        Usage(
                            "always-ready executions",
                            _is("Flex Consumption", "Always Ready", "Always Ready Total Executions"),
                            lambda c, w: _flex_executions(c) if c.zone_redundant else 0,
                            "10 executions",
                        ),
                    ),
                    zone_redundant_ok=True,
                ),
            },
            terraform="functions",
            note="Zone redundancy keeps 2 always-ready instances running (about NZ$90 a month), so the app never scales to zero.",
            zr_min_instances=2,
        ),
        Service(
            "postgres",
            "PostgreSQL Flexible Server",
            "Azure Database for PostgreSQL",
            ("relational_db",),
            {
                "B1ms": Tier("Burstable B1ms: 1 vCore, 2 GB", (_pg_compute(PG_BURST, "B1MS", "B1MS"), PG_STORAGE), production_grade=False),
                "B2s": Tier("Burstable B2s: 2 vCores, 4 GB", (_pg_compute(PG_BURST, "B2S", "B2S"), PG_STORAGE), production_grade=False),
                "D2ds_v5": Tier(
                    "General Purpose D2ds v5: 2 vCores, 8 GB", (_pg_compute(PG_GP, "2 vCore", "vCore"), PG_STORAGE), zone_redundant_ok=True
                ),
                "D4ds_v5": Tier(
                    "General Purpose D4ds v5: 4 vCores, 16 GB", (_pg_compute(PG_GP, "4 vCore", "vCore"), PG_STORAGE), zone_redundant_ok=True
                ),
            },
            terraform="postgres",
            data_store=True,
            note="Zone-redundant HA runs a standby in another zone: compute and storage double.",
        ),
        Service(
            "azure_sql",
            "Azure SQL Database",
            "SQL Database",
            ("relational_db",),
            {
                "S0": Tier(
                    "Standard S0: 10 DTUs, 250 GB included",
                    (Usage("database days", _is(SQL_STD, "S0", "S0 DTUs"), lambda c, w: DAYS, "days"),),
                    production_grade=False,
                ),
                "S2": Tier(
                    "Standard S2: 50 DTUs, 250 GB included", (Usage("database days", _is(SQL_STD, "S2", "S2 DTUs"), lambda c, w: DAYS, "days"),)
                ),
                "GP_2vcore": Tier(
                    "General Purpose, 2 vCores",
                    (
                        Usage("compute hours", _is(SQL_GP, "2 vCore", "vCore"), lambda c, w: HOURS, "hours"),
                        Usage(
                            "zone redundancy",
                            _is(SQL_GP, "2 vCore Zone Redundancy", "Zone Redundancy vCore"),
                            lambda c, w: HOURS if c.zone_redundant else 0,
                            "hours",
                        ),
                        Usage("storage", _is(SQL_GP_STORAGE, "General Purpose", "General Purpose Data Stored"), _db_storage_gb, "GB-month"),
                    ),
                    zone_redundant_ok=True,
                ),
            },
            terraform="azure_sql",
            data_store=True,
        ),
        Service(
            "cosmos_db",
            "Cosmos DB (NoSQL)",
            "Azure Cosmos DB",
            ("nosql_db",),
            {
                "serverless": Tier(
                    "Serverless",
                    (
                        Usage(
                            "request units",
                            _is("Azure Cosmos DB serverless", "RUs", "1M RUs"),
                            lambda c, w: _s(c, "mru_month", max(1.0, w.peak_rps * 0.2 * SECONDS * 5 / 1e6)),
                            "million RUs",
                        ),
                        Usage(
                            "data stored",
                            _is("Azure Cosmos DB", "RUs", "Data Stored"),
                            lambda c, w: _s(c, "storage_gb", max(w.data_gb, 1)),
                            "GB-month",
                        ),
                    ),
                    production_grade=True,
                ),
                "provisioned_400": Tier(
                    "Provisioned 400 RU/s",
                    (
                        Usage(
                            "throughput (100 RU/s units)",
                            _is("Azure Cosmos DB", "RUs", "100 RU/s"),
                            lambda c, w: HOURS * _s(c, "ru_per_sec", 400) / 100,
                            "100 RU/s-hours",
                        ),
                        Usage(
                            "data stored",
                            _is("Azure Cosmos DB", "RUs", "Data Stored"),
                            lambda c, w: _s(c, "storage_gb", max(w.data_gb, 1)),
                            "GB-month",
                        ),
                    ),
                    zone_redundant_ok=True,
                ),
            },
            terraform="cosmos_db",
            data_store=True,
        ),
        Service(
            "blob_storage",
            "Storage account (Blob)",
            "Storage",
            ("object_storage",),
            {
                "hot_lrs": _blob("Hot LRS"),
                "hot_zrs": _blob("Hot ZRS"),
                "hot_grs": _blob("Hot GRS"),
            },
            api_extra="productName eq 'General Block Blob v2'",
            terraform="blob_storage",
            data_store=True,
            residency_note="New Zealand North has no paired region, so geo-redundant (GRS) storage is not sold there; use ZRS.",
        ),
        Service(
            "redis",
            "Azure Cache for Redis",
            "Redis Cache",
            ("cache",),
            {
                "basic_c0": Tier(
                    "Basic C0: 250 MB, single node, no SLA",
                    (Usage("cache hours", _is("Azure Redis Cache Basic", "C0", "C0 Cache"), _hours, "hours"),),
                    production_grade=False,
                ),
                "standard_c0": Tier(
                    "Standard C0: 250 MB, replicated", (Usage("cache hours", _is("Azure Redis Cache Standard", "C0", "C0 Cache"), _hours, "hours"),)
                ),
                "standard_c1": Tier(
                    "Standard C1: 1 GB, replicated", (Usage("cache hours", _is("Azure Redis Cache Standard", "C1", "C1 Cache"), _hours, "hours"),)
                ),
            },
            terraform="redis",
            note="Microsoft is moving customers to Azure Managed Redis; these tiers remain on sale but plan the migration.",
        ),
        Service(
            "key_vault",
            "Key Vault",
            "Key Vault",
            ("secrets",),
            {
                "standard": Tier(
                    "Standard",
                    (Usage("operations", _is("Key Vault", "Standard", "Operations"), lambda c, w: _s(c, "ops_10k", 10 + w.peak_rps), "10K ops"),),
                ),
            },
            terraform="key_vault",
        ),
        Service(
            "log_analytics",
            "Log Analytics workspace",
            "Log Analytics",
            ("monitoring",),
            {
                "pay_as_you_go": Tier(
                    "Analytics logs, pay as you go (first 5 GB a month free)",
                    (
                        Usage(
                            "log ingestion",
                            _is("Log Analytics", "Analytics Logs", "Analytics Logs Data Ingestion"),
                            lambda c, w: _s(c, "ingest_gb", 2 + w.peak_rps * 0.4),
                            "GB",
                        ),
                    ),
                ),
            },
            terraform="log_analytics",
        ),
        Service(
            "ai_search",
            "Azure AI Search",
            "Azure Cognitive Search",
            ("search",),
            {
                "basic": Tier(
                    "Basic: 15 GB, up to 3 replicas", (Usage("search unit hours", _is("Azure AI Search", "Basic", "Basic Unit"), _hours, "hours"),)
                ),
                "s1": Tier(
                    "Standard S1: 160 GB per partition",
                    (Usage("search unit hours", _is("Azure AI Search", "Standard S1", "Standard S1 Unit"), _hours, "hours"),),
                    zone_redundant_ok=True,
                ),
            },
            terraform="ai_search",
            note="Instances = replicas. Two or more replicas give a read SLA, three give a read-write SLA.",
        ),
        Service(
            "azure_openai",
            "Azure OpenAI (Foundry Models)",
            "Foundry Models",
            ("llm",),
            {
                "gpt-4o-mini_global": _llm(
                    "gpt-4o-mini, Global Standard", "gpt-4o-mini-0718-Inp-glbl Tokens", "gpt-4o-mini-0718-Outp-glbl Tokens", "global"
                ),
                "o4-mini_datazone": _llm(
                    "o4-mini, Data Zone Standard", "o4-mini 0416 Inp Data Zone Tokens", "o4-mini 0416 Outp Data Zone Tokens", "datazone"
                ),
            },
            terraform="azure_openai",
            residency_note=(
                "No Foundry model is sold in New Zealand North, and NZ North has no GPU VMs for self-hosting. Global "
                "deployments may process prompts in any Azure region; Data Zone deployments stay within a multi-country "
                "zone. Neither keeps processing in New Zealand."
            ),
        ),
        Service(
            "front_door",
            "Front Door",
            "Azure Front Door Service",
            ("edge",),
            {
                "standard": _front_door("Standard", "Standard: global edge, CDN, TLS, health-probe failover between regions"),
                "premium": _front_door("Premium", "Premium: Standard plus a managed-rule web application firewall and bot protection"),
            },
            global_service=True,
            private_endpoint_ok=False,
            terraform="front_door",
            note="Use it to fail over between regions; Premium includes the WAF, so it replaces Application Gateway in multi-region designs.",
            residency_note="Global edge network: NZ users usually connect at the Auckland edge, but routing and caching are global, "
            "so requests can be handled at edge sites outside New Zealand.",
        ),
        Service(
            "app_gateway",
            "Application Gateway WAF v2",
            "Application Gateway",
            ("edge",),
            {
                "waf_v2": Tier(
                    "WAF v2: regional layer-7 load balancer with web application firewall",
                    (
                        Usage("gateway hours", _is("Application Gateway WAF v2", "Standard", "Standard Fixed Cost"), lambda c, w: HOURS, "hours"),
                        Usage(
                            "capacity units",
                            _is("Application Gateway WAF v2", "Standard", "Standard Capacity Units"),
                            lambda c, w: HOURS * _s(c, "capacity_units", 2),
                            "CU-hours",
                        ),
                    ),
                    zone_redundant_ok=True,
                ),
            },
            private_endpoint_ok=False,
            terraform="app_gateway",
        ),
        Service(
            "aks",
            "AKS (Kubernetes)",
            "Azure Kubernetes Service",
            ("containers",),
            {
                "standard_d2s": Tier(
                    "Standard tier (uptime SLA) + D2s v5 nodes",
                    (
                        Usage("cluster uptime SLA", _is("Azure Kubernetes Service", "Standard", "Standard Uptime SLA"), lambda c, w: HOURS, "hours"),
                        _vm_usage("D2s_v5", lambda c, w: c.instances, "node hours (D2s v5)"),
                    ),
                    zone_redundant_ok=True,
                ),
                "standard_d4s": Tier(
                    "Standard tier (uptime SLA) + D4s v5 nodes",
                    (
                        Usage("cluster uptime SLA", _is("Azure Kubernetes Service", "Standard", "Standard Uptime SLA"), lambda c, w: HOURS, "hours"),
                        _vm_usage("D4s_v5", lambda c, w: c.instances, "node hours (D4s v5)"),
                    ),
                    zone_redundant_ok=True,
                ),
            },
            private_endpoint_ok=False,
            terraform="aks",
            note="Instances = nodes. Disks and egress not included.",
        ),
        Service(
            "vm",
            "Virtual Machine (Linux)",
            "Virtual Machines",
            ("virtual_machines",),
            {
                "D2s_v5": Tier("D2s v5: 2 vCPU, 8 GB", (_vm_usage("D2s_v5", lambda c, w: c.instances, "VM hours"),), zone_redundant_ok=True),
                "D4s_v5": Tier("D4s v5: 4 vCPU, 16 GB", (_vm_usage("D4s_v5", lambda c, w: c.instances, "VM hours"),), zone_redundant_ok=True),
            },
            private_endpoint_ok=False,
            terraform="vm",
            note="Disks, backup and egress not included.",
        ),
    ]
}

NEED_ROLES = {  # which catalogue roles satisfy each workload need
    "web_app": ("web_app",),
    "api": ("api", "web_app"),
    "relational_db": ("relational_db",),
    "nosql_db": ("nosql_db",),
    "object_storage": ("object_storage",),
    "cache": ("cache",),
    "llm": ("llm",),
    "search": ("search",),
    "background_jobs": ("background_jobs",),
    "containers": ("containers",),
    "virtual_machines": ("virtual_machines",),
}


def ambiguous_recipes(book, regions: tuple[str, ...] = ("newzealandnorth", "australiaeast")) -> list[str]:
    """Recipes whose filter matches two different prices for the same band, e.g. every Postgres size shares the meter 'vCore'."""
    out = []
    for sid, svc in SERVICES.items():
        for tid, tier in svc.tiers.items():
            for u in tier.usages:
                for region in regions:
                    rows = [m for m in book.meters(u.service or svc.api_service, u.region or region, u.extra or svc.api_extra) if u.match(m)]
                    prices: dict[float, set[float]] = {}
                    for m in rows:
                        prices.setdefault(m.tier_min, set()).add(m.usd)
                    if any(len(p) > 1 for p in prices.values()):
                        out.append(f"{sid}/{tid} {u.label} in {region}")
    return out


def canonical_names(service: str, tier: str) -> tuple[str, str]:
    """Map near-miss spellings ('App Service', 'p1 v3') to catalogue ids; anything unrecognised is left as written."""
    key = _squash(service)
    sid = next((i for i, s in SERVICES.items() if key in (_squash(i), _squash(s.name), _squash(s.name.split(" (")[0]))), service)
    if sid in SERVICES:
        tier = next((t for t in SERVICES[sid].tiers if _squash(t) == _squash(tier)), tier)
    return sid, tier


def options(role: str | None = None) -> list[dict]:
    """A compact menu for the agent: services, tiers and what each is good for."""
    out = []
    for s in SERVICES.values():
        if role and role not in s.roles:
            continue
        out.append(
            {
                "service": s.id,
                "name": s.name,
                "roles": list(s.roles),
                "tiers": {
                    k: {"label": t.label, "zone_redundant_ok": t.zone_redundant_ok, "production_grade": t.production_grade}
                    for k, t in s.tiers.items()
                },
                "note": s.note,
                "residency_note": s.residency_note,
                "global_service": s.global_service,
            }
        )
    return out
