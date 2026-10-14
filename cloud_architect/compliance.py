"""What a workload's frameworks, policies, recovery targets and stack constraints demand of a design.

The rule-based architect builds to these requirements and the assessor checks any design against
them, so both read the same source. Each requirement keeps the reasons behind it (a framework
clause or an accepted policy rule) so findings can cite them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import PolicyRule, Workload
from .regions import RESIDENCY_REGIONS

# Framework clauses behind each requirement, quoted in findings.
CLAUSES = {
    "pci_waf": "PCI DSS v4.0 req. 6.4.2: public-facing web applications are protected by an automated solution that detects and prevents attacks",
    "pci_network": "PCI DSS v4.0 req. 1.3: network access to and from the cardholder data environment is restricted",
    "pci_logs": "PCI DSS v4.0 req. 10.5.1: audit log history is kept for at least 12 months, with the last three months immediately available",
    "hipaa_audit": "HIPAA Security Rule 164.312(b): audit controls record and examine activity in systems that contain health information",
    "hipaa_retention": "HIPAA 164.316(b)(2): required records are kept for six years",
    "hipaa_backup": "HIPAA 164.308(a)(7): a data backup plan and a disaster recovery plan",
    "hipaa_access": "HIPAA 164.312(e)(1): health information is protected from unauthorised access over networks",
    "gdpr_transfer": (
        "GDPR Chapter V: personal data leaves the EEA only for adequate countries or under safeguards such as standard contractual clauses"
    ),
    "gdpr_retention": "GDPR art. 5(1)(e): personal data is kept no longer than necessary",
    "iso_separation": "ISO/IEC 27001:2022 Annex A 8.31: development, test and production environments are separated",
    "iso_logs": "ISO/IEC 27001:2022 Annex A 8.15: logs of activities are produced, stored and protected",
    "soc2_change": "SOC 2 CC8.1: changes are tested before they reach production",
    "soc2_logs": "SOC 2 CC7.2: system activity is monitored; auditors examine a 12-month period",
}

DR_WARM_RTO = 240  # minutes: restoring a geo-backup into another region takes hours, so a tighter RTO needs a running standby
DR_REPLICA_RPO = 60  # minutes: geo-redundant backups can lose up to about an hour; less than that needs a live replica
GEO_BACKUP_RPO = 24 * 60  # minutes: a day of loss is covered by geo-redundant backups
PITR_MAX_DAYS = 35  # PostgreSQL and Azure SQL point-in-time restore keep at most 35 days


@dataclass
class Need:
    value: object
    sources: list[str] = field(default_factory=list)

    def cite(self) -> str:
        return "; ".join(dict.fromkeys(self.sources))


@dataclass
class Requirements:
    allowed: set[str]
    region_rules: list[PolicyRule]
    log_days: Need
    backup_days: Need
    private_data: Need
    waf: Need
    cmk: Need
    audit_logs: Need
    separate_test: Need
    geo_backup: Need
    dr: str  # "none" | "replica" | "warm"
    dr_sources: list[str]
    approved: Need  # set of catalogue ids, or None
    spend_cap: list[PolicyRule]
    tags: Need
    stack: dict[str, list[tuple[str, str]]]  # must / forbid / prefer -> [(service or need, constraint text)]


def _max(items: list[tuple[int, str]], default: int) -> Need:
    best = max([d for d, _ in items], default=default)
    return Need(max(best, default), [s for d, s in items if d == best])


def stack_rules(constraints: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Read stack constraints written in plain words: 'must use PostgreSQL', 'no Kubernetes', '.NET team'."""
    out: dict[str, list[tuple[str, str]]] = {"must": [], "forbid": [], "prefer": []}
    for text in constraints:
        t = text.lower()
        negative = bool(re.search(r"\b(no|not|avoid|without|never)\b", t))
        strong = bool(re.search(r"\b(must|only|require[sd]?|mandated?|standard(ise|ize)d? on)\b", t))
        found = []
        if re.search(r"postgres", t):
            found.append("postgres")
        if re.search(r"sql server|mssql|azure sql|t-sql", t):
            found.append("azure_sql")
        if re.search(r"kubernetes|\baks\b|\bk8s\b", t):
            found.append("aks")
        if re.search(r"virtual machines?|\bvms?\b", t):
            found.append("vm")
        if re.search(r"container apps?|docker|containers?", t) and "aks" not in found:
            found.append("container_apps")
        if re.search(r"cosmos|mongodb|document database", t):
            found.append("cosmos_db")
        if re.search(r"\.net\b|dotnet|c#", t) and not found:
            out["prefer"].append(("azure_sql", text))  # SQL Server is the database a .NET team usually knows best
            continue
        for svc in found:
            out["forbid" if negative else ("must" if strong else "prefer")].append((svc, text))
    return out


