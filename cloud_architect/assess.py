"""Price a design, check its hard constraints, and review it against the Well-Architected pillars.

Hard constraints (violations) must hold or be explicitly accepted in `design.accepted_exceptions`:
data residency, budget, every workload need covered, every component sold in its region, the
stack constraints, and every company policy rule the user accepted. Everything else is a review
finding with a severity, a fix and, where one applies, the framework clause or policy it comes
from. All of it is deterministic, so the same design always gets the same verdict, whoever (or
whatever) proposed it.
"""

from __future__ import annotations

import math

from .catalogue import NEED_ROLES, SERVICES, billed_instances, pg_storage_gb
from .compliance import CLAUSES, PITR_MAX_DAYS, Requirements, requirements
from .models import AVAILABILITY_TARGET, REGIONS, RESIDENCY_REGIONS, Assessment, Component, CostLine, Design, Finding, Workload
from .pricing import PriceBook, tiered_cost
from .regions import ALL, DATA_ZONES, GDPR_ADEQUATE, JURISDICTIONS, SYMBOL, has_zones, label, location_region

COMPUTE_ROLES = {"web_app", "api", "containers", "virtual_machines"}
DATABASES = ("postgres", "azure_sql", "cosmos_db")
DEV_ONLY_TIERS = {("app_service", "B1"), ("postgres", "B1ms"), ("postgres", "B2s"), ("redis", "basic_c0"), ("azure_sql", "S0")}
# Rule of thumb for a typical web app: concurrent users one instance or replica serves comfortably.
USERS_PER_INSTANCE = {
    ("app_service", "B1"): 100, ("app_service", "S1"): 250, ("app_service", "P0v3"): 400, ("app_service", "P1v3"): 800,
    ("app_service", "P2v3"): 1600, ("container_apps", "0.5vcpu"): 200, ("container_apps", "1vcpu"): 400, ("container_apps", "2vcpu"): 800,
    ("vm", "D2s_v5"): 400, ("vm", "D4s_v5"): 800, ("aks", "standard_d2s"): 400, ("aks", "standard_d4s"): 800,
}  # fmt: skip
SQL_MAX_GB = {"S0": 250, "S2": 250}
# The cheapest tier of each service, used to price dev and test copies of the production design.
NONPROD_TIER = {
    "app_service": "B1", "container_apps": "0.5vcpu", "postgres": "B1ms", "azure_sql": "S0", "cosmos_db": "serverless", "redis": "basic_c0",
    "ai_search": "basic", "blob_storage": "hot_lrs", "aks": "standard_d2s",
}  # fmt: skip
SHARED = ("front_door", "app_gateway", "entra_external_id", "backup_vault")  # not copied into dev and test


def money(amount: float, currency: str) -> str:
    return f"{SYMBOL.get(currency, currency + ' ')}{amount:,.0f}"


def dr_gaps(design: Design, w: Workload, need_compute: bool = True) -> list[str]:
    """What stops the DR region from taking over. Naming a DR region is not DR: something has to run there."""
    if not design.dr_region:
        return ["no disaster-recovery region"]
    there = [c for c in design.components if c.region == design.dr_region and c.service in SERVICES]
    gaps = []
    if need_compute and not any(set(SERVICES[c.service].roles) & COMPUTE_ROLES for c in there):
        gaps.append("no standby compute")
    if {"relational_db", "nosql_db"} & set(w.needs) and not any(c.service in DATABASES for c in there):
        gaps.append("no copy of the database")
    return gaps


def _accepted(design: Design, *keywords: str) -> str | None:
    for e in design.accepted_exceptions:
        if any(k and k.lower() in e.lower() for k in keywords):
            return e
    return None


