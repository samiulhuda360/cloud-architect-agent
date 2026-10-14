"""A rule-based architect: a sensible design from fixed rules, no language model.

It is the fallback when no LLM is configured and the baseline the agent is evaluated against.
The rules encode common Azure practice and the workload's requirements:

- region: closest to the users inside the residency and policy boundary; DR in the paired region;
- reliability: zone redundancy for high availability, a warm standby for a 99.99% target or a
  short RTO, a database replica for a tight RPO, geo-redundant backups when backups must survive
  a regional outage, long-term retention past 35 days;
- security: private endpoints for sensitive data, a WAF for card data and public personal data,
  customer-managed keys and tags when a policy asks, audit logs and log retention for the frameworks;
- capacity: database tier and storage from 12- and 36-month growth, instances from peak concurrency,
  a cache for read-heavy traffic, background jobs for integrations;
- stack: the team's database and compute constraints.

It then downsizes tier by tier until the design fits the budget, recording what it gave up.
"""

from __future__ import annotations

import math

from .assess import USERS_PER_INSTANCE, assess
from .catalogue import backup_copies
from .compliance import PITR_MAX_DAYS, requirements
from .models import Component, Design, Workload
from .pricing import PriceBook
from .regions import ALL, DATA_ZONES, JURISDICTIONS, dr_region, label, location_region, nearest, primary_region, sold

# Downgrades tried in order when a design is over budget: (service, from tier) -> to tier
DOWNGRADES = [
    ("app_service", "P1v3", "P0v3"),
    ("postgres", "D4ds_v5", "D2ds_v5"),
    ("ai_search", "s1", "basic"),
    ("redis", "standard_c1", "standard_c0"),
    ("app_service", "P0v3", "S1"),
    ("postgres", "D2ds_v5", "B2s"),
    ("azure_sql", "GP_2vcore", "S2"),
    ("app_gateway", "waf_v2", None),
    ("redis", "standard_c0", "basic_c0"),
    ("app_service", "S1", "B1"),
    ("postgres", "B2s", "B1ms"),
    ("azure_sql", "S2", "S0"),
]
MAX_AUTOSCALE = {"B1": 3, "S1": 10, "P0v3": 30, "P1v3": 30, "P2v3": 30}


def _wanted(w: Workload, engine: str) -> list[tuple[str, str]]:
    """Representative tiers the primary region must sell for this workload."""
    out = []
    if "web_app" in w.needs or "api" in w.needs:
        out.append(("app_service", "P1v3"))
    if "relational_db" in w.needs:
        out.append((engine, "D2ds_v5" if engine == "postgres" else "GP_2vcore"))
    for need, item in {
        "nosql_db": ("cosmos_db", "serverless"),
        "object_storage": ("blob_storage", "hot_lrs"),
        "cache": ("redis", "standard_c0"),
        "search": ("ai_search", "basic"),
        "background_jobs": ("functions", "flex"),
        "containers": ("container_apps", "1vcpu"),
        "virtual_machines": ("vm", "D2s_v5"),
    }.items():
        if need in w.needs:
            out.append(item)
    return out


def choose_regions(w: Workload, engine: str = "postgres") -> tuple[str, set[str]]:
    req = requirements(w)
    default = JURISDICTIONS[w.residency]["default"]
    if default not in req.allowed and req.allowed:
        default = sorted(req.allowed)[0]
    wanted = _wanted(w, engine)
    if w.environment == "production" and w.availability in ("high", "critical"):
        wanted.append(("blob_storage", "hot_zrs"))  # sold only where there are availability zones
    return primary_region(req.allowed, default, w.user_locations, w.preferred_region, wanted), req.allowed


