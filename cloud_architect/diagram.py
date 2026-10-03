"""A Mermaid diagram of a design: one box per region, so anything that crosses the residency
boundary is visible at a glance. Renders on GitHub and in the web UI."""

from __future__ import annotations

from .catalogue import SERVICES
from .models import REGIONS, RESIDENCY_REGIONS, Design, Workload

COMPUTE = ("app_service", "container_apps", "aks", "vm", "functions")


def _label(c) -> str:
    svc = SERVICES[c.service]
    extra = [c.tier]
    if c.instances > 1:
        extra.append(f"x{c.instances}")
    if c.zone_redundant:
        extra.append("zone-redundant")
    if c.private_endpoint:
        extra.append("private")
    if c.settings.get("replica"):
        extra.append("replica")
    return f"{svc.name}<br/><small>{' · '.join(extra)}</small>"


def mermaid(design: Design, w: Workload) -> str:
    allowed = RESIDENCY_REGIONS[w.residency]
    comps = [c for c in design.components if c.service in SERVICES]
    ids = {id(c): f"c{n}" for n, c in enumerate(comps)}
    lines = ["flowchart LR", '  users(["Users"])']
    by_region: dict[str, list] = {}
    for c in comps:
        by_region.setdefault("global" if SERVICES[c.service].global_service else c.region, []).append(c)
    for region, items in by_region.items():
        title = "Global edge" if region == "global" else REGIONS.get(region, region)
        if region == design.dr_region and region != design.region:
            title += " (DR)"
        outside = region != "global" and region not in allowed
        key = region.replace("-", "_")
        lines.append(f'  subgraph {key}["{title}{" (outside residency boundary)" if outside else ""}"]')
        lines += [f'    {ids[id(c)]}["{_label(c)}"]' for c in items]
        lines.append("  end")
        if outside:
            lines.append(f"  style {key} fill:#fdecea,stroke:#c0392b,stroke-width:2px")

    def edge(a, b, arrow="-->"):
        lines.append(f"  {ids[id(a)]} {arrow} {ids[id(b)]}")

    replica = {id(c) for c in comps if c.settings.get("replica")}
    front = [c for c in comps if c.service == "front_door"]
    gateways = [c for c in comps if c.service == "app_gateway"]
    compute = [c for c in comps if c.service in COMPUTE]
    others = [c for c in comps if c.service not in COMPUTE + ("front_door", "app_gateway", "log_analytics")]

    def entry(region):  # where traffic lands in a region: its gateway, else its compute
        return [g for g in gateways if g.region == region] or [c for c in compute if c.region == region]

    first = front or gateways or [c for c in compute if c.region == design.region] or compute
    lines += [f"  users --> {ids[id(c)]}" for c in first]
    for f in front:
        for region in dict.fromkeys(c.region for c in compute):
            for target in entry(region):
                edge(f, target)
    for g in gateways:
        for c in compute:
            if c.region == g.region:
                edge(g, c)
    for c in compute:
        for o in others:
            # Each region's compute uses its own data; the primary region also uses shared services elsewhere (e.g. the LLM).
            if o.region == c.region or (c.region == design.region and id(o) not in replica):
                edge(c, o)
    for r in (c for c in comps if id(c) in replica):
        source = next((c for c in comps if c.service == r.service and id(c) not in replica), None)
        if source:
            lines.append(f"  {ids[id(source)]} -. replicates .-> {ids[id(r)]}")
    for lg in (c for c in comps if c.service == "log_analytics"):
        for c in compute:
            edge(c, lg, "-.->")
    return "\n".join(lines)
