"""A markdown report of a design: what it costs, what it checks, the diagram, and why."""

from __future__ import annotations

from .assess import money
from .catalogue import SERVICES
from .diagram import mermaid
from .models import AVAILABILITY_TARGET, Assessment, Design, Workload
from .regions import JURISDICTIONS, label


def report(d: Design, w: Workload, a: Assessment, diagram_file: str = "") -> str:
    cur = w.currency
    per: dict[tuple, float] = {}
    for line in a.lines:
        if line.environment in ("prod", w.environments[0]):
            key = (line.service, line.tier, line.region)
            per[key] = per.get(key, 0) + line.amount
    out = [
        f"# {w.name}",
        "",
        w.description or "",
        "",
        "| | |",
        "|---|---|",
        f"| Monthly cost | **{money(a.total_month, cur)}**"
        + (f" (budget {money(w.budget_month, cur)})" if w.budget_month is not None else "")
        + " |",
        "| By environment | " + ", ".join(f"{env} {money(v, cur)}" for env, v in a.by_environment.items()) + " |",
        f"| Region | {label(d.region)}" + (f", disaster recovery in {label(d.dr_region)}" if d.dr_region else "") + " |",
        f"| Data residency | {JURISDICTIONS[w.residency]['label']} |",
        f"| Availability target | {AVAILABILITY_TARGET[w.availability]}"
        + (f", RPO {w.rpo_minutes} min" if w.rpo_minutes is not None else "")
        + (f", RTO {w.rto_minutes} min" if w.rto_minutes is not None else "")
        + " |",
        f"| Frameworks | {', '.join(f.upper().replace('_', ' ') for f in w.frameworks) or 'none'} |",
        f"| Hard violations | {len(a.violations)} |",
        "",
        "## Architecture",
        "",
    ]
    if diagram_file:
        out += [f"![Architecture diagram]({diagram_file})", ""]
    out += ["```mermaid", mermaid(d, w, theme=True), "```", ""]
    if d.rationale:
        out += [d.rationale, ""]
    if d.tradeoffs:
        out += ["Trade-offs:", ""] + [f"- {t}" for t in d.tradeoffs] + [""]
    if d.accepted_exceptions:
        out += ["Accepted exceptions:", ""] + [f"- {e}" for e in d.accepted_exceptions] + [""]
    out += [
        "## Components and monthly price",
        "",
        f"Prices from the Azure Retail Prices API in {cur} (US$1 = {a.fx_rate:g} {cur} at Azure's rate).",
        "",
        f"| Service | Tier | Region | Purpose | {cur} / month |",
        "|---|---|---|---|---:|",
    ]
    for c in d.components:
        name = SERVICES[c.service].name if c.service in SERVICES else c.service
        cost = per.get((c.service, c.tier, c.region), 0)
        out.append(f"| {name} | {c.tier} | {label(c.region) if c.service in SERVICES and not SERVICES[c.service].global_service else 'global'} "
                   f"| {c.purpose} | {money(cost, cur)} |")  # fmt: skip
    if len(a.by_environment) > 1:
        for env, v in a.by_environment.items():
            if env != "prod":
                out.append(f"| {env} environment | smaller copy | {label(d.region)} | cheapest tiers, one instance | {money(v, cur)} |")
    out += [f"| **Total** | | | | **{money(a.total_month, cur)}** |", ""]
    s = a.sizing
    if s:
        out += [
            "## Sizing",
            "",
            f"- Database: {s.get('data_gb_now', 0):,.0f} GB today, {s.get('data_gb_12m', 0):,.0f} GB in 12 months, "
            f"{s.get('data_gb_36m', 0):,.0f} GB in 36 months at {w.data_growth_pct:g}% a year"
            + (f"; {s['db_storage_gb']:,.0f} GB provisioned." if "db_storage_gb" in s else "."),
            f"- Peak concurrency: {s.get('concurrent_users', 0):,} users; the web tier serves about "
            f"{s.get('web_capacity_users', 0):,} at full scale-out.",
            f"- Backups kept {s.get('backup_retention_days', 0):g} days; logs kept {s.get('log_retention_days', 0):g} days.",
            "",
        ]
    if w.accepted_policies:
        out += ["## Company policy rules enforced", ""]
        out += [f"- **{p.check.replace('_', ' ')}** ({p.id}): {p.citation}" for p in w.accepted_policies]
        out.append("")
    out += ["## Checks", ""]
    if a.violations:
        out += [f"- **Violation {v.rule}**: {v.message} Fix: {v.fix}" + (f" Source: {v.source}" if v.source else "") for v in a.violations]
    else:
        out.append("- No hard violations: residency, budget, coverage, stack and every accepted policy rule hold.")
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    for f in sorted(a.findings, key=lambda f: order[f.severity]):
        out.append(
            f"- {f.severity} · {f.pillar} · **{f.rule}**: {f.message}"
            + (f" Fix: {f.fix}" if f.fix else "")
            + (f" *Source: {f.source}*" if f.source else "")
        )
    out.append("")
    return "\n".join(out)
