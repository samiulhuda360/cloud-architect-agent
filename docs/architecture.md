# How Cloud Architect Agent works

This is the reference for anyone reviewing or extending the code. The README covers what the project does and why.

```
workload ──► architect (LLM agent, or rules) ──► design ──► assessor ──► price, violations, findings
                    ▲                                          │
                    └──────────── tool results ◄───────────────┘
design ──► terraform.py (main.tf) and diagram.py (Mermaid)
```

## 1. Pricing (`pricing.py`, `catalogue.py`)

**Source.** The [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices) is public, so no Azure account is needed.

**Queries.** The client queries by `serviceName`, `armRegionName` and `priceType eq 'Consumption'`. It follows `NextPageLink`, retries `429` and `5xx` responses with back-off, and caches each query in SQLite (`cache/prices.sqlite`) for a week.

**Currency.** Prices are fetched in USD and converted at Azure's own rate. The NZD prices the API returns are rounded to four decimal places, which skews small meters. So the rate is derived once from a reference SKU that Azure prices in both currencies: Linux D2s v5 in Australia East. That gives US$1 = NZ$1.7667.

**Recipes.** Each service tier in the catalogue is a list of `Usage` recipes. A recipe has:

| Field | Meaning |
|---|---|
| `match` | exact product, SKU, meter and (for global services) pricing zone |
| `quantity(component, workload)` | monthly units: hours, GB-months, 10K requests, GB-seconds, … |
| `free` | a monthly free grant, subtracted once (see below) |
| `service`, `extra`, `region` | price from another API service or a fixed zone (e.g. AKS nodes are Virtual Machines; Front Door is priced per zone) |