def llm_placement(w: Workload, region: str, allowed: set[str]) -> tuple[str, str, list[str], list[str]]:
    """(tier, region, exceptions, tradeoffs) for the language model: in the boundary when Azure sells a way to do it."""
    jur = JURISDICTIONS[w.residency]["label"]
    everywhere = set(ALL)
    if w.residency == "any" and not w.pii:
        r = next((r for r in nearest(everywhere, region) if sold("azure_openai", "gpt-4o-mini_global", r)), region)
        return "gpt-4o-mini_global", r, [], []
    order = nearest(allowed, region)
    zone = DATA_ZONES.get(w.residency)
    if zone:  # an EU or US Data Zone keeps processing inside the jurisdiction
        for r in order:
            if sold("azure_openai", "o4-mini_datazone", r):
                return "o4-mini_datazone", r, [], [f"The language model uses a Data Zone deployment: prompts stay in the {w.residency.upper()} zone."]
    for tier in ("o4-mini_regional", "gpt-4o-mini_regional"):
        for r in order:
            if sold("azure_openai", tier, r):
                return tier, r, [], [f"The language model uses a Regional deployment in {label(r)}: prompts are processed there."]
    if w.residency == "any":
        r = next((r for r in nearest(everywhere, region) if sold("azure_openai", "o4-mini_datazone", r)), region)
        return "o4-mini_datazone", r, [], ["Personal data: the model uses a Data Zone deployment instead of Global."]
    dz = [r for r in nearest(everywhere, region) if sold("azure_openai", "o4-mini_datazone", r)]
    inside = [r for r in dz if r in allowed]
    if inside and w.residency in ("australia", "anz"):
        r = inside[0]
        return "o4-mini_datazone", r, [], [f"The language model runs in {label(r)} (Data Zone): the closest option Azure sells for {jur}."]
    r = dz[0] if dz else region
    zone = next(
        (f"the {z.upper()} data zone" for z, countries in DATA_ZONES.items() if r in ALL and ALL[r].country in countries), "a multi-country data zone"
    )
    exception = (
        f"[RES-REGION] [RES-AI-ZONE] llm: Azure sells no language model that keeps processing inside {jur}; prompts are processed "
        f"in {zone} from {label(r)}. Keep personal data out of prompts or get approval for this exception."
    )
    return "o4-mini_datazone", r, [exception], [f"The language model runs in {label(r)}, outside {jur}: see the accepted exception."]


