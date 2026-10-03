"""A rule-based architect: a sensible design from fixed rules, no language model.

It is the fallback when no LLM is configured and the baseline the agent is evaluated against.
The rules encode common Azure practice (zone redundancy for high availability, private endpoints
for personal data, Key Vault and Log Analytics always) and then downsize tier by tier until the
design fits the budget, recording what it gave up.
"""

from __future__ import annotations

from .assess import assess
from .models import RESIDENCY_REGIONS, Component, Design, Workload
from .pricing import PriceBook

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


def primary_region(w: Workload) -> str:
    return "newzealandnorth"  # users are in New Zealand; NZ North is allowed under every residency setting


def dr_region(w: Workload) -> str | None:
    if w.availability != "critical" or w.environment != "production":
        return None
    return {"nz": None, "anz": "australiaeast", "any": "australiaeast"}[w.residency]


def design(w: Workload) -> Design:
    region = primary_region(w)
    prod, ha = w.environment == "production", w.environment == "production" and w.availability in ("high", "critical")
    comps: list[Component] = []
    pe = w.pii and prod

    def add(service, tier, purpose, **kw):
        comps.append(Component(service=service, tier=tier, region=kw.pop("region", region), purpose=purpose, **kw))

    if "containers" in w.needs or ("web_app" in w.needs and "api" in w.needs and w.peak_rps > 100):
        add("container_apps", "1vcpu" if ha else "0.5vcpu", "web front end and API in containers", instances=3 if ha else 1, zone_redundant=ha)
    elif "web_app" in w.needs or "api" in w.needs:
        if not prod:
            add("app_service", "B1", "web app and API (dev)")
        elif ha:
            add("app_service", "P1v3", "web app and API, zone redundant", instances=3, zone_redundant=True)
        else:
            add("app_service", "S1", "web app and API", instances=1)
    if "background_jobs" in w.needs:
        add("functions", "flex", "scheduled and queued background jobs", zone_redundant=ha)
    if "virtual_machines" in w.needs:
        add("vm", "D2s_v5", "workload that needs a full VM", instances=2 if ha else 1, zone_redundant=ha)
    if "relational_db" in w.needs:
        tier = "B1ms" if not prod else ("D4ds_v5" if w.data_gb > 500 or w.peak_rps > 200 else "D2ds_v5")
        add("postgres", tier, "relational database", zone_redundant=ha, private_endpoint=pe)
    if "nosql_db" in w.needs:
        add("cosmos_db", "provisioned_400" if ha else "serverless", "document database", zone_redundant=ha, private_endpoint=pe)
    if "object_storage" in w.needs:
        add("blob_storage", "hot_zrs" if ha else "hot_lrs", "files and documents", private_endpoint=pe)
    if "cache" in w.needs or (w.peak_rps > 50 and "relational_db" in w.needs):
        add("redis", "standard_c0" if prod else "basic_c0", "cache for hot reads and sessions")
    if "search" in w.needs:
        add("ai_search", "s1" if ha else "basic", "full-text and vector search", instances=3 if ha else 1, zone_redundant=ha)
    exceptions, tradeoffs = [], []
    if "llm" in w.needs:
        if w.residency == "any" and not w.pii:
            add("azure_openai", "gpt-4o-mini_global", "language model", region="australiaeast")
        else:
            add("azure_openai", "o4-mini_datazone", "language model (Data Zone, Australia East)", region="australiaeast")
            if w.residency == "nz":
                exceptions.append(
                    "llm: no Azure language model is sold in New Zealand North; prompts are processed in an "
                    "Australian data zone. Keep personal data out of prompts or get approval for this exception."
                )
            tradeoffs.append("The language model runs in Australia East (Data Zone): the only way to keep it near NZ on Azure.")
    dr = dr_region(w)
    web_pii = w.pii and prod and "web_app" in w.needs
    if dr:
        # Warm standby: the same app at one instance and a replica of each database, promoted on failover.
        # Two regions need a global entry point; Front Door Premium carries the WAF, so no Application Gateway.
        for c in [c for c in comps if c.service in ("app_service", "container_apps")]:
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
        add("front_door", "premium" if web_pii else "standard", "global entry point: health probes move traffic to the DR region")
    elif web_pii:
        add("app_gateway", "waf_v2", "web application firewall in front of the app", zone_redundant=ha)
    add("key_vault", "standard", "secrets, keys and certificates")
    add("log_analytics", "pay_as_you_go", "logs and metrics for every component")
    if w.availability == "critical" and w.residency == "nz":
        tradeoffs.append(
            "New Zealand has one Azure region: a full DR site would have to leave NZ, so this design relies on "
            "three availability zones in Auckland and accepts the regional-outage risk."
        )
    return Design(
        region=region,
        dr_region=dr,
        components=comps,
        accepted_exceptions=exceptions,
        tradeoffs=tradeoffs,
        rationale=f"Rule-based design for {w.environment}, {w.availability} availability, residency '{w.residency}'.",
    )


def fit_budget(d: Design, w: Workload, book: PriceBook) -> Design:
    """Apply downgrades in order until the design fits the budget (or nothing is left to give up)."""
    if w.budget_nzd_month is None:
        return d
    for service, frm, to in DOWNGRADES:
        if assess(d, w, book).total_nzd_month <= w.budget_nzd_month:
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
                    d.tradeoffs.append(f"Downsized {service} from {frm} to {to} to fit the budget.")
    return d


def architect(w: Workload, book: PriceBook) -> Design:
    allowed = RESIDENCY_REGIONS[w.residency]
    d = design(w)
    assert d.region in allowed
    return fit_budget(d, w, book)