**Tiered meters.** `tiered_cost` applies each volume band in turn (for example, Log Analytics' first 5 GB free). Identical duplicate rows, which Azure sometimes publishes, are collapsed.

**Free grants.** Some regions publish a grant as a $0 first band and others don't. If a $0 band is present, the recipe's `free` value is not subtracted again.

**Platform minimums.** `billed_instances()` applies the floor Azure enforces once zone redundancy is on:
- **App Service** runs at least 2 instances.
- **Flex Consumption Functions** keeps 2 always-ready instances, billed on the baseline meter around the clock, with executions on the always-ready meters.

**One price per band.** Every recipe must resolve to exactly one price per volume band. `catalogue.ambiguous_recipes()` enforces this. It runs offline in the test suite and weekly in CI against the live API (`LIVE_PRICES=1`). The check exists because Azure gives all ten PostgreSQL General Purpose sizes the same meter name.

**Normalisation.** Names are normalised before anything is judged, so a design is assessed on its choices and not its spelling:
- regions: `New Zealand North` → `newzealandnorth`
- services: `App Service` → `app_service`
- tiers: `p1 v3` → `P1v3`

## 2. Assessment (`assess.py`)

The assessor is deterministic: the same design and workload always get the same verdict. The agent, the baseline and the rule-based architect are all judged by it.

### Hard constraints (violations)

A violation blocks a design unless `accepted_exceptions` contains an entry that names the rule code (for example `"[RES-REGION] …"`) or the thing it relaxes. Accepted exceptions are reported, never hidden.

| Code | Pillar | Fails when |
|---|---|---|
| `UNKNOWN` | coverage | the service or tier is not in the catalogue |
| `REGION` | coverage | the region is not one of the five supported |
| `NOT-SOLD` | coverage | Azure has no price for that service in that region |
| `NEED` | coverage | a workload need (web app, SQL, LLM, …) is not covered by any component |
| `RES-REGION` | residency | a regional component sits outside the residency boundary |
| `RES-AI-GLOBAL` | residency | an AI deployment is *Global* while residency is not `any`, or the workload has personal data |
| `RES-AI-ZONE` | residency | an AI deployment is *Data Zone* under `nz` residency (a medium finding under `anz`) |
| `RES-DR` | residency | the DR region is outside the residency boundary |
| `BUDGET` | cost | the monthly total is over budget |

### Well-Architected review (findings)

| Code | Severity | Flags |
|---|---|---|
| `REL-ZONES` | high | high or critical availability, but compute is not zone-redundant with at least 2 billed instances (the DR standby is exempt) |
| `REL-DB-HA` | high | high or critical availability, but the database has no zone-redundant standby (replicas are exempt) |
| `REL-DR` | high | critical availability with no DR region, or a DR region with no standby compute or no copy of the database |
| `REL-ZR-TIER` | high | zone redundancy requested on a tier that doesn't support it |
| `REL-FAILOVER` | medium | a working DR site but no Front Door, so failover is a manual DNS change |
| `REL-STORAGE` | medium | locally redundant storage under high availability |
| `REL-TIER` | medium | a dev or test tier in production |
| `SEC-PRIVATE` | high | personal data in a store that is reachable from the internet |
| `SEC-SECRETS` | high | no Key Vault |
| `SEC-WAF` | medium | a public web app with personal data and no WAF (Application Gateway WAF v2 or Front Door Premium) |
| `OPS-LOGS` | medium | no Log Analytics workspace |
| `PERF-CACHE` | medium | more than 50 requests/s at peak on a SQL database with no cache |
| `PERF-EDGE` | low | more than 50,000 users, residency `any`, and no edge network |
| `COST-DEV` | medium | a production tier in a dev environment |
| `COST-RESERVE` | info | steady compute that a reservation would make cheaper |
| `RES-EDGE` | medium | a global edge service under `nz` or `anz` residency |

## 3. The architects

### Rule-based (`rules.py`)

Fixed rules encode common practice:
- zone redundancy for high availability;
- private endpoints for personal data;
- Key Vault and Log Analytics always;
- for critical workloads, a warm standby in the DR region (inside the residency boundary) with database replicas and Front Door, which is Premium with WAF when the app handles personal data.

The design is then downsized tier by tier until it fits the budget, and each step is recorded in `tradeoffs`.

### LLM agent (`agent.py`)

The agent is an OpenAI-compatible tool-calling loop (at most 12 steps) with three tools:

| Tool | Returns |
|---|---|
| `list_options(role?)` | the catalogue: services, tiers, zone-redundancy support, notes and residency notes |
| `assess_design(design)` | total and per-component NZD, violations with fixes, high findings, other findings |
| `submit_design(design)` | accepts the design, or rejects it once if hard violations remain |

The system prompt sets five rules:
1. Never state a price that didn't come from `assess_design`.
2. Fix every violation.
3. Stay within the budget.
4. Record a residency exception only when no compliant option exists, and start it with the rule code.
5. Address high findings, or say in `tradeoffs` why not.

Rate limits, timeouts and `5xx` errors are retried with back-off. A daily quota error fails fast instead.

### LLM without tools (the baseline)

The baseline gets the same rules, region codes, allowed regions and full catalogue (with its notes) as the agent, but no tools. It must estimate the price from its own knowledge. An unparseable reply gets one repair turn, the same allowance the agent gets for a rejected submission. The difference between the two systems is therefore only the tools.

## 4. Evaluation (`evaluate.py`)

20 scenarios in `eval/scenarios.yaml`, each with optional expectations:

| Expectation | Meaning |
|---|---|
| `max_total` | total is at most this amount |
| `all_in` | named regions only |
| `private_data` | every data store behind a private endpoint |
| `zone_redundant_db` | the database is zone-redundant |
| `has` | the design includes these services |
| `no_tier` | the design avoids these tiers |
| `llm_exception` | an exception is recorded for the LLM |
| `dr_region` | a working DR site exists: standby compute and a database copy |
| `dr_note` | the single-region risk is recorded |

Each design is scored on:
- silent violations;
- staying within budget;
- expectations met;
- high findings left open;
- `terraform validate`;
- for the baseline, how far its own cost estimate is from the live price of its design.

`--resume` re-runs only scenarios that failed with a provider error. A model that answered badly is never re-run.

## 5. Terraform (`terraform.py`)

**Scope.** One resource group with a random suffix for globally unique names. Secrets come in as variables.

**Networking.** Each region gets a virtual network only if it needs one, with only the subnets it needs:

| Subnet | Address range | Used for |
|---|---|---|
| private endpoints | `.1.0/24` | private endpoints |
| workloads | `.2.0/24` | VMs |
| gateway | `.3.0/24` | Application Gateway |
| container apps | `.4.0/23` | zone-redundant Container Apps environment |
| app integration | `.6.0/24` | App Service VNet integration, delegated to `Microsoft.Web/serverFarms` |

**Private endpoints.** Each one registers in a private DNS zone (`privatelink.postgres.database.azure.com`, …). The zone is linked to every virtual network, and web apps join the network through VNet integration so they can resolve and reach their data.

**Disaster recovery:**
- PostgreSQL replicas use `create_mode = "Replica"`.
- Azure SQL replicas are geo-secondaries (`create_mode = "Secondary"`).
- A Cosmos DB replica becomes a second `geo_location` on the same account, with automatic failover.
- Front Door gets one origin per web app: priority 1 in the primary region, priority 2 in the DR region. A health probe and an HTTPS route connect them. Premium adds a WAF policy with the Microsoft Default and Bot Manager rule sets.

**Formatting.** `_fmt` aligns attributes exactly as `terraform fmt` does. Tests run a `terraform fmt` round-trip and `terraform validate` on every scenario design when Terraform is installed (CI installs it).