def design(w: Workload) -> Design:
    req = requirements(w)
    stack = req.stack
    engine = "azure_sql" if any(s == "azure_sql" for s, _ in stack["must"] + stack["prefer"]) else "postgres"
    if any(s == engine for s, _ in stack["forbid"]):
        engine = "postgres" if engine == "azure_sql" else "azure_sql"
    region, allowed = choose_regions(w, engine)
    prod = w.environment == "production"
    ha = prod and w.availability in ("high", "critical")
    pe = req.private_data.value
    cmk = bool(req.cmk.value)
    approved = req.approved.value

    def ok(service: str) -> bool:  # an approved-services policy rules out optional extras it does not list
        return approved is None or service in approved

    comps: list[Component] = []
    exceptions: list[str] = []
    tradeoffs: list[str] = []

    def add(service, tier, purpose, **kw):
        c = Component(service=service, tier=tier, region=kw.pop("region", region), purpose=purpose, **kw)
        comps.append(c)
        return c

    # ---------------------------------------------------------------- compute
    wants_aks = any(s == "aks" for s, _ in stack["must"] + stack["prefer"]) and not any(s == "aks" for s, _ in stack["forbid"])
    wants_ca = any(s == "container_apps" for s, _ in stack["must"] + stack["prefer"])
    concurrent = w.concurrent()
    if wants_aks and ("containers" in w.needs or "web_app" in w.needs or "api" in w.needs):
        add("aks", "standard_d2s", "Kubernetes cluster for the web front end and API", instances=3 if ha else 2, zone_redundant=ha)
    elif "containers" in w.needs or wants_ca or ("web_app" in w.needs and "api" in w.needs and w.peak_rps > 100):
        tier = "1vcpu" if ha else "0.5vcpu"
        replicas = 3 if ha else 1
        needed = math.ceil(concurrent / USERS_PER_INSTANCE[("container_apps", tier)])
        settings = {"max_instances": float(needed)} if prod and needed > max(replicas * 3, 3) else {}
        add("container_apps", tier, "web front end and API in containers", instances=replicas, zone_redundant=ha, settings=settings)
    elif "web_app" in w.needs or "api" in w.needs:
        if not prod:
            tier, n, zr, purpose = "B1", 1, False, "web app and API (dev)"
        elif ha:
            tier, n, zr, purpose = "P1v3", 3, True, "web app and API, zone redundant"
        else:
            tier, n, zr, purpose = "S1", 1, False, "web app and API"
        needed = math.ceil(concurrent / USERS_PER_INSTANCE[("app_service", tier)]) if prod else 1
        if needed > MAX_AUTOSCALE[tier]:
            tier = "P1v3"
            needed = math.ceil(concurrent / USERS_PER_INSTANCE[("app_service", tier)])
        settings = {"max_instances": float(min(needed, MAX_AUTOSCALE[tier]))} if needed > n else {}
        if settings:
            purpose += f", autoscale to {settings['max_instances']:g} instances for {concurrent:,} concurrent users"
        add("app_service", tier, purpose, instances=n, zone_redundant=zr, settings=settings)
    if "background_jobs" in w.needs or (w.integrations and ok("functions")):
        jobs = "scheduled and queued background jobs"
        if w.integrations:
            jobs += f", including sync with {', '.join(w.integrations)} (queued, with retries)"
        add("functions", "flex", jobs, zone_redundant=ha)
    if "virtual_machines" in w.needs:
        add("vm", "D2s_v5", "workload that needs a full VM", instances=2 if ha else 1, zone_redundant=ha)

    # ---------------------------------------------------------------- data
    has_pair = bool(region in ALL and ALL[region].pair)
    backup_need = int(req.backup_days.value)
    geo = bool(req.geo_backup.value)
    if geo and not has_pair and req.dr == "none":
        tradeoffs.append(f"{label(region)} has no paired region, so geo-redundant backup is not offered; backups stay in-region.")
    if "relational_db" in w.needs:
        at36 = w.data_gb_at(36)
        write_heavy = w.peak_rps > 100 and w.read_ratio < 0.5
        db: dict[str, float] = {"backup_days": float(min(max(backup_need, 7), PITR_MAX_DAYS))}
        if geo and has_pair:
            db["geo_backup"] = 1
        if engine == "postgres":
            tier = "B1ms" if not prod else ("D4ds_v5" if at36 > 500 or w.peak_rps > 200 or write_heavy else "D2ds_v5")
            add("postgres", tier, "relational database", zone_redundant=ha, private_endpoint=pe, customer_managed_key=cmk, settings=db)
            if backup_need > PITR_MAX_DAYS and not ok("backup_vault"):
                tradeoffs.append(f"Backups are needed for {backup_need} days but Azure Backup is not an approved service; PostgreSQL keeps 35.")
            elif backup_need > PITR_MAX_DAYS:
                add(
                    "backup_vault",
                    "grs" if geo and has_pair else "lrs",
                    f"long-term PostgreSQL backups, kept {backup_need} days",
                    settings={"retention_days": float(backup_need)},
                )
        else:
            tier = "S0" if not prod else ("GP_2vcore" if ha or at36 > 250 or write_heavy else "S2")
            if backup_need > PITR_MAX_DAYS:
                db["ltr_days"] = float(backup_need)
                db["ltr_gb"] = round(w.data_gb_at(12) * backup_copies(backup_need), 1)
            add("azure_sql", tier, "relational database (SQL Server engine)", zone_redundant=ha and tier == "GP_2vcore", private_endpoint=pe,
                customer_managed_key=cmk, settings=db)  # fmt: skip
    if "nosql_db" in w.needs:
        add(
            "cosmos_db",
            "provisioned_400" if ha else "serverless",
            "document database",
            zone_redundant=ha,
            private_endpoint=pe,
            customer_managed_key=cmk,
        )
    if "object_storage" in w.needs:
        grs = (geo or req.dr != "none") and has_pair and sold("blob_storage", "hot_grs", region) is not False
        tier = "hot_grs" if grs else ("hot_zrs" if ha else "hot_lrs")
        settings = {"delete_after_days": round(w.retention_years * 365)} if w.retention_years else {}
        purpose = "files and documents" + (f", deleted after {w.retention_years:g} years" if w.retention_years else "")
        add("blob_storage", tier, purpose, private_endpoint=pe, customer_managed_key=cmk, settings=settings)
    cache_for_reads = w.peak_rps > 50 and w.read_ratio >= 0.5 and "relational_db" in w.needs
    web = [c for c in comps if c.service in ("app_service", "container_apps")]
    sessions = prod and concurrent > 500 and any(c.instances > 1 or c.settings.get("max_instances", 1) > 1 for c in web)
    if "cache" in w.needs or ((cache_for_reads or sessions) and ok("redis")):
        why = "cache for hot reads" if cache_for_reads or "cache" in w.needs else "shared session store"
        add("redis", "standard_c0" if prod else "basic_c0", why + (" and sessions" if sessions and why != "shared session store" else ""))
    if "search" in w.needs:
        add("ai_search", "s1" if ha else "basic", "full-text and vector search", instances=3 if ha else 1, zone_redundant=ha)
    if "llm" in w.needs:
        tier, llm_region, exc, notes = llm_placement(w, region, allowed)
        add("azure_openai", tier, "language model", region=llm_region)
        exceptions += exc
        tradeoffs += notes
    if w.identity == "entra_external_id" and ok("entra_external_id"):
        add("entra_external_id", "core", "customer sign-up and sign-in (first 50,000 monthly users free)")

    # ---------------------------------------------------------------- disaster recovery and the edge
    dr = None
    if req.dr != "none":
        dr = dr_region(region, allowed, _wanted(w, engine))
        if dr is None:
            tradeoffs.append(
                f"No second region inside the allowed boundary can host the DR site, so this design relies on availability zones "
                f"in {label(region)} and accepts the regional-outage risk."
            )
    web_sensitive = bool(req.waf.value) and prod and "web_app" in w.needs
    geos = {ALL[r].geography for r in (location_region(x) for x in w.user_locations) if r in ALL}
    if dr:
        if req.dr == "warm":
            # Warm standby: the same app at one instance and a replica of each database, promoted on failover.
            for c in [c for c in comps if c.service in ("app_service", "container_apps", "aks")]:
                add(c.service, c.tier, "warm standby in the DR region, scaled out after failover", region=dr)
        for c in [c for c in comps if c.service in ("postgres", "azure_sql", "cosmos_db")]:
            add(
                c.service,
                c.tier,
                "cross-region replica, promoted on failover",
                region=dr,
                private_endpoint=c.private_endpoint,
                settings={"replica": 1},
            )
    if ok("front_door") and (dr and req.dr == "warm" or (prod and "web_app" in w.needs and len(geos) > 1)):
        # Two regions, or users on several continents, need a global entry point; Premium carries the WAF.
        why = (
            "global entry point: health probes move traffic to the DR region"
            if dr and req.dr == "warm"
            else "edge network for users on several continents"
        )
        add("front_door", "premium" if web_sensitive else "standard", why)
    elif web_sensitive and ok("app_gateway"):
        add("app_gateway", "waf_v2", "web application firewall in front of the app", zone_redundant=ha)
    elif web_sensitive and ok("front_door"):
        add("front_door", "premium", "web application firewall at the edge (Application Gateway is not an approved service)")
    kv = add("key_vault", "standard", "secrets, keys and certificates")
    if cmk:
        kv.settings["cmk_keys"] = float(sum(c.customer_managed_key for c in comps))
        kv.purpose += ", including the customer-managed encryption keys"
    logs = add("log_analytics", "pay_as_you_go", "logs and metrics for every component")
    if req.log_days.value > 30:
        logs.settings["retention_days"] = float(req.log_days.value)
        logs.purpose += f", kept {req.log_days.value} days"
    if req.audit_logs.value:
        logs.settings["audit"] = 1
        logs.purpose += ", with audit logs from every data store"
    tags = {"workload": w.name, "environment": "prod" if prod else w.environments[0]}
    tags.update({t: "" for t in req.tags.value if t not in tags})
    return Design(
        region=region,
        dr_region=dr,
        components=comps,
        accepted_exceptions=exceptions,
        tradeoffs=tradeoffs,
        tags=tags,
        rationale=(
            f"Rule-based design for {', '.join(w.environments)}, {w.availability} availability, "
            f"{JURISDICTIONS[w.residency]['label']} residency, in {label(region)}" + (f" with DR in {label(dr)}." if dr else ".")
        ),
    )