def price_component(c: Component, w: Workload, book: PriceBook, environment: str = "prod") -> tuple[list[CostLine], bool]:
    """Cost lines for one component in the workload's currency, and whether Azure sells it in that region at all."""
    svc = SERVICES[c.service]
    tier = svc.tiers[c.tier]
    fx = book.fx(w.currency)
    lines, sold = [], False
    for u in tier.usages:
        service = u.service or svc.api_service
        rows = [m for m in book.meters(service, u.pricing_region(c), u.extra or svc.api_extra) if u.match(m)]
        if rows:
            sold = True
        qty = max(0.0, u.quantity(c, w))
        if qty == 0:
            continue
        # Some regions publish the monthly free grant as a $0 first band; subtract it only once.
        grant_listed = len(rows) > 1 and any(m.tier_min == 0 and m.usd == 0 for m in rows)
        billable = max(0.0, qty - (0 if grant_listed else u.free))
        amount = tiered_cost(rows, billable) * fx
        lines.append(
            CostLine(
                service=c.service,
                tier=c.tier,
                region=c.region,
                item=u.label,
                quantity=round(qty, 2),
                unit=u.unit,
                amount=round(amount, 2),
                environment=environment,
            )
        )
    return lines, sold


def nonprod_components(design: Design, w: Workload) -> list[Component]:
    """A dev or test copy of the design: primary region only, cheapest tiers, one instance, small data."""
    out = []
    for c in design.components:
        if c.service in SHARED or c.service not in SERVICES or c.settings.get("replica") or c.region != design.region:
            continue
        settings: dict[str, float] = {}
        if c.service in ("postgres", "azure_sql"):
            settings["storage_gb"] = 32
        elif c.service in ("cosmos_db", "blob_storage"):
            settings["storage_gb"] = max(1.0, round((w.data_gb if c.service == "cosmos_db" else w.blob_gb) * 0.05, 1))
        elif c.service == "log_analytics":
            settings["ingest_gb"] = 1
        elif c.service == "azure_openai":
            settings["input_mtokens"] = round(w.llm_input_mtokens * 0.05, 2)
            settings["output_mtokens"] = round(w.llm_output_mtokens * 0.05, 2)
        elif c.service == "functions":
            settings["executions_month"] = 50_000
        tier = NONPROD_TIER.get(c.service, c.tier)
        out.append(Component(service=c.service, tier=tier, region=c.region, instances=1, purpose=c.purpose, settings=settings))
    return out


def database_backup_days(design: Design, w: Workload) -> dict[int, float]:
    """Effective backup retention of each primary database: point-in-time restore, extended by long-term retention."""
    vault = [c for c in design.components if c.service == "backup_vault"]
    vault_days = max((c.settings.get("retention_days", w.backup_retention_days) for c in vault), default=0)
    out = {}
    for c in design.components:
        if c.service not in ("postgres", "azure_sql") or c.settings.get("replica"):
            continue
        days = min(c.settings.get("backup_days", min(w.backup_retention_days, PITR_MAX_DAYS)), PITR_MAX_DAYS)
        days = max(days, vault_days if c.service == "postgres" else c.settings.get("ltr_days", 0))
        out[id(c)] = days
    return out


