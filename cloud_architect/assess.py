"""Price a design, check its hard constraints, and review it against the Well-Architected pillars.

Hard constraints (violations) must hold or be explicitly accepted in `design.accepted_exceptions`:
data residency, budget, every workload need covered, every component sold in its region.
Everything else is a review finding with a severity and a fix. All of it is deterministic, so the
same design always gets the same verdict, whoever (or whatever) proposed it.
"""

from __future__ import annotations

from .catalogue import NEED_ROLES, SERVICES, billed_instances
from .models import AVAILABILITY_TARGET, REGIONS, RESIDENCY_REGIONS, Assessment, Component, CostLine, Design, Finding, Workload
from .pricing import PriceBook, tiered_cost

COMPUTE_ROLES = {"web_app", "api", "containers", "virtual_machines"}
DATABASES = ("postgres", "azure_sql", "cosmos_db")
DEV_ONLY_TIERS = {("app_service", "B1"), ("postgres", "B1ms"), ("postgres", "B2s"), ("redis", "basic_c0"), ("azure_sql", "S0")}


def dr_gaps(design: Design, w: Workload) -> list[str]:
    """What stops the DR region from taking over. Naming a DR region is not DR: something has to run there."""
    if not design.dr_region:
        return ["no disaster-recovery region"]
    there = [c for c in design.components if c.region == design.dr_region and c.service in SERVICES]
    gaps = []
    if not any(set(SERVICES[c.service].roles) & COMPUTE_ROLES for c in there):
        gaps.append("no standby compute")
    if {"relational_db", "nosql_db"} & set(w.needs) and not any(c.service in DATABASES for c in there):
        gaps.append("no copy of the database")
    return gaps


def _accepted(design: Design, *keywords: str) -> str | None:
    for e in design.accepted_exceptions:
        if any(k.lower() in e.lower() for k in keywords):
            return e
    return None


def price_component(c: Component, w: Workload, book: PriceBook) -> tuple[list[CostLine], bool]:
    """Cost lines for one component, and whether Azure sells it in that region at all."""
    svc = SERVICES[c.service]
    tier = svc.tiers[c.tier]
    fx = book.usd_to_nzd()
    lines, sold = [], False
    for u in tier.usages:
        service = u.service or svc.api_service
        region = u.region or c.region
        rows = [m for m in book.meters(service, region, u.extra or svc.api_extra) if u.match(m)]
        if rows:
            sold = True
        qty = max(0.0, u.quantity(c, w))
        if qty == 0:
            continue
        # Some regions publish the monthly free grant as a $0 first band; subtract it only once.
        grant_listed = len(rows) > 1 and any(m.tier_min == 0 and m.usd == 0 for m in rows)
        billable = max(0.0, qty - (0 if grant_listed else u.free))
        nzd = tiered_cost(rows, billable) * fx
        lines.append(
            CostLine(service=c.service, tier=c.tier, region=c.region, item=u.label, quantity=round(qty, 2), unit=u.unit, nzd_month=round(nzd, 2))
        )
    return lines, sold


