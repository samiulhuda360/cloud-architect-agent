"""The workload a user describes and the architecture the agent proposes."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Need = Literal[
    "web_app", "api", "relational_db", "nosql_db", "object_storage", "cache", "llm", "search", "background_jobs", "containers", "virtual_machines"
]
Residency = Literal["nz", "anz", "any"]
Availability = Literal["standard", "high", "critical"]

REGIONS = {
    "newzealandnorth": "New Zealand North (Auckland)",
    "australiaeast": "Australia East (Sydney)",
    "australiasoutheast": "Australia Southeast (Melbourne)",
    "southeastasia": "Southeast Asia (Singapore)",
    "eastus": "East US (Virginia)",
}
RESIDENCY_REGIONS = {
    "nz": {"newzealandnorth"},
    "anz": {"newzealandnorth", "australiaeast", "australiasoutheast"},
    "any": set(REGIONS),
}
AVAILABILITY_TARGET = {"standard": "99.9%", "high": "99.95%", "critical": "99.99%"}


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def region_code(value):
    """'New Zealand North' or 'New Zealand North (Auckland)' -> 'newzealandnorth'. Unknown names pass through."""
    if not isinstance(value, str):
        return value
    key = _squash(value)
    return next((code for code, label in REGIONS.items() if key in (code, _squash(label))), value.strip())


class Workload(BaseModel):
    """What the user wants to run, in business terms."""

    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=1200)
    needs: list[Need] = Field(min_length=1)
    monthly_users: int = Field(default=1_000, ge=1)
    peak_rps: float = Field(default=5, ge=0, description="peak requests per second")
    data_gb: float = Field(default=10, ge=0, description="primary database size")
    blob_gb: float = Field(default=0, ge=0, description="files, images, documents")
    llm_input_mtokens: float = Field(default=0, ge=0, description="LLM input tokens per month, in millions")
    llm_output_mtokens: float = Field(default=0, ge=0, description="LLM output tokens per month, in millions")
    residency: Residency = "any"
    availability: Availability = "standard"
    environment: Literal["production", "dev"] = "production"
    pii: bool = Field(default=False, description="handles personal information")
    budget_nzd_month: float | None = Field(default=None, ge=0)


class Component(BaseModel):
    """One deployed piece: a catalogue entry at a tier, in a region."""

    service: str = Field(description="catalogue id, e.g. app_service")
    tier: str
    region: str
    instances: int = Field(default=1, ge=1, le=50)
    zone_redundant: bool = False
    private_endpoint: bool = False
    purpose: str = ""
    settings: dict[str, float] = Field(default_factory=dict, description="sizing overrides, e.g. storage_gb")

    @field_validator("region", mode="before")
    @classmethod
    def _region_code(cls, v):
        return region_code(v)

    @model_validator(mode="after")
    def _catalogue_names(self):
        # A design is judged on its choices, not its spelling: 'App Service' / 'p1 v3' become app_service / P1v3.
        from .catalogue import canonical_names  # the catalogue imports this module

        self.service, self.tier = canonical_names(self.service, self.tier)
        return self


class Design(BaseModel):
    region: str
    dr_region: str | None = None
    components: list[Component]
    rationale: str = ""
    tradeoffs: list[str] = Field(default_factory=list)
    accepted_exceptions: list[str] = Field(
        default_factory=list, description="constraints knowingly relaxed, with the reason, e.g. 'llm outside NZ: no Azure model in NZ North'"
    )

    @field_validator("region", "dr_region", mode="before")
    @classmethod
    def _region_codes(cls, v):
        return region_code(v)


class CostLine(BaseModel):
    service: str
    tier: str
    region: str
    item: str
    quantity: float
    unit: str
    nzd_month: float


class Finding(BaseModel):
    pillar: Literal["reliability", "security", "cost", "operations", "performance", "residency", "coverage"]
    severity: Literal["high", "medium", "low", "info"]
    rule: str
    message: str
    fix: str = ""


class Assessment(BaseModel):
    """Everything known about a design: price, constraint checks and the Well-Architected review."""

    total_nzd_month: float
    lines: list[CostLine]
    violations: list[Finding]
    findings: list[Finding]
    fx_usd_to_nzd: float

    @property
    def ok(self) -> bool:
        return not self.violations
