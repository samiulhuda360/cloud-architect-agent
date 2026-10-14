"""Architecture diagrams for a design, from one graph model rendered three ways:

- Mermaid, for the web UI and the markdown report (renders on GitHub);
- draw.io (`.drawio`), using draw.io's built-in Azure shape library by stencil name, so engineers can
  open and edit it (no Microsoft icon files are bundled here);
- SVG, a self-contained picture for the report.

The graph shows the users and where they are, identity, the global edge, one box per region (primary,
DR, and any region a service had to use), availability zones, the private network boundary with its
private endpoints, data flows, replication and backups, outside systems, and anything placed outside
the residency boundary.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from .catalogue import SERVICES
from .compliance import requirements
from .models import Component, Design, Workload
from .regions import label

COMPUTE = ("app_service", "container_apps", "aks", "vm", "functions")
DATA = ("postgres", "azure_sql", "cosmos_db", "blob_storage", "redis", "ai_search")
SHARED = ("key_vault", "log_analytics", "backup_vault")
EDGE = ("front_door", "app_gateway")
TEAL_INIT = (
    '%%{init: {"theme": "base", "themeVariables": {"primaryColor": "#e6f2ef", "primaryBorderColor": "#2f6f68", '
    '"primaryTextColor": "#13302c", "lineColor": "#5f7471", "secondaryColor": "#f3efe6", "tertiaryColor": "#f7f8f6"}}}%%'
)
# draw.io's built-in Azure library (Azure icons in the "Azure" shape set), referenced by stencil path.
STENCIL = {
    "app_service": "compute/App_Services.svg",
    "container_apps": "other/Container_App_Environments.svg",
    "functions": "compute/Function_Apps.svg",
    "aks": "compute/Kubernetes_Services.svg",
    "vm": "compute/Virtual_Machine.svg",
    "postgres": "databases/Azure_Database_PostgreSQL_Server.svg",
    "azure_sql": "databases/SQL_Database.svg",
    "cosmos_db": "databases/Azure_Cosmos_DB.svg",
    "blob_storage": "storage/Storage_Accounts.svg",
    "redis": "databases/Cache_Redis.svg",
    "ai_search": "app_services/Search_Services.svg",
    "azure_openai": "ai_machine_learning/Azure_OpenAI.svg",
    "key_vault": "security/Key_Vaults.svg",
    "log_analytics": "management_governance/Log_Analytics_Workspaces.svg",
    "backup_vault": "other/Backup_Vault.svg",
    "front_door": "networking/Front_Doors.svg",
    "app_gateway": "networking/Application_Gateways.svg",
    "entra_external_id": "identity/External_Identities.svg",
    "entra_id": "identity/Azure_Active_Directory.svg",
    "users": "identity/Users.svg",
    "external": "integration/Software_as_a_Service.svg",
    "private_endpoint": "networking/Private_Endpoint.svg",
}
KIND_COLOUR = {
    "compute": "#2f6f68",
    "data": "#1f5f8b",
    "edge": "#7a5c12",
    "shared": "#5f7471",
    "ai": "#8a4b2a",
    "external": "#6b6b6b",
    "identity": "#2f6f68",
}


@dataclass
class Node:
    id: str
    title: str
    detail: str
    stencil: str
    kind: str
    group: str  # id of the group it sits in ("" = outside every group)


@dataclass
class Group:
    id: str
    title: str
    kind: str  # region | dr | other | global | private | external
    parent: str = ""
    outside: bool = False


@dataclass
class Edge:
    src: str
    dst: str
    text: str = ""
    dashed: bool = False


@dataclass
class Graph:
    nodes: list[Node] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def node(self, nid: str) -> Node:
        return next(n for n in self.nodes if n.id == nid)


def _detail(c: Component) -> str:
    extra = [c.tier]
    if c.instances > 1:
        extra.append(f"x{c.instances}")
    if c.settings.get("max_instances"):
        extra.append(f"autoscale to {c.settings['max_instances']:g}")
    if c.zone_redundant:
        extra.append("3 zones")
    if c.private_endpoint:
        extra.append("private endpoint")
    if c.customer_managed_key:
        extra.append("customer key")
    if c.settings.get("replica"):
        extra.append("replica")
    if c.settings.get("geo_backup"):
        extra.append("geo backup")
    if c.service == "log_analytics" and c.settings.get("retention_days", 30) > 30:
        extra.append(f"kept {c.settings['retention_days']:g} days")
    return " · ".join(extra)


def _kind(service: str) -> str:
    if service in COMPUTE:
        return "compute"
    if service in DATA:
        return "data"
    if service in EDGE:
        return "edge"
    if service == "azure_openai":
        return "ai"
    if service == "entra_external_id":
        return "identity"
    return "shared"


def build(design: Design, w: Workload) -> Graph:
    g = Graph()
    allowed = requirements(w).allowed
    comps = [c for c in design.components if c.service in SERVICES]
    ids = {id(c): f"c{n}" for n, c in enumerate(comps)}
    replica = {id(c) for c in comps if c.settings.get("replica")}

    where = ", ".join(w.user_locations) if w.user_locations else ""
    g.nodes.append(Node("users", "Users", where or f"{w.monthly_users:,} a month", STENCIL["users"], "external", ""))
    if w.identity == "entra_id":
        g.nodes.append(Node("idp", "Microsoft Entra ID", "staff sign-in", STENCIL["entra_id"], "identity", ""))
    elif w.identity == "external":
        g.nodes.append(Node("idp", "Identity provider", "third party", STENCIL["external"], "identity", ""))

    regions: list[str] = []
    for c in comps:
        if not SERVICES[c.service].global_service and c.region not in regions:
            regions.append(c.region)
    regions.sort(key=lambda r: (r != design.region, r != design.dr_region, r))
    if any(SERVICES[c.service].global_service for c in comps):
        g.groups.append(Group("global", "Global edge", "global"))
    for r in regions:
        zones = any(c.zone_redundant for c in comps if c.region == r)
        role = " · primary" if r == design.region else (" · disaster recovery" if r == design.dr_region else "")
        outside = r not in allowed
        title = label(r) + role + (" · 3 availability zones" if zones else "") + (" · outside the residency boundary" if outside else "")
        kind = "region" if r == design.region else ("dr" if r == design.dr_region else "other")
        g.groups.append(Group(f"r_{r}", title, kind, outside=outside))
        if any(c.private_endpoint for c in comps if c.region == r):
            g.groups.append(Group(f"p_{r}", "Private network · private endpoints", "private", parent=f"r_{r}"))
    for c in comps:
        svc = SERVICES[c.service]
        if svc.global_service:
            group = "global"
        elif c.private_endpoint:
            group = f"p_{c.region}"
        else:
            group = f"r_{c.region}"
        g.nodes.append(Node(ids[id(c)], svc.name.split(" (")[0], _detail(c), STENCIL.get(c.service, ""), _kind(c.service), group))
    if w.integrations:
        g.groups.append(Group("ext", "Outside systems", "external"))
        for n, name in enumerate(w.integrations):
            g.nodes.append(Node(f"x{n}", name, "integration", STENCIL["external"], "external", "ext"))

    def edge(a, b, text="", dashed=False):
        g.edges.append(Edge(a, b, text, dashed))

    front = [c for c in comps if c.service == "front_door"]
    gateways = [c for c in comps if c.service == "app_gateway"]
    compute = [c for c in comps if c.service in COMPUTE]
    web = [c for c in compute if c.service != "functions"]
    stores = [c for c in comps if c.service in DATA or c.service == "azure_openai"]

    def entry(region):
        return [x for x in gateways if x.region == region] or [x for x in web if x.region == region]

    first = front or gateways or [c for c in web if c.region == design.region] or compute[:1]
    for f in first:
        edge("users", ids[id(f)], "HTTPS")
    for f in front:
        for region in dict.fromkeys(c.region for c in web):
            for target in entry(region):
                edge(ids[id(f)], ids[id(target)], "primary" if region == design.region else "failover", dashed=region != design.region)
    for gw in gateways:
        for c in web:
            if c.region == gw.region:
                edge(ids[id(gw)], ids[id(c)])
    ext_id = [c for c in comps if c.service == "entra_external_id"]
    if ext_id:
        edge("users", ids[id(ext_id[0])], "sign in", dashed=True)
    elif any(n.id == "idp" for n in g.nodes):
        edge("users", "idp", "sign in", dashed=True)
    for c in compute:
        for o in stores:
            if id(o) in replica:
                continue
            if o.region == c.region or (c.region == design.region and o.service == "azure_openai"):
                if c.service == "functions" and o.service in ("redis", "ai_search", "azure_openai"):
                    continue
                edge(ids[id(c)], ids[id(o)], "prompts" if o.service == "azure_openai" else "")
    for r in (c for c in comps if id(c) in replica):
        source = next((c for c in comps if c.service == r.service and id(c) not in replica), None)
        if source:
            edge(ids[id(source)], ids[id(r)], "geo-replication", dashed=True)
    for vault in (c for c in comps if c.service == "backup_vault"):
        for db in (c for c in comps if c.service == "postgres" and id(c) not in replica):
            edge(ids[id(db)], ids[id(vault)], "weekly backups", dashed=True)
    jobs = [c for c in compute if c.service == "functions"]
    for n, _ in enumerate(w.integrations):
        if jobs:
            edge(ids[id(jobs[0])], f"x{n}", "sync, retried")
        elif web:
            edge(ids[id(web[0])], f"x{n}", "API calls")
    logs = [c for c in comps if c.service == "log_analytics"]
    for lg in logs:
        for c in compute:
            if c.region == lg.region or c.region == design.region:
                edge(ids[id(c)], ids[id(lg)], "logs", dashed=True)
                break
    return g


# ---------------------------------------------------------------- Mermaid
def _mm(text: str) -> str:
    return text.replace('"', "'")


def mermaid(design: Design, w: Workload, theme: bool = False) -> str:
    g = build(design, w)
    lines = [TEAL_INIT] if theme else []
    lines += ["flowchart LR"]
    placed = set()

    def node_line(n: Node, indent: str) -> str:
        shape = ('(["', '"])') if n.id == "users" else ('["', '"]')
        return f"{indent}{n.id}{shape[0]}{_mm(n.title)}<br/><small>{_mm(n.detail)}</small>{shape[1]}"

    for n in g.nodes:
        if not n.group:
            lines.append(node_line(n, "  "))
            placed.add(n.id)

    def emit(group: Group, indent: str):
        lines.append(f'{indent}subgraph {group.id}["{_mm(group.title)}"]')
        for n in g.nodes:
            if n.group == group.id:
                lines.append(node_line(n, indent + "  "))
                placed.add(n.id)
        for child in g.groups:
            if child.parent == group.id:
                emit(child, indent + "  ")
        lines.append(f"{indent}end")

    for group in g.groups:
        if not group.parent:
            emit(group, "  ")
    for e in g.edges:
        arrow = "-.->" if e.dashed else "-->"
        lines.append(f"  {e.src} {arrow}|{_mm(e.text)}| {e.dst}" if e.text else f"  {e.src} {arrow} {e.dst}")
    styles = {
        "region": "fill:#eef6f4,stroke:#2f6f68,stroke-width:2px",
        "dr": "fill:#f3efe6,stroke:#8a7a4f,stroke-width:2px",
        "other": "fill:#f7f8f6,stroke:#5f7471",
        "global": "fill:#f7f8f6,stroke:#5f7471,stroke-dasharray:4 3",
        "private": "fill:#ffffff,stroke:#1f5f8b,stroke-dasharray:5 4",
        "external": "fill:#f7f7f7,stroke:#8a8a8a,stroke-dasharray:3 3",
    }
    for group in g.groups:
        style = "fill:#fdecea,stroke:#b8322a,stroke-width:2px" if group.outside else styles[group.kind]
        lines.append(f"  style {group.id} {style}")
    return "\n".join(lines)


# ---------------------------------------------------------------- layout shared by draw.io and SVG
NODE_W, NODE_H, GAP_X, GAP_Y, PAD, HEAD = 170, 74, 28, 34, 18, 34


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float


def _rows(nodes: list[Node]) -> list[list[Node]]:
    order = {"edge": 0, "identity": 1, "compute": 1, "ai": 2, "data": 2, "shared": 3, "external": 1}
    rows: dict[int, list[Node]] = {}
    for n in nodes:
        rows.setdefault(order.get(n.kind, 3), []).append(n)
    return [rows[k] for k in sorted(rows)]


def layout(g: Graph) -> tuple[dict[str, Box], dict[str, Box], float, float]:
    """Absolute boxes for groups and nodes: free nodes in a left column, then each top-level group as a column."""
    nodes: dict[str, Box] = {}
    groups: dict[str, Box] = {}
    x = 20.0
    free = [n for n in g.nodes if not n.group]
    for k, n in enumerate(free):
        nodes[n.id] = Box(x, 60 + k * (NODE_H + GAP_Y * 2), NODE_W, NODE_H)
    x += NODE_W + GAP_X * 2 if free else 0
    height = 0.0
    rank = {"global": 0, "region": 1, "external": 2, "dr": 3, "other": 4}
    for group in sorted((gr for gr in g.groups if not gr.parent), key=lambda gr: rank[gr.kind]):
        top = 20.0
        own = [n for n in g.nodes if n.group == group.id]
        child = next((c for c in g.groups if c.parent == group.id), None)
        inner = [n for n in g.nodes if child and n.group == child.id]
        rows = _rows(own)
        width = max([len(r) for r in rows] + [len(inner) if inner else 0, 2]) * (NODE_W + GAP_X) - GAP_X + 2 * PAD
        if inner:
            width = max(width, len(inner) * (NODE_W + GAP_X) - GAP_X + 4 * PAD)
        y = top + HEAD
        placed_rows = 0
        for r in rows:
            if r[0].kind == "shared" and inner:
                # the private network sits between the compute and the shared services
                cy = y
                for k, n in enumerate(inner):
                    nodes[n.id] = Box(x + 2 * PAD + k * (NODE_W + GAP_X), cy + HEAD, NODE_W, NODE_H)
                groups[child.id] = Box(x + PAD, cy, width - 2 * PAD, HEAD + NODE_H + PAD)
                y = cy + HEAD + NODE_H + PAD + GAP_Y
                inner = []
            for k, n in enumerate(r):
                nodes[n.id] = Box(x + PAD + k * (NODE_W + GAP_X), y, NODE_W, NODE_H)
            y += NODE_H + GAP_Y
            placed_rows += 1
        if inner:
            for k, n in enumerate(inner):
                nodes[n.id] = Box(x + 2 * PAD + k * (NODE_W + GAP_X), y + HEAD, NODE_W, NODE_H)
            groups[child.id] = Box(x + PAD, y, width - 2 * PAD, HEAD + NODE_H + PAD)
            y += HEAD + NODE_H + PAD + GAP_Y
        groups[group.id] = Box(x, top, width, y - top - GAP_Y + PAD)
        height = max(height, y + PAD)
        x += width + GAP_X * 2
    return groups, nodes, x, max(height, 60 + len(free) * (NODE_H + GAP_Y * 2))


# ---------------------------------------------------------------- draw.io
def drawio(design: Design, w: Workload) -> str:
    g = build(design, w)
    groups, nodes, width, height = layout(g)
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    fill = {"region": "#eef6f4", "dr": "#f3efe6", "other": "#f7f8f6", "global": "#f7f8f6", "private": "#ffffff", "external": "#f7f7f7"}
    stroke = {"region": "#2f6f68", "dr": "#8a7a4f", "other": "#5f7471", "global": "#5f7471", "private": "#1f5f8b", "external": "#8a8a8a"}
    for gr in g.groups:
        b = groups[gr.id]
        parent = groups.get(gr.parent)
        x, y = (b.x - parent.x, b.y - parent.y) if parent else (b.x, b.y)
        f, s = ("#fdecea", "#b8322a") if gr.outside else (fill[gr.kind], stroke[gr.kind])
        dashed = 1 if gr.kind in ("global", "private", "external") else 0
        style = (
            f"rounded=1;arcSize=4;whiteSpace=wrap;html=1;container=1;collapsible=0;fillColor={f};strokeColor={s};dashed={dashed};"
            "verticalAlign=top;align=left;spacingLeft=10;spacingTop=6;fontStyle=1;fontSize=12;fontColor=#13302c;"
        )
        cells.append(
            f'<mxCell id="{gr.id}" value="{html.escape(gr.title)}" style="{style}" vertex="1" parent="{gr.parent or 1}">'
            f'<mxGeometry x="{x:.0f}" y="{y:.0f}" width="{b.w:.0f}" height="{b.h:.0f}" as="geometry"/></mxCell>'
        )
    for n in g.nodes:
        b = nodes[n.id]
        parent = groups.get(n.group)
        x, y = (b.x - parent.x, b.y - parent.y) if parent else (b.x, b.y)
        icon = f"image=img/lib/azure2/{n.stencil};" if n.stencil else ""
        style = (
            f"image;aspect=fixed;html=1;points=[];align=center;fontSize=11;{icon}labelPosition=center;"
            "verticalLabelPosition=bottom;verticalAlign=top;fontColor=#13302c;"
        )
        value = html.escape(f"<b>{html.escape(n.title)}</b><br><font style='font-size:10px' color='#5f7471'>{html.escape(n.detail)}</font>")
        cells.append(
            f'<mxCell id="{n.id}" value="{value}" style="{style}" vertex="1" parent="{n.group or 1}">'
            f'<mxGeometry x="{x + (NODE_W - 40) / 2:.0f}" y="{y:.0f}" width="40" height="40" as="geometry"/></mxCell>'
        )
    for k, e in enumerate(g.edges):
        style = f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;strokeColor=#5f7471;fontSize=10;dashed={int(e.dashed)};"
        cells.append(
            f'<mxCell id="e{k}" value="{html.escape(e.text)}" style="{style}" edge="1" parent="1" source="{e.src}" target="{e.dst}">'
            '<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
    title = html.escape(w.name)
    return (
        f'<mxfile host="cloud-architect-agent" type="device"><diagram id="architecture" name="{title}">'
        f'<mxGraphModel dx="{width:.0f}" dy="{height:.0f}" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" '
        f'page="1" pageScale="1" pageWidth="{max(width, 850):.0f}" pageHeight="{max(height, 1100):.0f}" math="0" shadow="0">'
        f"<root>{''.join(cells)}</root></mxGraphModel></diagram></mxfile>\n"
    )


# ---------------------------------------------------------------- SVG
def svg(design: Design, w: Workload) -> str:
    g = build(design, w)
    groups, nodes, width, height = layout(g)
    fill = {"region": "#eef6f4", "dr": "#f3efe6", "other": "#f7f8f6", "global": "#f7f8f6", "private": "#ffffff", "external": "#f7f7f7"}
    stroke = {"region": "#2f6f68", "dr": "#8a7a4f", "other": "#5f7471", "global": "#5f7471", "private": "#1f5f8b", "external": "#8a8a8a"}
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height + 20:.0f}" viewBox="0 0 {width:.0f} {height + 20:.0f}" '
        'font-family="Segoe UI, Helvetica, Arial, sans-serif">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#5f7471"/></marker></defs>',
        f'<rect width="{width:.0f}" height="{height + 20:.0f}" fill="#ffffff"/>',
    ]
    for gr in sorted(g.groups, key=lambda x: bool(x.parent)):
        b = groups[gr.id]
        f, s = ("#fdecea", "#b8322a") if gr.outside else (fill[gr.kind], stroke[gr.kind])
        dash = ' stroke-dasharray="5 4"' if gr.kind in ("global", "private", "external") else ""
        out.append(
            f'<rect x="{b.x:.0f}" y="{b.y:.0f}" width="{b.w:.0f}" height="{b.h:.0f}" rx="8" fill="{f}" stroke="{s}" stroke-width="1.5"{dash}/>'
        )
        out.append(
            f'<text x="{b.x + 10:.0f}" y="{b.y + 21:.0f}" font-size="12" font-weight="700" fill="#13302c">{html.escape(_fit(gr.title, b.w))}</text>'
        )
    for e in g.edges:
        a, b = nodes[e.src], nodes[e.dst]
        x1, y1, x2, y2 = _ports(a, b)
        mid = (x1 + x2) / 2
        path = (
            f"M{x1:.0f},{y1:.0f} C{mid:.0f},{y1:.0f} {mid:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
            if abs(x1 - x2) > 4
            else f"M{x1:.0f},{y1:.0f} L{x2:.0f},{y2:.0f}"
        )
        dash = ' stroke-dasharray="5 4"' if e.dashed else ""
        out.append(f'<path d="{path}" fill="none" stroke="#5f7471" stroke-width="1.3"{dash} marker-end="url(#arrow)"/>')
        if e.text:
            out.append(
                f'<text x="{(x1 + x2) / 2:.0f}" y="{(y1 + y2) / 2 - 4:.0f}" font-size="10" fill="#5f7471" text-anchor="middle" '
                f'paint-order="stroke" stroke="#ffffff" stroke-width="3">{html.escape(e.text)}</text>'
            )
    for n in g.nodes:
        b = nodes[n.id]
        colour = KIND_COLOUR.get(n.kind, "#5f7471")
        out.append(
            f'<rect x="{b.x:.0f}" y="{b.y:.0f}" width="{b.w:.0f}" height="{b.h:.0f}" rx="7" fill="#ffffff" stroke="{colour}" stroke-width="1.5"/>'
        )
        out.append(f'<rect x="{b.x:.0f}" y="{b.y:.0f}" width="5" height="{b.h:.0f}" rx="2" fill="{colour}"/>')
        out.append(
            f'<text x="{b.x + 12:.0f}" y="{b.y + 22:.0f}" font-size="12" font-weight="700" fill="#13302c">{html.escape(_fit(n.title, b.w))}</text>'
        )
        for k, part in enumerate(_wrap(n.detail, 26)[:3]):
            out.append(f'<text x="{b.x + 12:.0f}" y="{b.y + 39 + k * 13:.0f}" font-size="10" fill="#5f7471">{html.escape(part)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _ports(a: Box, b: Box) -> tuple[float, float, float, float]:
    if a.x + a.w <= b.x:
        return a.x + a.w, a.y + a.h / 2, b.x, b.y + b.h / 2
    if b.x + b.w <= a.x:
        return a.x, a.y + a.h / 2, b.x + b.w, b.y + b.h / 2
    if a.y + a.h <= b.y:
        return a.x + a.w / 2, a.y + a.h, b.x + b.w / 2, b.y
    return a.x + a.w / 2, a.y, b.x + b.w / 2, b.y + b.h


def _fit(text: str, width: float) -> str:
    most = int(width / 7)
    return text if len(text) <= most else text[: most - 1] + "…"


def _wrap(text: str, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split(" "):
        if line and len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + ([line] if line else [])
