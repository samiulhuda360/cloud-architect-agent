"""MCP server: Azure pricing, NZ data-residency checks and the Well-Architected review as tools
for any Model Context Protocol client.

    python -m cloud_architect mcp          # stdio, for desktop assistants

The client's own model does the designing; these tools give it live NZD prices, region
availability and the same deterministic assessment the agent uses. No LLM key is needed here.
"""

from __future__ import annotations

import json
from functools import lru_cache

from fastmcp import FastMCP

from . import catalogue
from .assess import assess
from .catalogue import SERVICES
from .config import settings
from .models import REGIONS, Design, Workload
from .pricing import PriceBook
from .rules import architect
from .terraform import generate

mcp = FastMCP(
    "cloud-architect",
    instructions=(
        "Azure architecture tools for New Zealand workloads. Prices are live from the Azure Retail Prices API, in NZD. "
        "Use list_services to see the catalogue, check_region before placing a service, assess_design to price and review "
        "a design, and terraform_for to get infrastructure as code."
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
def check_region(service: str, region: str) -> str:
    """Whether Azure sells a catalogue service in a region (newzealandnorth, australiaeast, ...), with residency notes."""
    svc = SERVICES.get(service)
    if not svc:
        return json.dumps({"error": f"unknown service {service}"})
    sold = _book().available(svc.api_service, region, svc.api_extra)
    return json.dumps({"service": service, "region": REGIONS.get(region, region), "sold": sold, "residency_note": svc.residency_note})


@mcp.tool
def assess_design(workload: dict, design: dict) -> str:
    """Price a design in NZD and check residency, budget and coverage, plus the Well-Architected review."""
    w, d = Workload.model_validate(workload), Design.model_validate(design)
    a = assess(d, w, _book())
    return a.model_dump_json()


@mcp.tool
def rule_based_design(workload: dict) -> str:
    """A baseline design from fixed rules (no model), already fitted to the budget."""
    w = Workload.model_validate(workload)
    return architect(w, _book()).model_dump_json()


@mcp.tool
def terraform_for(workload: dict, design: dict) -> str:
    """Terraform (azurerm v4) for a design."""
    return generate(Design.model_validate(design), Workload.model_validate(workload))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
