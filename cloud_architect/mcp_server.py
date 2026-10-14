"""MCP server: Azure pricing, data-residency checks, company-policy extraction, the Well-Architected
review, diagrams and Terraform as tools for any Model Context Protocol client.

    python -m cloud_architect mcp          # stdio, for desktop assistants

The client's own model does the designing; these tools give it live prices in any currency, region
availability and the same deterministic assessment the agent uses. No LLM key is needed here: policy
and brief extraction use the rule-based readers unless the server has a model configured.
"""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from pathlib import Path

from fastmcp import FastMCP

from . import brief, catalogue, policies
from .assess import assess
from .catalogue import SERVICES
from .config import settings
from .diagram import drawio, mermaid
from .models import Design, Workload
from .pricing import PriceBook
from .regions import ALL, JURISDICTIONS, RESIDENCY_REGIONS, label
from .rules import architect
from .terraform import generate

mcp = FastMCP(
    "cloud-architect",
    instructions=(
        "Azure architecture tools for any region. Prices are live from the Azure Retail Prices API, in the workload's currency "
        "(USD, EUR, GBP, AUD, NZD, CAD and more). Use list_regions to see jurisdictions and region pairs, list_services for the "
        "catalogue, check_region before placing a service, extract_policies to turn a policy document into rules (review them, "
        "then put accepted ones in the workload's policies with status 'accepted'), assess_design to price and review a design, "
        "and diagram_for and terraform_for for the outputs."
    ),
)


@lru_cache(maxsize=1)
def _book() -> PriceBook:
    return PriceBook(settings().price_cache)


@mcp.tool
def list_services(role: str = "") -> str:
    """The catalogue of Azure building blocks and tiers, optionally filtered by role
    (web_app, api, relational_db, nosql_db, object_storage, cache, llm, search, background_jobs, containers, virtual_machines)."""
    return json.dumps(catalogue.options(role or None))


@mcp.tool
def list_regions(jurisdiction: str = "") -> str:
    """Jurisdictions (eu, uk, us, canada, australia, nz, anz, any) with their allowed Azure regions and region pairs."""
    keys = [jurisdiction] if jurisdiction in JURISDICTIONS else list(JURISDICTIONS)
    return json.dumps(
        {
            j: {
                "label": JURISDICTIONS[j]["label"],
                "currency": JURISDICTIONS[j]["currency"],
                "regions": {r: {"name": label(r), "pair": ALL[r].pair, "restricted": ALL[r].restricted} for r in sorted(RESIDENCY_REGIONS[j])},
            }
            for j in keys
        }
    )


@mcp.tool
def check_region(service: str, region: str) -> str:
    """Whether Azure sells a catalogue service in a region (uksouth, westeurope, eastus2, ...), with residency notes."""
    svc = SERVICES.get(service)
    if not svc:
        return json.dumps({"error": f"unknown service {service}"})
    sold = _book().available(svc.api_service, region, svc.api_extra)
    return json.dumps({"service": service, "region": label(region), "sold": sold, "residency_note": svc.residency_note})


@mcp.tool
def extract_policies(text: str = "", path: str = "", filename: str = "policy.md", content_b64: str = "") -> str:
    """Propose machine-checkable rules from a company policy: pass the text, a local file path, or base64 file content
    (PDF, DOCX, Markdown or text). Each rule cites its section and sentence. Rules come back 'proposed': show them to the
    person and enforce only the ones they accept."""
    if path:
        data, filename = Path(path).read_bytes(), Path(path).name
    elif content_b64:
        data = base64.b64decode(content_b64)
    else:
        data, filename = text.encode("utf-8"), filename if filename.endswith((".md", ".txt")) else "policy.md"
    return json.dumps(policies.extract(filename, data, settings()))


@mcp.tool
def extract_brief(text: str = "", path: str = "") -> str:
    """Propose workload values (users, data size and growth, RPO/RTO, frameworks, budget, ...) from a requirements brief or RFP."""
    if path:
        return json.dumps(brief.extract(Path(path).name, Path(path).read_bytes(), settings()))
    return json.dumps(brief.extract("brief.md", text.encode("utf-8"), settings()))


@mcp.tool
def assess_design(workload: dict, design: dict) -> str:
    """Price a design in the workload's currency and check residency, budget, coverage, stack and accepted policy rules,
    plus the Well-Architected and compliance review."""
    w, d = Workload.model_validate(workload), Design.model_validate(design)
    return assess(d, w, _book()).model_dump_json()


@mcp.tool
def rule_based_design(workload: dict) -> str:
    """A baseline design from fixed rules (no model), already fitted to the budget."""
    w = Workload.model_validate(workload)
    return architect(w, _book()).model_dump_json()


@mcp.tool
def diagram_for(workload: dict, design: dict, format: str = "mermaid") -> str:
    """The architecture diagram: 'mermaid' (renders in markdown) or 'drawio' (opens and edits in draw.io)."""
    w, d = Workload.model_validate(workload), Design.model_validate(design)
    return drawio(d, w) if format == "drawio" else mermaid(d, w, theme=True)


@mcp.tool
def terraform_for(workload: dict, design: dict) -> str:
    """Terraform (azurerm v4) for a design."""
    return generate(Design.model_validate(design), Workload.model_validate(workload))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