def requirements(w: Workload) -> Requirements:
    prod = w.environment == "production"
    fw = set(w.frameworks)
    pol = w.accepted_policies

    allowed = set(RESIDENCY_REGIONS[w.residency])
    region_rules = [p for p in pol if p.check == "allowed_regions" and p.regions]
    for p in region_rules:
        allowed &= set(p.regions)

    logs: list[tuple[int, str]] = []
    if "pci_dss" in fw:
        logs.append((365, CLAUSES["pci_logs"]))
    if "hipaa" in fw:
        logs.append((6 * 365, CLAUSES["hipaa_retention"]))
    if "soc2" in fw:
        logs.append((365, CLAUSES["soc2_logs"]))
    logs += [(p.days or 0, p.citation) for p in pol if p.check == "min_log_retention_days"]

    backups = [(w.backup_retention_days, "workload backup policy")] + [
        (p.days or 0, p.citation) for p in pol if p.check == "min_backup_retention_days"
    ]

    private = []
    if w.pii and prod:
        private.append("personal data in production")
    if "pci_dss" in fw:
        private.append(CLAUSES["pci_network"])
    if "hipaa" in fw:
        private.append(CLAUSES["hipaa_access"])
    private += [p.citation for p in pol if p.check == "no_public_endpoints"]

    waf = []
    if "pci_dss" in fw:
        waf.append(CLAUSES["pci_waf"])
    elif w.pii and prod and "web_app" in w.needs:
        waf.append("public web app handling personal data")

    audit = []
    if "hipaa" in fw:
        audit.append(CLAUSES["hipaa_audit"])
    if "pci_dss" in fw:
        audit.append(CLAUSES["pci_logs"])
    if "iso27001" in fw:
        audit.append(CLAUSES["iso_logs"])

    separate = []
    if "iso27001" in fw:
        separate.append(CLAUSES["iso_separation"])
    if "soc2" in fw:
        separate.append(CLAUSES["soc2_change"])

    dr, dr_sources = "none", []
    if prod and w.availability == "critical":
        dr, dr_sources = "warm", ["99.99% availability target"]
    if prod and w.rto_minutes is not None and w.rto_minutes <= DR_WARM_RTO:
        dr, dr_sources = "warm", dr_sources + [f"RTO of {w.rto_minutes} minutes"]
    if prod and dr == "none" and w.rpo_minutes is not None and w.rpo_minutes < DR_REPLICA_RPO:
        dr, dr_sources = "replica", [f"RPO of {w.rpo_minutes} minutes"]

    geo = []
    if w.geo_backup:
        geo.append("workload backup policy: copy backups to a second region")
    if prod and w.rpo_minutes is not None and w.rpo_minutes <= GEO_BACKUP_RPO:
        geo.append(f"RPO of {w.rpo_minutes} minutes")
    if "hipaa" in fw:
        geo.append(CLAUSES["hipaa_backup"])

    approved = [p for p in pol if p.check == "approved_services" and p.services]
    approved_set = set.intersection(*(set(p.services) for p in approved)) if approved else None

    return Requirements(
        allowed=allowed,
        region_rules=region_rules,
        log_days=_max(logs, 30),
        backup_days=_max(backups, 7),
        private_data=Need(bool(private), private),
        waf=Need(bool(waf), waf),
        cmk=Need(any(p.check == "customer_managed_keys" for p in pol), [p.citation for p in pol if p.check == "customer_managed_keys"]),
        audit_logs=Need(bool(audit) and prod, audit),
        separate_test=Need(bool(separate) and prod, separate),
        geo_backup=Need(bool(geo), geo),
        dr=dr,
        dr_sources=dr_sources,
        approved=Need(approved_set, [p.citation for p in approved]),
        spend_cap=[p for p in pol if p.check == "max_monthly_spend" and p.amount],
        tags=Need(sorted({t for p in pol if p.check == "required_tags" for t in p.tags}), [p.citation for p in pol if p.check == "required_tags"]),
        stack=stack_rules(w.constraints),
    )