def assess(design: Design, w: Workload, book: PriceBook) -> Assessment:
    lines: list[CostLine] = []
    violations: list[Finding] = []
    findings: list[Finding] = []
    req: Requirements = requirements(w)
    allowed = RESIDENCY_REGIONS[w.residency]
    cur = w.currency

    def violate(pillar, rule, msg, fix="", accept_keys=(), source=""):
        # An exception counts when it names the rule code (e.g. "[RES-REGION] ...") or the thing it relaxes.
        accepted = _accepted(design, rule, *accept_keys) if accept_keys else None
        if accepted:
            findings.append(Finding(pillar=pillar, severity="high", rule=rule, message=f"{msg} Accepted: {accepted}", fix=fix, source=source))
        else:
            violations.append(Finding(pillar=pillar, severity="high", rule=rule, message=msg, fix=fix, source=source))

    def find(pillar, severity, rule, msg, fix="", source=""):
        findings.append(Finding(pillar=pillar, severity=severity, rule=rule, message=msg, fix=fix, source=source))

    # ---------------------------------------------------------------- validity and price
    prod = w.environment == "production"
    base_env = "prod" if prod else w.environments[0]
    valid: list[Component] = []
    for c in design.components:
        svc = SERVICES.get(c.service)
        if not svc or c.tier not in svc.tiers:
            violate("coverage", "UNKNOWN", f"'{c.service}/{c.tier}' is not in the catalogue.", "Use list_options to pick a real service and tier.")
            continue
        if c.region not in REGIONS:
            if not svc.global_service:
                violate("coverage", "REGION", f"{svc.name}: unknown region '{c.region}'.", "Use an Azure region code such as uksouth or westeurope.")
                continue
            c = c.model_copy(update={"region": design.region})  # a global service is billed by where its users are
        comp_lines, sold = price_component(c, w, book, base_env)
        if not sold:
            violate(
                "coverage",
                "NOT-SOLD",
                f"{svc.name} ({c.tier}) has no Azure price in {label(c.region)}: it is not sold there.",
                "Choose another region or service.",
            )
            continue
        lines += comp_lines
        valid.append(c)
    checked = design.model_copy(update={"components": valid})
    for env in w.environments:
        if env != base_env:
            for c in nonprod_components(checked, w):
                lines += price_component(c, w, book, env)[0]
    total = round(sum(line.amount for line in lines), 2)
    by_env = {env: round(sum(line.amount for line in lines if line.environment == env), 2) for env in w.environments}
    by_id: dict[str, list[Component]] = {}
    for c in valid:
        by_id.setdefault(c.service, []).append(c)
    data_stores = [c for c in valid if SERVICES[c.service].data_store and SERVICES[c.service].private_endpoint_ok]

    # ---------------------------------------------------------------- hard constraints: residency
    jur = JURISDICTIONS[w.residency]["label"]
    for c in valid:
        svc = SERVICES[c.service]
        llm_key = "llm" if "llm" in svc.roles else c.service
        if svc.global_service:
            if w.residency != "any":
                if c.service == "front_door":
                    find(
                        "residency",
                        "medium",
                        "RES-EDGE",
                        f"{svc.name}: {svc.residency_note}",
                        "Use Application Gateway in-region if traffic must not leave the country.",
                    )
                else:
                    find(
                        "residency",
                        "low",
                        "RES-GLOBAL",
                        f"{svc.name}: {svc.residency_note}",
                        "Choose a geography inside the boundary when you create it.",
                    )
        elif c.region not in allowed:
            violate(
                "residency",
                "RES-REGION",
                f"{svc.name} is in {label(c.region)}, outside the allowed regions for '{jur}' residency.",
                "Move it, or record an accepted exception with the reason.",
                accept_keys=(c.service, svc.name, svc.name.split(" (")[0], llm_key),
            )
        for p in req.region_rules:
            if not svc.global_service and c.region not in p.regions:
                violate(
                    "policy",
                    "POL-REGION",
                    f"{svc.name} is in {label(c.region)}, which the policy does not allow.",
                    f"Use one of: {', '.join(label(r) for r in p.regions[:6])}{'…' if len(p.regions) > 6 else ''}.",
                    accept_keys=(p.id, c.service, llm_key),
                    source=p.citation,
                )
        if c.service == "azure_openai":
            deployment = svc.tiers[c.tier].note.split(": ")[-1]
            zone = next((z for z, countries in DATA_ZONES.items() if c.region in ALL and ALL[c.region].country in countries), None)
            if deployment == "global" and (w.residency != "any" or w.pii):
                violate(
                    "residency",
                    "RES-AI-GLOBAL",
                    "Global deployment: prompts (and any personal information in them) may be processed in any Azure region.",
                    "Use a Data Zone or Regional deployment, or keep personal data out of prompts.",
                    accept_keys=("azure_openai", "llm", "global"),
                )
            elif deployment == "datazone" and w.residency != "any" and zone != w.residency:
                if w.residency in ("australia", "anz"):
                    find("residency", "medium", "RES-AI-ZONE", "Data Zone deployment: confirm the zone's countries meet your residency policy.")
                else:
                    violate(
                        "residency",
                        "RES-AI-ZONE",
                        f"Data Zone deployment processes prompts across the {zone.upper() + ' ' if zone else ''}zone, outside '{jur}'.",
                        svc.residency_note,
                        accept_keys=("azure_openai", "llm"),
                    )
        if "gdpr" in w.frameworks and w.pii and not svc.global_service and c.region in ALL and ALL[c.region].country not in GDPR_ADEQUATE:
            find(
                "compliance",
                "high",
                "COMP-GDPR-TRANSFER",
                f"{svc.name} in {label(c.region)} moves personal data to a country without an EU adequacy decision.",
                "Keep it in the EEA or an adequate country, or sign standard contractual clauses and record a transfer impact assessment.",
                CLAUSES["gdpr_transfer"],
            )
    if design.dr_region and design.dr_region not in allowed:
        violate(
            "residency",
            "RES-DR",
            f"Disaster-recovery region {label(design.dr_region)} is outside the allowed residency regions.",
            "Pick a DR region inside the residency boundary, or accept the exception.",
            accept_keys=("dr", "disaster"),
        )

    # ---------------------------------------------------------------- hard constraints: coverage, budget, stack
    for need in w.needs:
        roles = set(NEED_ROLES[need])
        if not any(roles & set(SERVICES[c.service].roles) for c in valid):
            violate("coverage", "NEED", f"Nothing in the design covers the need '{need}'.", "Add a component for it.")
    if w.budget_month is not None and total > w.budget_month:
        violate(
            "cost",
            "BUDGET",
            f"Estimated {money(total, cur)}/month is over the {money(w.budget_month, cur)} budget by {money(total - w.budget_month, cur)}.",
            "Downsize tiers, cut instances, drop an environment, or raise the budget.",
            accept_keys=("budget",),
        )
    used = {c.service for c in valid}
    for svc_id, text in req.stack["must"]:
        if svc_id not in used:
            violate(
                "coverage",
                "STACK-MUST",
                f"The constraint '{text}' asks for {SERVICES[svc_id].name}, which the design does not use.",
                "Use it.",
                ("stack",),
            )
    for svc_id, text in req.stack["forbid"]:
        if svc_id in used:
            violate(
                "coverage",
                "STACK-FORBID",
                f"The constraint '{text}' rules out {SERVICES[svc_id].name}, which the design uses.",
                "Replace it.",
                ("stack",),
            )
    for svc_id, text in req.stack["prefer"]:
        role = SERVICES[svc_id].roles[0]
        if svc_id not in used and any(role in SERVICES[s].roles for s in used):
            find("operations", "low", "STACK-PREFER", f"'{text}': {SERVICES[svc_id].name} would suit the team better than the chosen service.")

    # ---------------------------------------------------------------- hard constraints: accepted company policies
    fx = book.fx(cur)
    for p in req.spend_cap:
        cap = p.amount / book.fx(p.currency or cur) * fx
        if total > cap:
            violate(
                "policy", "POL-SPEND", f"Estimated {money(total, cur)}/month is over the policy's monthly cap of {money(cap, cur)}.",
                "Downsize or ask for an approved exception.", accept_keys=(p.id, "spend"), source=p.citation,
            )  # fmt: skip
    if req.approved.value is not None:
        for c in valid:
            if c.service not in req.approved.value:
                violate(
                    "policy",
                    "POL-SERVICE",
                    f"{SERVICES[c.service].name} is not on the approved services list.",
                    "Use an approved service, or get it approved.",
                    accept_keys=(c.service, "approved"),
                    source=req.approved.cite(),
                )
    if req.cmk.value:
        for c in data_stores:
            if not c.customer_managed_key and not c.settings.get("replica"):
                violate(
                    "policy", "POL-CMK", f"{SERVICES[c.service].name} is encrypted with a Microsoft-managed key.",
                    "Encrypt it with a customer-managed key from Key Vault.", accept_keys=("cmk", c.service), source=req.cmk.cite(),
                )  # fmt: skip
    public_policy = [p for p in w.accepted_policies if p.check == "no_public_endpoints"]
    if public_policy:
        for c in data_stores:
            if not c.private_endpoint:
                violate(
                    "policy", "POL-PUBLIC", f"{SERVICES[c.service].name} has a public endpoint.",
                    "Add a private endpoint and disable public network access.", accept_keys=("public", c.service), source=public_policy[0].citation,
                )  # fmt: skip
    if req.tags.value:
        missing = [t for t in req.tags.value if t not in design.tags]
        if missing:
            violate(
                "policy", "POL-TAGS", f"Required tags missing: {', '.join(missing)}.", "Add them to the design's tags.", ("tag",), req.tags.cite()
            )

    # backups and logs: a policy makes them a hard constraint; a framework or the workload makes them a finding
    backup_need = int(req.backup_days.value)
    policy_backup = [s for s in req.backup_days.sources if s != "workload backup policy"]
    backups = database_backup_days(checked, w)
    for c in valid:
        days = backups.get(id(c))
        if days is None or days >= backup_need:
            continue
        msg = f"{SERVICES[c.service].name} keeps backups for {days:g} days; {backup_need} are required."
        fix = "Add long-term retention (point-in-time restore stops at 35 days)." if backup_need > PITR_MAX_DAYS else "Raise the backup retention."
        if policy_backup:
            violate("policy", "POL-BACKUP", msg, fix, accept_keys=("backup",), source="; ".join(policy_backup))
        else:
            find("reliability", "high", "REL-BACKUP", msg, fix, "workload backup policy")
    log_need = int(req.log_days.value)
    log_kept = max((c.settings.get("retention_days", 30) for c in by_id.get("log_analytics", [])), default=0)
    if log_need > 30 and log_kept < log_need:
        policy_logs = [s for s in req.log_days.sources if s not in CLAUSES.values()]
        msg = f"Logs are kept for {log_kept:g} days; {log_need} are required."
        if policy_logs:
            violate("policy", "POL-LOGS", msg, "Set the workspace's total retention.", accept_keys=("log",), source="; ".join(policy_logs))
        else:
            find("compliance", "high", "COMP-LOGS", msg, "Set the workspace's total retention (long-term retention is cheap).", req.log_days.cite())

    # ---------------------------------------------------------------- Well-Architected review
    ha = prod and w.availability in ("high", "critical")
    store_ids = {id(c) for c in data_stores}
    for c in valid:
        svc, tier = SERVICES[c.service], SERVICES[c.service].tiers[c.tier]
        # A warm standby in the DR region is deliberately small, and read replicas cannot have HA of their own.
        standby = bool(design.dr_region) and c.region == design.dr_region and c.region != design.region
        if c.zone_redundant and not tier.zone_redundant_ok:
            find("reliability", "high", "REL-ZR-TIER", f"{svc.name} {c.tier} does not support zone redundancy.", "Move to a tier that does.")
        elif c.zone_redundant and not svc.global_service and not has_zones(c.region):
            find(
                "reliability",
                "high",
                "REL-ZR-REGION",
                f"{label(c.region)} has no availability zones, so {svc.name} cannot be zone redundant.",
                "Use a region with zones.",
            )
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
            not prod
            and (c.service, c.tier) not in DEV_ONLY_TIERS
            and c.service in ("app_service", "postgres", "redis", "azure_sql")
            and tier.production_grade
            and c.tier not in ("S1", "S2", "standard_c0")
        ):
            find("cost", "medium", "COST-DEV", f"{svc.name} {c.tier} is a production tier in a dev environment.", "Use a burstable or basic tier.")
        if req.private_data.value and not public_policy and id(c) in store_ids and not c.private_endpoint:
            find(
                "security",
                "high",
                "SEC-PRIVATE",
                f"{svc.name} holds sensitive data but is reachable from the internet.",
                "Add a private endpoint and disable public network access.",
                req.private_data.cite(),
            )

    # disaster recovery, driven by the availability target and the recovery objectives
    if req.dr != "none":
        gaps = dr_gaps(design, w, need_compute=req.dr == "warm")
        why = " and ".join(req.dr_sources)
        single = len({r for r in allowed if not ALL[r].restricted}) == 1
        if not design.dr_region:
            find(
                "reliability",
                "high",
                "REL-DR",
                f"One region cannot meet the {why}: a regional outage stops the whole application.",
                "Add a disaster-recovery region."
                + (f" {jur} has one Azure region open to all customers, so DR means leaving it or accepting the risk." if single else ""),
                why,
            )
        elif gaps:
            find(
                "reliability",
                "high",
                "REL-DR",
                f"{label(design.dr_region)} is named as the DR region but has {' and '.join(gaps)}, so nothing can take over.",
                "Add a warm standby and a database replica in the DR region, with Front Door to switch traffic."
                if req.dr == "warm"
                else "Replicate the database to the DR region.",
                why,
            )
        elif req.dr == "warm" and "front_door" not in by_id:
            find(
                "reliability",
                "medium",
                "REL-FAILOVER",
                "No global entry point: failing over to the DR region needs a manual DNS change.",
                "Put Front Door in front of both regions with health probes.",
                why,
            )
    if req.geo_backup.value:
        replicated = {c.service for c in valid if c.settings.get("replica")}
        for c in valid:
            if (
                c.service in ("postgres", "azure_sql")
                and not c.settings.get("replica")
                and c.service not in replicated
                and not c.settings.get("geo_backup")
            ):
                has_pair = bool(c.region in ALL and ALL[c.region].pair)
                find(
                    "reliability",
                    "high" if has_pair else "medium",
                    "REL-GEO-BACKUP",
                    f"{SERVICES[c.service].name} backups stay in {label(c.region)}: a regional outage loses them with the server."
                    + ("" if has_pair else f" {label(c.region)} has no paired region, so geo-redundant backup is not offered there."),
                    "Turn on geo-redundant backup." if has_pair else "Replicate to another allowed region, or accept the risk.",
                    req.geo_backup.cite(),
                )
    # security and compliance
    if "key_vault" not in by_id:
        find(
            "security", "high", "SEC-SECRETS", "No Key Vault: secrets and connection strings end up in app settings or code.",
            "Add Key Vault and use managed identities.",
        )  # fmt: skip
    waf = "app_gateway" in by_id or any(c.tier == "premium" for c in by_id.get("front_door", []))
    if req.waf.value and prod and any("web_app" in SERVICES[c.service].roles for c in valid) and not waf:
        find(
            "security",
            "high" if "pci_dss" in w.frameworks else "medium",
            "SEC-WAF",
            "Public web app handling sensitive data with no web application firewall.",
            "Put Application Gateway WAF v2 (in-region) or Front Door Premium in front.",
            req.waf.cite(),
        )
    if req.audit_logs.value and not any(c.settings.get("audit") for c in by_id.get("log_analytics", [])):
        find(
            "compliance",
            "high",
            "COMP-AUDIT",
            "Data stores do not send audit logs to a central workspace.",
            "Add diagnostic settings that stream every data store's audit logs to Log Analytics.",
            req.audit_logs.cite(),
        )
    if req.separate_test.value and not ({"dev", "test"} & set(w.environments)):
        find(
            "compliance", "medium", "COMP-TEST-ENV", "Only a production environment: changes cannot be tested before release.",
            "Add a test environment (it is priced as a smaller copy).", req.separate_test.cite(),
        )  # fmt: skip
    if "gdpr" in w.frameworks and w.pii and not w.retention_years:
        find(
            "compliance", "low", "COMP-RETENTION", "No retention period is set for personal data.",
            "Set how long records are kept; the design then deletes files after that period.", CLAUSES["gdpr_retention"],
        )  # fmt: skip
    if "hipaa" in w.frameworks:
        find(
            "compliance", "info", "COMP-BAA", "Health information needs Microsoft's HIPAA Business Associate Agreement.",
            "The BAA comes with Microsoft's Product Terms; confirm each service is in its scope in the Trust Center before go-live.",
        )  # fmt: skip
    if w.identity == "entra_external_id" and "entra_external_id" not in by_id:
        find(
            "security", "medium", "SEC-IDENTITY", "Customer sign-in was asked for but no identity service is in the design.", "Add Entra External ID."
        )
    elif w.identity == "none" and w.pii and prod and "web_app" in w.needs:
        find(
            "security",
            "low",
            "SEC-IDENTITY",
            "No identity provider chosen for a web app with personal data.",
            "Choose Entra ID (staff) or Entra External ID (customers).",
        )
    elif w.identity == "external":
        find(
            "residency",
            "info",
            "RES-IDP",
            "A third-party identity provider holds user profiles.",
            "Include its data location in the residency review.",
        )
    # operations and performance
    if "log_analytics" not in by_id:
        find("operations", "medium", "OPS-LOGS", "No central logging: incidents will be investigated blind.", "Add a Log Analytics workspace.")
    if w.integrations and not any("background_jobs" in SERVICES[c.service].roles for c in valid):
        find(
            "operations",
            "medium",
            "OPS-INTEGRATION",
            f"Calls to {', '.join(w.integrations)} run inside user requests: when the other side is slow or down, users wait or fail.",
            "Move integration calls to queued background jobs with retries.",
        )
    if w.peak_rps > 50 and w.read_ratio >= 0.5 and "relational_db" in w.needs and "redis" not in by_id:
        find(
            "performance",
            "medium",
            "PERF-CACHE",
            f"{w.peak_rps:g} requests/s at peak, {w.read_ratio:.0%} reads, all hitting the database.",
            "Add a cache.",
        )
    concurrent = w.concurrent()
    capacity, scale_out = 0, 0
    for c in valid:
        per = USERS_PER_INSTANCE.get((c.service, c.tier))
        if per and c.region == design.region:
            most = c.settings.get("max_instances") or (max(c.instances * 3, 3) if c.service == "container_apps" else billed_instances(c))
            capacity += int(per * most)
            scale_out = max(scale_out, int(most))
    if capacity and prod and capacity < concurrent:
        find(
            "performance",
            "medium",
            "PERF-INSTANCES",
            f"About {capacity:,} concurrent users fit at full scale-out, but {concurrent:,} are expected at peak.",
            "Raise the autoscale maximum or the tier.",
        )
    if prod and concurrent > 500 and scale_out > 1 and "web_app" in w.needs and "redis" not in by_id:
        find(
            "performance",
            "low",
            "PERF-SESSIONS",
            f"{concurrent:,} concurrent users across several instances share no session store.",
            "Add a cache for sessions.",
        )
    geos = {ALL[r].geography for r in (location_region(x) for x in w.user_locations) if r in ALL}
    if prod and "web_app" in w.needs and "front_door" not in by_id and (len(geos) > 1 or (w.monthly_users > 50_000 and w.residency == "any")):
        find(
            "performance",
            "medium" if len(geos) > 1 else "low",
            "PERF-EDGE",
            "Users on more than one continent and no edge network." if len(geos) > 1 else "Many users and no edge cache.",
            "Put Front Door in front for caching and nearby TLS termination.",
        )
    if prod and any(c.service in ("vm", "aks", "postgres", "azure_sql") for c in valid):
        find(
            "cost", "info", "COST-RESERVE", "Steady compute can be reserved for 1 or 3 years at a large discount.",
            "Price a reservation or savings plan once usage is stable.",
        )  # fmt: skip

    # ---------------------------------------------------------------- sizing: growth over 12 and 36 months
    sizing = {
        "data_gb_now": round(w.data_gb, 1),
        "data_gb_12m": round(w.data_gb_at(12), 1),
        "data_gb_36m": round(w.data_gb_at(36), 1),
        "concurrent_users": concurrent,
        "web_capacity_users": capacity,
        "log_retention_days": log_kept,
        "backup_retention_days": max(backups.values(), default=0),
    }
    for c in valid:
        if c.service == "postgres" and not c.settings.get("replica"):
            sizing["db_storage_gb"] = pg_storage_gb(c, w)
        if c.service == "azure_sql" and c.tier in SQL_MAX_GB:
            sizing["db_storage_gb"] = SQL_MAX_GB[c.tier]
    if "db_storage_gb" in sizing and w.data_growth_pct and w.data_gb:
        at36 = sizing["data_gb_36m"]
        if any(c.service == "azure_sql" and c.tier in SQL_MAX_GB for c in valid) and at36 > 250:
            find(
                "performance", "high", "PERF-CAPACITY", f"The database reaches {at36:,.0f} GB in 3 years; Standard S0-S2 stop at 250 GB.",
                "Use the General Purpose tier.",
            )  # fmt: skip
        elif sizing["db_storage_gb"] < at36:
            months = 12 * math.log(sizing["db_storage_gb"] / w.data_gb) / math.log(1 + w.data_growth_pct / 100)
            find(
                "operations",
                "low",
                "OPS-GROWTH",
                f"Provisioned storage ({sizing['db_storage_gb']:,.0f} GB) is full in about {months:.0f} months "
                f"at {w.data_growth_pct:g}% growth a year.",
                "Plan a storage increase (it can grow online but never shrink) or turn on storage auto-grow.",
            )
    return Assessment(
        total_month=total,
        currency=cur,
        fx_rate=fx,
        lines=lines,
        violations=violations,
        findings=findings,
        by_environment=by_env,
        sizing=sizing,
    )