def assess(design: Design, w: Workload, book: PriceBook) -> Assessment:
    lines: list[CostLine] = []
    violations: list[Finding] = []
    findings: list[Finding] = []
    allowed = RESIDENCY_REGIONS[w.residency]

    def violate(pillar, rule, msg, fix="", accept_keys=()):
        # An exception counts when it names the rule code (e.g. "[RES-REGION] ...") or the thing it relaxes.
        accepted = _accepted(design, rule, *accept_keys) if accept_keys else None
        if accepted:
            findings.append(Finding(pillar=pillar, severity="high", rule=rule, message=f"{msg} Accepted: {accepted}", fix=fix))
        else:
            violations.append(Finding(pillar=pillar, severity="high", rule=rule, message=msg, fix=fix))

    def find(pillar, severity, rule, msg, fix=""):
        findings.append(Finding(pillar=pillar, severity=severity, rule=rule, message=msg, fix=fix))

    # ---------------------------------------------------------------- validity and price
    valid: list[Component] = []
    for c in design.components:
        svc = SERVICES.get(c.service)
        if not svc or c.tier not in svc.tiers:
            violate("coverage", "UNKNOWN", f"'{c.service}/{c.tier}' is not in the catalogue.", "Use list_options to pick a real service and tier.")
            continue
        if c.region not in REGIONS:
            violate("coverage", "REGION", f"{svc.name}: unknown region '{c.region}'.", f"Use one of {', '.join(REGIONS)}.")
            continue
        comp_lines, sold = price_component(c, w, book)
        if not sold:
            violate(
                "coverage",
                "NOT-SOLD",
                f"{svc.name} ({c.tier}) has no Azure price in {REGIONS[c.region]}: it is not sold there.",
                "Choose another region or service.",
            )
            continue
        lines += comp_lines
        valid.append(c)
    total = round(sum(line.nzd_month for line in lines), 2)
    by_id: dict[str, list[Component]] = {}
    for c in valid:
        by_id.setdefault(c.service, []).append(c)

    # ---------------------------------------------------------------- hard constraints
    for c in valid:
        svc = SERVICES[c.service]
        if svc.global_service:
            if w.residency != "any":
                find(
                    "residency",
                    "medium",
                    "RES-EDGE",
                    f"{svc.name}: {svc.residency_note}",
                    "Use Application Gateway in-region if traffic must not leave the country.",
                )
        elif c.region not in allowed:
            violate(
                "residency",
                "RES-REGION",
                f"{svc.name} is in {REGIONS[c.region]}, outside the allowed regions for "
                f"'{w.residency}' residency ({', '.join(REGIONS[r] for r in sorted(allowed))}).",
                "Move it, or record an accepted exception with the reason.",
                accept_keys=(c.service, svc.name, svc.name.split(" (")[0], "llm" if "llm" in svc.roles else c.service),
            )
        if c.service == "azure_openai":
            deployment = svc.tiers[c.tier].note.split(": ")[-1]
            if deployment == "global" and (w.residency != "any" or w.pii):
                violate(
                    "residency",
                    "RES-AI-GLOBAL",
                    "Global deployment: prompts (and any personal information in them) may be processed in any Azure region.",
                    "Use a Data Zone or regional deployment, or keep personal data out of prompts.",
                    accept_keys=("azure_openai", "llm", "global"),
                )
            elif deployment == "datazone" and w.residency == "nz":
                violate(
                    "residency",
                    "RES-AI-ZONE",
                    "Data Zone deployment processes prompts outside New Zealand.",
                    svc.residency_note,
                    accept_keys=("azure_openai", "llm"),
                )
            elif deployment == "datazone":
                find("residency", "medium", "RES-AI-ZONE", "Data Zone deployment: confirm the zone's countries meet your residency policy.")
    if design.dr_region and design.dr_region not in allowed:
        violate(
            "residency",
            "RES-DR",
            f"Disaster-recovery region {REGIONS.get(design.dr_region, design.dr_region)} is outside the allowed residency regions.",
            "Pick a DR region inside the residency boundary, or accept the exception.",
            accept_keys=("dr", "disaster"),
        )
    for need in w.needs:
        roles = set(NEED_ROLES[need])
        if not any(roles & set(SERVICES[c.service].roles) for c in valid):
            violate("coverage", "NEED", f"Nothing in the design covers the need '{need}'.", "Add a component for it.")
    if w.budget_nzd_month is not None and total > w.budget_nzd_month:
        violate(
            "cost",
            "BUDGET",
            f"Estimated NZ${total:,.0f}/month is over the NZ${w.budget_nzd_month:,.0f} budget by NZ${total - w.budget_nzd_month:,.0f}.",
            "Downsize tiers, cut instances, or raise the budget.",
            accept_keys=("budget",),
        )

    # ---------------------------------------------------------------- Well-Architected review
    prod = w.environment == "production"
    ha = prod and w.availability in ("high", "critical")
    for c in valid:
        svc, tier = SERVICES[c.service], SERVICES[c.service].tiers[c.tier]
        # A warm standby in the DR region is deliberately small, and read replicas cannot have HA of their own.
        standby = bool(design.dr_region) and c.region == design.dr_region and c.region != design.region
        if c.zone_redundant and not tier.zone_redundant_ok:
            find("reliability", "high", "REL-ZR-TIER", f"{svc.name} {c.tier} does not support zone redundancy.", "Move to a tier that does.")
        if ha and not standby and set(svc.roles) & COMPUTE_ROLES and not (c.zone_redundant and billed_instances(c) >= 2):
            find(
                "reliability",
                "high",
                "REL-ZONES",
                f"{svc.name} runs in one zone (or one instance) but the target is {AVAILABILITY_TARGET[w.availability]}.",
                "Enable zone redundancy with at least 2 instances (3 recommended).",
            )
        if ha and not standby and c.service in ("postgres", "azure_sql") and not c.zone_redundant:
            find("reliability", "high", "REL-DB-HA", f"{svc.name} has no zone-redundant standby.", "Enable zone-redundant high availability.")
        if ha and c.service == "blob_storage" and c.tier == "hot_lrs":
            find("reliability", "medium", "REL-STORAGE", "Locally redundant storage loses data if one datacentre fails.", "Use ZRS (or GRS for DR).")
        if prod and not tier.production_grade:
            find("reliability", "medium", "REL-TIER", f"{svc.name} {c.tier} is a dev/test tier ({tier.label}).", "Use a production tier.")
        if (
            w.environment == "dev"
            and (c.service, c.tier) not in DEV_ONLY_TIERS
            and c.service in ("app_service", "postgres", "redis", "azure_sql")
            and tier.production_grade
            and c.tier not in ("S1", "S2", "standard_c0")
        ):
            find("cost", "medium", "COST-DEV", f"{svc.name} {c.tier} is a production tier in a dev environment.", "Use a burstable or basic tier.")
        if w.pii and svc.data_store and svc.private_endpoint_ok and not c.private_endpoint:
            find(
                "security",
                "high",
                "SEC-PRIVATE",
                f"{svc.name} holds personal data but is reachable from the internet.",
                "Add a private endpoint and disable public network access.",
            )
    if w.availability == "critical" and prod:
        gaps = dr_gaps(design, w)
        if not design.dr_region:
            find(
                "reliability",
                "high",
                "REL-DR",
                f"A single region cannot meet {AVAILABILITY_TARGET['critical']} for the whole application.",
                "Add a disaster-recovery region."
                + (" New Zealand has one Azure region, so DR means leaving NZ or accepting the risk." if w.residency == "nz" else ""),
            )
        elif gaps:
            find(
                "reliability",
                "high",
                "REL-DR",
                f"{REGIONS.get(design.dr_region, design.dr_region)} is named as the DR region but has {' and '.join(gaps)}, "
                "so nothing can take over.",
                "Add a warm standby and a database replica in the DR region, with Front Door to switch traffic.",
            )
        elif "front_door" not in by_id:
            find(
                "reliability",
                "medium",
                "REL-FAILOVER",
                "No global entry point: failing over to the DR region needs a manual DNS change.",
                "Put Front Door in front of both regions with health probes.",
            )
    if "key_vault" not in by_id:
        find(
            "security",
            "high",
            "SEC-SECRETS",
            "No Key Vault: secrets and connection strings end up in app settings or code.",
            "Add Key Vault and use managed identities.",
        )
    waf = "app_gateway" in by_id or any(c.tier == "premium" for c in by_id.get("front_door", []))
    if w.pii and prod and any(set(SERVICES[c.service].roles) & {"web_app"} for c in valid) and not waf:
        find(
            "security",
            "medium",
            "SEC-WAF",
            "Public web app handling personal data with no web application firewall.",
            "Put Application Gateway WAF v2 (in-region) or Front Door Premium in front.",
        )
    if "log_analytics" not in by_id:
        find("operations", "medium", "OPS-LOGS", "No central logging: incidents will be investigated blind.", "Add a Log Analytics workspace.")
    if w.peak_rps > 50 and "relational_db" in w.needs and "redis" not in by_id:
        find("performance", "medium", "PERF-CACHE", f"{w.peak_rps:g} requests/s at peak will all hit the database.", "Add a cache.")
    if prod and any(c.service in ("vm", "aks", "postgres") for c in valid):
        find(
            "cost",
            "info",
            "COST-RESERVE",
            "Steady compute can be reserved for 1 or 3 years at a large discount.",
            "Price a reservation or savings plan once usage is stable.",
        )
    if w.monthly_users > 50_000 and "front_door" not in by_id and w.residency == "any":
        find("performance", "low", "PERF-EDGE", "Many users and no edge cache.", "Consider Front Door for static content.")

    return Assessment(total_nzd_month=total, lines=lines, violations=violations, findings=findings, fx_usd_to_nzd=book.usd_to_nzd())
