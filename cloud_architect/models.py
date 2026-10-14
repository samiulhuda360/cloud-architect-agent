"""The workload a user describes, the company policies it must follow, and the architecture proposed for it."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .regions import CURRENCIES, JURISDICTIONS, REGIONS, RESIDENCY_REGIONS, _squash, region_code

__all__ = ["REGIONS", "RESIDENCY_REGIONS", "region_code", "_squash"]

Need = Literal[
    "web_app", "api", "relational_db", "nosql_db", "object_storage", "cache", "llm", "search", "background_jobs", "containers", "virtual_machines"
]
Residency = Literal["eu", "uk", "us", "canada", "australia", "nz", "anz", "any"]
Availability = Literal["standard", "high", "critical"]
Framework = Literal["gdpr", "hipaa", "pci_dss", "iso27001", "soc2"]
Identity = Literal["none", "entra_id", "entra_external_id", "external"]
Env = Literal["dev", "test", "prod"]
AVAILABILITY_TARGET = {"standard": "99.9%", "high": "99.95%", "critical": "99.99%"}
UPTIME = {"99.9": "standard", "99.95": "high", "99.99": "critical"}

PolicyCheck = Literal[
    "allowed_regions",
    "customer_managed_keys",
    "no_public_endpoints",
    "min_backup_retention_days",
    "approved_services",
    "max_monthly_spend",
    "required_tags",
    "min_log_retention_days",
]


class PolicyRule(BaseModel):
    """One machine-checkable rule taken from a company policy document, with the clause it came from."""

    id: str = ""
    check: PolicyCheck
    regions: list[str] = Field(default_factory=list, description="allowed_regions: Azure region codes")
    days: int | None = Field(default=None, ge=0, description="retention rules: minimum days")
    amount: float | None = Field(default=None, ge=0, description="max_monthly_spend: the cap")
    currency: str | None = None
    services: list[str] = Field(default_factory=list, description="approved_services: catalogue ids")
    tags: list[str] = Field(default_factory=list, description="required_tags: tag names")
    source: str = Field(default="", description="document the rule came from")
    section: str = ""
    quote: str = Field(default="", description="the sentence that states the rule, verbatim")
    status: Literal["proposed", "accepted", "rejected"] = "proposed"
    extracted_by: Literal["model", "rules", "manual"] = "manual"

    @field_validator("regions", mode="before")
    @classmethod
    def _codes(cls, v):
        return [region_code(r) for r in v or []]

    @property
    def citation(self) -> str:
        where = f"{self.source}, section {self.section}" if self.section else self.source
        return f'{where}: "{self.quote}"' if self.quote else where


class Workload(BaseModel):
    """What the user wants to run, in business terms."""

    name: str = Field(min_length=2, max_length=80)
    description: str = Field(default="", max_length=1200)
    needs: list[Need] = Field(min_length=1)
    # users
    monthly_users: int = Field(default=1_000, ge=1, description="monthly active users")
    peak_concurrent_users: int | None = Field(default=None, ge=1, description="users online at the busiest moment")
    user_locations: list[str] = Field(default_factory=list, description="where the users are: countries or Azure regions")
    # traffic
    peak_rps: float = Field(default=5, ge=0, description="peak requests per second")
    read_ratio: float = Field(default=0.8, ge=0, le=1, description="share of requests that only read data")
    # data
    data_gb: float = Field(default=10, ge=0, description="primary database size today")
    data_growth_pct: float = Field(default=0, ge=0, le=1000, description="database growth per year, in percent")
    blob_gb: float = Field(default=0, ge=0, description="files, images, documents")
    retention_years: float = Field(default=0, ge=0, le=100, description="how long records must be kept")
    backup_retention_days: int = Field(default=7, ge=1, le=3650, description="how long database backups are kept")
    geo_backup: bool = Field(default=False, description="keep a copy of backups in a second region")
    llm_input_mtokens: float = Field(default=0, ge=0, description="LLM input tokens per month, in millions")
    llm_output_mtokens: float = Field(default=0, ge=0, description="LLM output tokens per month, in millions")
    # reliability
    availability: Availability = "standard"
    rpo_minutes: int | None = Field(default=None, ge=0, description="most data you can afford to lose, in minutes")
    rto_minutes: int | None = Field(default=None, ge=0, description="longest you can be down after a disaster, in minutes")
    # security and compliance
    residency: Residency = "any"
    frameworks: list[Framework] = Field(default_factory=list)
    pii: bool = Field(default=False, description="handles personal information")
    health_data: bool = False
    card_data: bool = Field(default=False, description="stores, processes or transmits payment card data")
    identity: Identity = "none"
    # environments
    environments: list[Env] = Field(default_factory=lambda: ["prod"])
    environment: Literal["production", "dev"] = "production"
    # context
    integrations: list[str] = Field(default_factory=list, description="outside systems it talks to, e.g. Xero, Salesforce")
    constraints: list[str] = Field(default_factory=list, description="existing stack or rules, e.g. '.NET team', 'must use PostgreSQL'")
    budget_month: float | None = Field(default=None, ge=0)
    currency: str = ""
    preferred_region: str | None = None
    policies: list[PolicyRule] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        """Older workload files: budget_nzd_month, environment: dev, uptime_target: 99.95."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "budget_nzd_month" in data:
            nzd = data.pop("budget_nzd_month")
            data.setdefault("budget_month", nzd)
            data.setdefault("currency", "NZD")
        if "environment" in data and "environments" not in data:
            data["environments"] = ["dev"] if data["environment"] == "dev" else ["prod"]
        if "uptime_target" in data:
            target = str(data.pop("uptime_target")).rstrip("%")
            data.setdefault("availability", UPTIME.get(target, "standard"))
        return data

    @field_validator("preferred_region", mode="before")
    @classmethod
    def _region(cls, v):
        return region_code(v) if v else None

    @field_validator("environments")
    @classmethod
    def _envs(cls, v):
        order = ["dev", "test", "prod"]
        return sorted(set(v), key=order.index) or ["prod"]

    @model_validator(mode="after")
    def _derived(self):
        self.environment = "production" if "prod" in self.environments else "dev"
        self.currency = (self.currency or JURISDICTIONS[self.residency]["currency"]).upper()
        if self.currency not in CURRENCIES:
            raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}")
        if "pci_dss" in self.frameworks:
            self.card_data = True
        if "hipaa" in self.frameworks:
            self.health_data = True
        if self.health_data or self.card_data:
            self.pii = True
        return self

    # ------------------------------------------------------------ derived sizes
    def data_gb_at(self, months: float) -> float:
        """Database size after `months`, growing at data_growth_pct a year (compounded)."""
        return self.data_gb * (1 + self.data_growth_pct / 100) ** (months / 12)

    def concurrent(self) -> int:
        """Peak concurrent users; when not given, assume 5% of monthly users at the busiest moment."""
        return self.peak_concurrent_users or max(1, math.ceil(self.monthly_users * 0.05))

    @property
    def accepted_policies(self) -> list[PolicyRule]:
        return [p for p in self.policies if p.status == "accepted"]