def fit_budget(d: Design, w: Workload, book: PriceBook) -> Design:
    """Apply downgrades in order until the design fits the budget and any spend cap (or nothing is left to give up)."""
    caps = [w.budget_month] if w.budget_month is not None else []
    for p in requirements(w).spend_cap:
        caps.append(p.amount / book.fx(p.currency or w.currency) * book.fx(w.currency))
    if not caps:
        return d
    limit = min(caps)
    for service, frm, to in DOWNGRADES:
        if assess(d, w, book).total_month <= limit:
            break
        for c in list(d.components):
            if c.service == service and c.tier == frm:
                if to is None:
                    d.components.remove(c)
                    d.tradeoffs.append(f"Removed {service} ({frm}) to fit the budget.")
                else:
                    c.tier = to
                    if c.zone_redundant and service in ("app_service",) and to in ("S1", "B1"):
                        c.zone_redundant, c.instances = False, 1
                    if service == "postgres" and to.startswith("B"):
                        c.zone_redundant = False
                    if service == "azure_sql" and to != "GP_2vcore":
                        c.zone_redundant = False
                    if "max_instances" in c.settings and service == "app_service":
                        c.settings["max_instances"] = float(min(c.settings["max_instances"], MAX_AUTOSCALE[to]))
                    d.tradeoffs.append(f"Downsized {service} from {frm} to {to} to fit the budget.")
    return d


def architect(w: Workload, book: PriceBook) -> Design:
    d = design(w)
    assert d.region in requirements(w).allowed or not requirements(w).allowed
    return fit_budget(d, w, book)