class Component(BaseModel):
    """One deployed piece: a catalogue entry at a tier, in a region."""

    service: str = Field(description="catalogue id, e.g. app_service")
    tier: str
    region: str
    instances: int = Field(default=1, ge=1, le=50)
    zone_redundant: bool = False
    private_endpoint: bool = False
    customer_managed_key: bool = Field(default=False, description="encrypt with a key the customer holds in Key Vault")
    purpose: str = ""
    settings: dict[str, float] = Field(default_factory=dict, description="sizing overrides, e.g. storage_gb, retention_days")

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
        default_factory=list, description="constraints knowingly relaxed, with the reason, e.g. '[RES-REGION] llm outside the UK: ...'"
    )
    tags: dict[str, str] = Field(default_factory=dict, description="tags applied to every resource")

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
    amount: float = Field(description="monthly cost in the workload's currency")
    environment: Env = "prod"


class Finding(BaseModel):
    pillar: Literal["reliability", "security", "cost", "operations", "performance", "residency", "coverage", "compliance", "policy"]
    severity: Literal["high", "medium", "low", "info"]
    rule: str
    message: str
    fix: str = ""
    source: str = Field(default="", description="the policy clause or framework requirement behind the finding")


class Assessment(BaseModel):
    """Everything known about a design: price, constraint checks, the Well-Architected review and sizing."""

    total_month: float
    currency: str
    fx_rate: float = Field(description="Azure's own rate: 1 USD in the workload's currency")
    lines: list[CostLine]
    violations: list[Finding]
    findings: list[Finding]
    by_environment: dict[str, float] = Field(default_factory=dict)
    sizing: dict[str, float] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations
