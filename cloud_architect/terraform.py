"""Turn a design into Terraform for the azurerm provider (v4).

The output is a starting point a platform team can review and apply: one resource group; every
component with its tier, zone redundancy and instance count; managed identities and Key Vault;
private endpoints with private DNS zones, a virtual network in each region that needs one, and App
Service VNet integration so the apps can reach them; and, when the design has a DR region, database
replicas there and Front Door with priority failover between the regions. Compliance settings come
from the design too: customer-managed keys, backup retention and geo-redundant backups, long-term
backups in a backup vault, log retention, audit diagnostic settings, record deletion after the
retention period, autoscale for peak concurrency, and tags on every resource. Secrets are input
variables, never literals. Every generated file passes `terraform validate` and `terraform fmt`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .catalogue import pg_storage_gb
from .compliance import PITR_MAX_DAYS
from .models import Component, Design, Workload
from .regions import ALL

PG_SKU = {"B1ms": "B_Standard_B1ms", "B2s": "B_Standard_B2s", "D2ds_v5": "GP_Standard_D2ds_v5", "D4ds_v5": "GP_Standard_D4ds_v5"}
SQL_SKU = {"S0": "S0", "S2": "S2", "GP_2vcore": "GP_Gen5_2"}
APP_SKU = {"B1": "B1", "S1": "S1", "P0v3": "P0v3", "P1v3": "P1v3", "P2v3": "P2v3"}
REDIS = {"basic_c0": ("Basic", 0), "standard_c0": ("Standard", 0), "standard_c1": ("Standard", 1)}
STORAGE = {"hot_lrs": "LRS", "hot_zrs": "ZRS", "hot_grs": "GRS"}
CA = {"0.5vcpu": (0.5, "1Gi"), "1vcpu": (1, "2Gi"), "2vcpu": (2, "4Gi")}
OPENAI = {
    "gpt-4o-mini_global": ("gpt-4o-mini", "2024-07-18", "GlobalStandard"),
    "o4-mini_datazone": ("o4-mini", "2025-04-16", "DataZoneStandard"),
    "gpt-4o-mini_regional": ("gpt-4o-mini", "2024-07-18", "Standard"),
    "o4-mini_regional": ("o4-mini", "2025-04-16", "Standard"),
}
VM = {"D2s_v5": "Standard_D2s_v5", "D4s_v5": "Standard_D4s_v5", "standard_d2s": "Standard_D2s_v5", "standard_d4s": "Standard_D4s_v5"}
PE_GROUP = {"postgres": "postgresqlServer", "azure_sql": "sqlServer", "cosmos_db": "Sql", "blob_storage": "blob"}
DNS_ZONE = {
    "postgres": "privatelink.postgres.database.azure.com",
    "azure_sql": "privatelink.database.windows.net",
    "cosmos_db": "privatelink.documents.azure.com",
    "blob_storage": "privatelink.blob.core.windows.net",
}
TARGET = {
    "postgres": "azurerm_postgresql_flexible_server.postgres_{i}.id",
    "azure_sql": "azurerm_mssql_server.azure_sql_{i}.id",
    "cosmos_db": "azurerm_cosmosdb_account.cosmos_db_{i}.id",
    "blob_storage": "azurerm_storage_account.blob_storage_{i}.id",
}
ORIGIN = {  # the public host name of each kind of web compute, for Front Door and Application Gateway
    "app_service": "azurerm_linux_web_app.app_service_{i}.default_hostname",
    "container_apps": "azurerm_container_app.container_apps_{i}.ingress[0].fqdn",
}
SUBNETS = {  # key: (name, address suffix inside the region's /16, delegation)
    "private_endpoints": ("snet-private-endpoints", "1.0/24", None),
    "workloads": ("snet-workloads", "2.0/24", None),
    "gateway": ("snet-app-gateway", "3.0/24", None),
    "container_apps": ("snet-container-apps", "4.0/23", None),  # a Consumption-only environment needs /23 or larger
    "app_integration": ("snet-app-integration", "6.0/24", "Microsoft.Web/serverFarms"),  # App Service VNet integration
}


# Resource types that take tags; every one of them gets the design's tags.
TAGGED = {
    "azurerm_resource_group", "azurerm_virtual_network", "azurerm_private_dns_zone", "azurerm_private_endpoint", "azurerm_service_plan",
    "azurerm_linux_web_app", "azurerm_container_app_environment", "azurerm_container_app", "azurerm_storage_account",
    "azurerm_function_app_flex_consumption", "azurerm_postgresql_flexible_server", "azurerm_mssql_server", "azurerm_mssql_database",
    "azurerm_cosmosdb_account", "azurerm_redis_cache", "azurerm_key_vault", "azurerm_log_analytics_workspace", "azurerm_search_service",
    "azurerm_cognitive_account", "azurerm_cdn_frontdoor_profile", "azurerm_public_ip", "azurerm_web_application_firewall_policy",
    "azurerm_application_gateway", "azurerm_kubernetes_cluster", "azurerm_network_interface", "azurerm_linux_virtual_machine",
    "azurerm_data_protection_backup_vault", "azurerm_user_assigned_identity", "azurerm_monitor_autoscale_setting",
}  # fmt: skip
DIAG_TARGET = {  # what an audit diagnostic setting attaches to, per data store
    "postgres": "azurerm_postgresql_flexible_server.postgres_{i}.id",
    "azure_sql": "azurerm_mssql_database.azure_sql_{i}.id",
    "cosmos_db": "azurerm_cosmosdb_account.cosmos_db_{i}.id",
    "blob_storage": '"${{azurerm_storage_account.blob_storage_{i}.id}}/blobServices/default"',
    "key_vault": "azurerm_key_vault.key_vault_{i}.id",
}


def _slug(text: str, n: int = 18) -> str:
    return (re.sub(r"[^a-z0-9]", "", text.lower()) or "app")[:n]


def replica(c: Component) -> bool:
    return bool(c.settings.get("replica"))


def _private(c: Component) -> bool:
    # A Cosmos DB replica is another region of the same account; its endpoint would clash in the shared DNS zone.
    return c.private_endpoint and c.service in PE_GROUP and not (c.service == "cosmos_db" and replica(c))


@dataclass
class Ctx:
    """What a builder needs beyond its own component."""

    design: Design
    w: Workload
    name: str
    index: dict[int, int]  # id(component) -> its index among components of the same service
    vnets: dict[str, str]  # region -> virtual network key; "main" is the primary region's
    private: bool  # some data sits behind private endpoints, so apps need VNet integration
    cmk: bool = False  # some data is encrypted with a customer-managed key
    geo_cmk: bool = False  # ...and has geo-redundant backups, which need a key in the paired region too

    @property
    def vault(self) -> str:
        return "azurerm_key_vault.key_vault_0" if self.of("key_vault") else "azurerm_key_vault.cmk"

    def cmk_identity(self) -> str:
        return """
  identity {
    type = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.cmk.id]
  }"""

    def sfx(self, region: str) -> str:
        key = self.vnets.get(region, "main")
        return "" if key == "main" else f"_{key}"

    def of(self, service: str) -> list[Component]:
        return [c for c in self.design.components if c.service == service]

    def source(self, service: str) -> int:
        """Index of the first component of a service that is not a replica: what the replicas copy."""
        return next((self.index[id(c)] for c in self.of(service) if not replica(c)), 0)

    def hosts(self, region: str | None = None) -> list[tuple[Component, str]]:
        return [
            (c, ORIGIN[c.service].format(i=self.index[id(c)]))
            for c in self.design.components
            if c.service in ORIGIN and (region is None or c.region == region)
        ]


def generate(design: Design, w: Workload) -> str:
    comps = design.components
    index: dict[int, int] = {}
    counts: dict[str, int] = {}
    for c in comps:
        index[id(c)] = counts.get(c.service, 0)
        counts[c.service] = index[id(c)] + 1
    private = any(_private(c) for c in comps)
    # The subnets each region needs; a region gets a virtual network only if it needs one.
    subnets: dict[str, set[str]] = {}
    for c in comps:
        need = {
            "private_endpoints": _private(c),
            "app_integration": private and c.service == "app_service",
            "workloads": c.service == "vm",
            "gateway": c.service == "app_gateway",
            "container_apps": c.service == "container_apps" and c.zone_redundant,
        }
        if any(need.values()):
            subnets.setdefault(c.region, set()).update(k for k, v in need.items() if v)
    regions = sorted(subnets, key=lambda r: (r != design.region, r))
    vnets = {r: "main" if r == design.region else f"r{n}" for n, r in enumerate(regions)}
    cmk = any(c.customer_managed_key and not replica(c) for c in comps if c.service in PE_GROUP)
    geo_cmk = cmk and any(c.customer_managed_key and c.settings.get("geo_backup") for c in comps if c.service == "postgres")
    ctx = Ctx(design, w, _slug(w.name), index, vnets, private, cmk, geo_cmk)

    blocks = [HEADER.format(name=ctx.name, region=design.region), _tags(design)]
    blocks += [_vnet(vnets[r], r, n, subnets[r]) for n, r in enumerate(regions)]
    blocks += [_dns_zone(s, list(vnets.values())) for s in sorted({c.service for c in comps if _private(c)})]
    if not ctx.of("log_analytics") and ctx.of("container_apps"):
        blocks.append(LOGS.format(i=0, region=design.region, days=30))
    if cmk:
        blocks.append(_cmk(ctx))
    for c in comps:
        i = index[id(c)]
        if c.service in BUILDERS:
            blocks.append(BUILDERS[c.service](c, i, ctx))
        if c.service in DIAG_TARGET and not replica(c) and _audit(ctx):
            blocks.append(_diagnostics(c, i, ctx))
        if _private(c):
            blocks.append(
                PRIVATE_ENDPOINT.format(
                    svc=c.service, i=i, group=PE_GROUP[c.service], target=TARGET[c.service].format(i=i), region=c.region, sfx=ctx.sfx(c.region)
                )
            )
    return _fmt(_tag_resources("\n".join(b.strip() + "\n" for b in blocks if b.strip())))


def _audit(ctx: Ctx) -> bool:
    return any(c.settings.get("audit") for c in ctx.of("log_analytics"))


def _tags(design: Design) -> str:
    """Tags for every resource. Tags a policy requires but the design leaves blank become a required input."""
    fixed = {k: v for k, v in design.tags.items() if v} or {"workload": "app"}
    required = sorted(k for k, v in design.tags.items() if not v)
    body = "".join(f'\n    "{k}" = {json.dumps(v)}' for k, v in sorted(fixed.items()))
    if not required:
        return f"""
locals {{
  tags = {{{body}
  }}
}}
"""
    keys = ", ".join(f'"{k}"' for k in required)
    return f"""
variable "required_tags" {{
  type = map(string)
  description = "Tags the company policy requires on every resource: {", ".join(required)}"

  validation {{
    condition = alltrue([for k in [{keys}] : length(lookup(var.required_tags, k, "")) > 0])
    error_message = "Set every required tag: {", ".join(required)}."
  }}
}}

locals {{
  tags = merge({{{body}
  }}, var.required_tags)
}}
"""


def _tag_resources(code: str) -> str:
    """Add `tags = local.tags` to the end of every top-level resource block whose type takes tags."""
    out, current = [], None
    for line in code.splitlines():
        m = re.match(r'^resource "(\w+)" "\w+" \{$', line)
        if m:
            current = m.group(1)
        elif line == "}" and current:
            if current in TAGGED:
                if out and out[-1].strip() == "}":
                    out.append("")
                out.append("  tags = local.tags")
            current = None
        out.append(line)
    return "\n".join(out) + "\n"


def _cmk(ctx: Ctx) -> str:
    """A user-assigned identity and an RSA key in Key Vault that the data stores encrypt with."""
    out = ""
    if not ctx.of("key_vault"):
        out += f'''
resource "azurerm_key_vault" "cmk" {{
  name = "kvcmk${{random_string.suffix.result}}"
  location = "{ctx.design.region}"
  resource_group_name = azurerm_resource_group.main.name
  tenant_id = data.azurerm_client_config.current.tenant_id
  sku_name = "standard"
  purge_protection_enabled = true
  soft_delete_retention_days = 90
  rbac_authorization_enabled = true
}}
'''
    out += _cmk_key("cmk", ctx.vault, ctx.design.region, ctx.name)
    if ctx.geo_cmk:
        pair = ALL[ctx.design.region].pair if ctx.design.region in ALL else None
        out += f'''
# Geo-redundant backups of a server encrypted with a customer-managed key need a key in the paired region as well.
resource "azurerm_key_vault" "cmk_geo" {{
  name = "kvgeo${{random_string.suffix.result}}"
  location = "{pair}"
  resource_group_name = azurerm_resource_group.main.name
  tenant_id = data.azurerm_client_config.current.tenant_id
  sku_name = "standard"
  purge_protection_enabled = true
  soft_delete_retention_days = 90
  rbac_authorization_enabled = true
}}
'''
        out += _cmk_key("cmk_geo", "azurerm_key_vault.cmk_geo", pair, ctx.name)
    return out


def _cmk_key(key: str, vault: str, region: str | None, name: str) -> str:
    return f'''
resource "azurerm_user_assigned_identity" "{key}" {{
  name = "id-{key.replace("_", "-")}-{name}"
  location = "{region}"
  resource_group_name = azurerm_resource_group.main.name
}}

# Whoever runs Terraform creates the key; the identity only wraps and unwraps data keys with it.
resource "azurerm_role_assignment" "{key}_officer" {{
  scope = {vault}.id
  role_definition_name = "Key Vault Crypto Officer"
  principal_id = data.azurerm_client_config.current.object_id
}}

resource "azurerm_role_assignment" "{key}_user" {{
  scope = {vault}.id
  role_definition_name = "Key Vault Crypto Service Encryption User"
  principal_id = azurerm_user_assigned_identity.{key}.principal_id
}}

resource "azurerm_key_vault_key" "{key}" {{
  name = "{key.replace("_", "-")}-data"
  key_vault_id = {vault}.id
  key_type = "RSA"
  key_size = 3072
  key_opts = ["unwrapKey", "wrapKey"]
  depends_on = [azurerm_role_assignment.{key}_officer]

  rotation_policy {{
    expire_after = "P2Y"
    notify_before_expiry = "P30D"

    automatic {{
      time_before_expiry = "P30D"
    }}
  }}
}}
'''


def _diagnostics(c: Component, i: int, ctx: Ctx) -> str:
    group = "allLogs" if c.service == "postgres" else "audit"
    return f'''
resource "azurerm_monitor_diagnostic_setting" "{c.service}_{i}_audit" {{
  name = "audit-to-log-analytics"
  target_resource_id = {DIAG_TARGET[c.service].format(i=i)}
  log_analytics_workspace_id = azurerm_log_analytics_workspace.log_analytics_0.id

  enabled_log {{
    category_group = "{group}"
  }}
}}
'''


_ATTR = re.compile(r'^( *)("[^"]+"|[A-Za-z_][\w-]*) *= *(.*)$')


def _fmt(code: str) -> str:
    """Align the '=' of consecutive one-line attributes at the same depth, as `terraform fmt` does."""
    out: list[str] = []
    run: list[re.Match] = []

    def flush():
        width = max((len(m.group(2)) for m in run), default=0)
        out.extend(f"{m.group(1)}{m.group(2).ljust(width)} = {m.group(3)}" for m in run)
        run.clear()

    for line in code.splitlines():
        m = _ATTR.match(line)
        if m and not m.group(3).endswith(("{", "[")):
            if run and run[0].group(1) != m.group(1):
                flush()
            run.append(m)
        else:
            flush()
            out.append(line)
    flush()
    return "\n".join(out) + "\n"


HEADER = """
terraform {{
  required_version = ">= 1.6"
  required_providers {{
    azurerm = {{
      source = "hashicorp/azurerm"
      version = "~> 4.0"
    }}
    random = {{
      source = "hashicorp/random"
      version = "~> 3.6"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
  subscription_id = var.subscription_id
}}

variable "subscription_id" {{
  type = string
  description = "Azure subscription to deploy into"
}}

variable "db_admin_password" {{
  type = string
  sensitive = true
  default = null
}}

variable "vm_ssh_public_key" {{
  type = string
  default = null
}}

data "azurerm_client_config" "current" {{}}

resource "random_string" "suffix" {{
  length = 6
  special = false
  upper = false
}}

resource "azurerm_resource_group" "main" {{
  name = "rg-{name}"
  location = "{region}"
}}
"""


def _vnet(key: str, region: str, n: int, needs: set[str]) -> str:
    sfx = "" if key == "main" else f"_{key}"
    net = f"10.{20 + n}"
    out = f'''
resource "azurerm_virtual_network" "{key}" {{
  name = "vnet-{key}-${{random_string.suffix.result}}"
  location = "{region}"
  resource_group_name = azurerm_resource_group.main.name
  address_space = ["{net}.0.0/16"]
}}
'''
    for sub, (name, cidr, delegation) in SUBNETS.items():
        if sub not in needs:
            continue
        delegate = (
            f'''

  delegation {{
    name = "delegation"

    service_delegation {{
      name = "{delegation}"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }}
  }}'''
            if delegation
            else ""
        )
        out += f'''
resource "azurerm_subnet" "{sub}{sfx}" {{
  name = "{name}"
  resource_group_name = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.{key}.name
  address_prefixes = ["{net}.{cidr}"]{delegate}
}}
'''
    return out


def _dns_zone(svc: str, vnet_keys: list[str]) -> str:
    out = f'''
resource "azurerm_private_dns_zone" "{svc}" {{
  name = "{DNS_ZONE[svc]}"
  resource_group_name = azurerm_resource_group.main.name
}}
'''
    for key in vnet_keys:
        out += f'''
resource "azurerm_private_dns_zone_virtual_network_link" "{svc}_{key}" {{
  name = "link-{svc}-{key}"
  resource_group_name = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.{svc}.name
  virtual_network_id = azurerm_virtual_network.{key}.id
}}
'''
    return out


LOGS = """
resource "azurerm_log_analytics_workspace" "log_analytics_{i}" {{
  name = "log-${{random_string.suffix.result}}-{i}"
  location = "{region}"
  resource_group_name = azurerm_resource_group.main.name
  sku = "PerGB2018"
  retention_in_days = {days}
}}
"""

PRIVATE_ENDPOINT = """
resource "azurerm_private_endpoint" "{svc}_{i}" {{
  name = "pe-{svc}-{i}"
  location = "{region}"
  resource_group_name = azurerm_resource_group.main.name
  subnet_id = azurerm_subnet.private_endpoints{sfx}.id

  private_service_connection {{
    name = "psc-{svc}-{i}"
    private_connection_resource_id = {target}
    subresource_names = ["{group}"]
    is_manual_connection = false
  }}

  private_dns_zone_group {{
    name = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.{svc}.id]
  }}
}}
"""


def _app_service(c, i, ctx):
    vnet = f"\n  virtual_network_subnet_id = azurerm_subnet.app_integration{ctx.sfx(c.region)}.id" if ctx.private else ""
    return f'''
resource "azurerm_service_plan" "app_service_{i}" {{
  name = "asp-{ctx.name}-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  os_type = "Linux"
  sku_name = "{APP_SKU[c.tier]}"
  worker_count = {c.instances}
  zone_balancing_enabled = {str(c.zone_redundant).lower()}
}}

resource "azurerm_linux_web_app" "app_service_{i}" {{
  name = "app-{ctx.name}-{i}-${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  service_plan_id = azurerm_service_plan.app_service_{i}.id
  https_only = true{vnet}

  identity {{
    type = "SystemAssigned"
  }}

  site_config {{
    always_on = {str(c.tier != "B1").lower()}
    minimum_tls_version = "1.2"
  }}
}}
''' + _autoscale(c, i, ctx)


def _autoscale(c: Component, i: int, ctx: Ctx) -> str:
    most = int(c.settings.get("max_instances", 0))
    if most <= c.instances:
        return ""
    rules = "".join(
        f"""

    rule {{
      metric_trigger {{
        metric_name = "CpuPercentage"
        metric_resource_id = azurerm_service_plan.app_service_{i}.id
        operator = "{op}"
        statistic = "Average"
        threshold = {threshold}
        time_aggregation = "Average"
        time_grain = "PT1M"
        time_window = "PT5M"
      }}

      scale_action {{
        cooldown = "PT5M"
        direction = "{direction}"
        type = "ChangeCount"
        value = 1
      }}
    }}"""
        for op, threshold, direction in (("GreaterThan", 70, "Increase"), ("LessThan", 30, "Decrease"))
    )
    return f'''
# Peak concurrency needs up to {most} instances; scale on CPU between {c.instances} and {most}.
resource "azurerm_monitor_autoscale_setting" "app_service_{i}" {{
  name = "autoscale-{ctx.name}-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  target_resource_id = azurerm_service_plan.app_service_{i}.id

  profile {{
    name = "peak"

    capacity {{
      default = {c.instances}
      minimum = {c.instances}
      maximum = {most}
    }}{rules}
  }}
}}
'''


def _container_apps(c, i, ctx):
    cpu, mem = CA[c.tier]
    zr = (
        f"\n  infrastructure_subnet_id = azurerm_subnet.container_apps{ctx.sfx(c.region)}.id\n  zone_redundancy_enabled = true"
        if c.zone_redundant
        else ""
    )
    return f'''
resource "azurerm_container_app_environment" "container_apps_{i}" {{
  name = "cae-{ctx.name}-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.log_analytics_0.id{zr}
}}

resource "azurerm_container_app" "container_apps_{i}" {{
  name = "ca-{ctx.name}-{i}"
  container_app_environment_id = azurerm_container_app_environment.container_apps_{i}.id
  resource_group_name = azurerm_resource_group.main.name
  revision_mode = "Single"

  identity {{
    type = "SystemAssigned"
  }}

  template {{
    min_replicas = {c.instances}
    max_replicas = {max(c.instances * 3, 3)}

    container {{
      name = "app"
      image = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu = {cpu}
      memory = "{mem}"
    }}
  }}

  ingress {{
    external_enabled = true
    target_port = 80

    traffic_weight {{
      latest_revision = true
      percentage = 100
    }}
  }}
}}
'''


def _functions(c, i, ctx):
    # Zone redundancy is a plan setting; Azure then keeps two always-ready instances, and the host storage must be ZRS.
    zr = "\n  zone_balancing_enabled = true" if c.zone_redundant else ""
    return f'''
resource "azurerm_storage_account" "functions_{i}" {{
  name = "stfn{i}${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  account_tier = "Standard"
  account_replication_type = "{"ZRS" if c.zone_redundant else "LRS"}"
  min_tls_version = "TLS1_2"
}}

resource "azurerm_storage_container" "functions_{i}" {{
  name = "deployments"
  storage_account_id = azurerm_storage_account.functions_{i}.id
}}

resource "azurerm_service_plan" "functions_{i}" {{
  name = "asp-fn-{ctx.name}-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  os_type = "Linux"
  sku_name = "FC1"{zr}
}}

resource "azurerm_function_app_flex_consumption" "functions_{i}" {{
  name = "fn-{ctx.name}-{i}-${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  service_plan_id = azurerm_service_plan.functions_{i}.id
  storage_container_type = "blobContainer"
  storage_container_endpoint = "${{azurerm_storage_account.functions_{i}.primary_blob_endpoint}}${{azurerm_storage_container.functions_{i}.name}}"
  storage_authentication_type = "SystemAssignedIdentity"
  runtime_name = "python"
  runtime_version = "3.12"
  maximum_instance_count = 40
  instance_memory_in_mb = 2048

  app_settings = {{
    AzureWebJobsStorage__accountName = azurerm_storage_account.functions_{i}.name
  }}

  identity {{
    type = "SystemAssigned"
  }}

  site_config {{}}
}}

# The app reaches its storage with its own identity, so no storage key appears anywhere.
resource "azurerm_role_assignment" "functions_{i}_blob" {{
  scope = azurerm_storage_account.functions_{i}.id
  role_definition_name = "Storage Blob Data Owner"
  principal_id = azurerm_function_app_flex_consumption.functions_{i}.identity[0].principal_id
}}

resource "azurerm_role_assignment" "functions_{i}_queue" {{
  scope = azurerm_storage_account.functions_{i}.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id = azurerm_function_app_flex_consumption.functions_{i}.identity[0].principal_id
}}
'''


def _postgres(c, i, ctx):
    head = f'''
resource "azurerm_postgresql_flexible_server" "postgres_{i}" {{
  name = "psql-{ctx.name}-{i}-${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  version = "16"
  sku_name = "{PG_SKU[c.tier]}"
  storage_mb = {pg_storage_gb(c, ctx.w) * 1024}
  public_network_access_enabled = {str(not _private(c)).lower()}'''
    if replica(c):  # a cross-region read replica, promoted to a standalone server on failover
        source = f"azurerm_postgresql_flexible_server.postgres_{ctx.source('postgres')}.id"
        return head + f'\n  create_mode = "Replica"\n  source_server_id = {source}\n}}\n'
    ha = '\n\n  high_availability {\n    mode = "ZoneRedundant"\n    standby_availability_zone = "2"\n  }' if c.zone_redundant else ""
    days = int(min(max(c.settings.get("backup_days", 14), 7), PITR_MAX_DAYS))
    geo = bool(c.settings.get("geo_backup"))
    extra = f"\n  backup_retention_days = {days}\n  geo_redundant_backup_enabled = {str(geo).lower()}"
    if ctx.w.data_growth_pct:
        extra += "\n  auto_grow_enabled = true"
    cmk = ""
    if c.customer_managed_key and ctx.cmk:
        ids = "azurerm_user_assigned_identity.cmk.id" + (", azurerm_user_assigned_identity.cmk_geo.id" if geo and ctx.geo_cmk else "")
        geo_keys = (
            "\n    geo_backup_key_vault_key_id = azurerm_key_vault_key.cmk_geo.id"
            "\n    geo_backup_user_assigned_identity_id = azurerm_user_assigned_identity.cmk_geo.id"
            if geo and ctx.geo_cmk
            else ""
        )
        cmk = (
            f'\n\n  identity {{\n    type = "UserAssigned"\n    identity_ids = [{ids}]\n  }}'
            "\n\n  customer_managed_key {"
            "\n    key_vault_key_id = azurerm_key_vault_key.cmk.id"
            "\n    primary_user_assigned_identity_id = azurerm_user_assigned_identity.cmk.id"
            f"{geo_keys}\n  }}"
        )
    return head + '\n  zone = "1"\n  administrator_login = "pgadmin"\n  administrator_password = var.db_admin_password' + extra + ha + cmk + "\n}\n"


def _azure_sql(c, i, ctx):
    server = f'''
resource "azurerm_mssql_server" "azure_sql_{i}" {{
  name = "sql-{ctx.name}-{i}-${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  version = "12.0"
  administrator_login = "sqladmin"
  administrator_login_password = var.db_admin_password
  minimum_tls_version = "1.2"
  public_network_access_enabled = {str(not _private(c)).lower()}{_sql_cmk(c, ctx)}
}}
'''
    secondary = (
        f'\n  create_mode = "Secondary"\n  creation_source_database_id = azurerm_mssql_database.azure_sql_{ctx.source("azure_sql")}.id'
        if replica(c)
        else ""
    )
    return (
        server
        + f'''
resource "azurerm_mssql_database" "azure_sql_{i}" {{
  name = "db-{ctx.name}"
  server_id = azurerm_mssql_server.azure_sql_{i}.id
  sku_name = "{SQL_SKU[c.tier]}"
  zone_redundant = {str(c.zone_redundant).lower()}{secondary}{_sql_backup(c)}
}}
'''
    )


def _sql_cmk(c: Component, ctx: Ctx) -> str:
    if not (c.customer_managed_key and ctx.cmk) or replica(c):
        return ""
    return (
        "\n  primary_user_assigned_identity_id = azurerm_user_assigned_identity.cmk.id"
        "\n  transparent_data_encryption_key_vault_key_id = azurerm_key_vault_key.cmk.id\n" + ctx.cmk_identity()
    )


def _sql_backup(c: Component) -> str:
    if replica(c):
        return ""
    storage = "Geo" if c.settings.get("geo_backup") else ("Zone" if c.zone_redundant else "Local")
    out = f'\n  storage_account_type = "{storage}"'
    out += f"\n\n  short_term_retention_policy {{\n    retention_days = {int(min(max(c.settings.get('backup_days', 7), 7), PITR_MAX_DAYS))}\n  }}"
    if c.settings.get("ltr_days"):
        weeks = min(520, -(-int(c.settings["ltr_days"]) // 7))
        out += f'\n\n  long_term_retention_policy {{\n    weekly_retention = "P{weeks}W"\n  }}'
    return out


def _cosmos(c, i, ctx):
    if replica(c):
        return ""  # a replica is another geo_location of the primary account
    regions = [c] + [r for r in ctx.of("cosmos_db") if replica(r)]
    geo = "".join(
        f"""

  geo_location {{
    location = "{r.region}"
    failover_priority = {n}
    zone_redundant = {str(r.zone_redundant).lower()}
  }}"""
        for n, r in enumerate(regions)
    )
    serverless = '\n\n  capabilities {\n    name = "EnableServerless"\n  }' if c.tier == "serverless" else ""
    failover = f"\n  automatic_failover_enabled = {str(len(regions) > 1).lower()}"
    return f'''
resource "azurerm_cosmosdb_account" "cosmos_db_{i}" {{
  name = "cosmos-{ctx.name}-{i}-${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  offer_type = "Standard"
  kind = "GlobalDocumentDB"
  public_network_access_enabled = {str(not _private(c)).lower()}{failover}{_cosmos_cmk(c, ctx)}

  consistency_policy {{
    consistency_level = "Session"
  }}{geo}{serverless}
}}

resource "azurerm_cosmosdb_sql_database" "cosmos_db_{i}" {{
  name = "app"
  resource_group_name = azurerm_resource_group.main.name
  account_name = azurerm_cosmosdb_account.cosmos_db_{i}.name
}}
'''


def _cosmos_cmk(c: Component, ctx: Ctx) -> str:
    if not (c.customer_managed_key and ctx.cmk):
        return ""
    return (
        "\n  key_vault_key_id = azurerm_key_vault_key.cmk.versionless_id"
        '\n  default_identity_type = "UserAssignedIdentity=${azurerm_user_assigned_identity.cmk.id}"\n' + ctx.cmk_identity()
    )


def _blob(c, i, ctx):
    return f'''
resource "azurerm_storage_account" "blob_storage_{i}" {{
  name = "st{i}${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  account_tier = "Standard"
  account_kind = "StorageV2"
  account_replication_type = "{STORAGE[c.tier]}"
  min_tls_version = "TLS1_2"
  allow_nested_items_to_be_public = false
  public_network_access_enabled = {str(not _private(c)).lower()}{ctx.cmk_identity() if c.customer_managed_key and ctx.cmk else ""}
}}
''' + _blob_extras(c, i, ctx)


def _blob_extras(c: Component, i: int, ctx: Ctx) -> str:
    out = ""
    if c.customer_managed_key and ctx.cmk:
        out += f"""
resource "azurerm_storage_account_customer_managed_key" "blob_storage_{i}" {{
  storage_account_id = azurerm_storage_account.blob_storage_{i}.id
  key_vault_id = {ctx.vault}.id
  key_name = azurerm_key_vault_key.cmk.name
  user_assigned_identity_id = azurerm_user_assigned_identity.cmk.id
}}
"""
    days = int(c.settings.get("delete_after_days", 0))
    if days:
        out += f"""
# Records are kept for the retention period and then deleted.
resource "azurerm_storage_management_policy" "blob_storage_{i}" {{
  storage_account_id = azurerm_storage_account.blob_storage_{i}.id

  rule {{
    name = "delete-after-retention"
    enabled = true

    filters {{
      blob_types = ["blockBlob"]
    }}

    actions {{
      base_blob {{
        delete_after_days_since_modification_greater_than = {days}
      }}
    }}
  }}
}}
"""
    return out


def _redis(c, i, ctx):
    sku, cap = REDIS[c.tier]
    return f'''
resource "azurerm_redis_cache" "redis_{i}" {{
  name = "redis-{ctx.name}-{i}-${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  capacity = {cap}
  family = "C"
  sku_name = "{sku}"
  minimum_tls_version = "1.2"
  non_ssl_port_enabled = false
}}
'''


def _key_vault(c, i, ctx):
    return f'''
resource "azurerm_key_vault" "key_vault_{i}" {{
  name = "kv{i}${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  tenant_id = data.azurerm_client_config.current.tenant_id
  sku_name = "standard"
  purge_protection_enabled = true
  soft_delete_retention_days = 90
  rbac_authorization_enabled = true
}}
'''


def _logs(c, i, ctx):
    days = int(c.settings.get("retention_days", 30))
    out = LOGS.format(i=i, region=c.region, days=min(max(days, 30), 90))
    if days > 90:
        out += f"""
# Interactive for 90 days, then long-term retention up to {days} days in total.
resource "azurerm_log_analytics_workspace_table" "log_analytics_{i}_diagnostics" {{
  name = "AzureDiagnostics"
  workspace_id = azurerm_log_analytics_workspace.log_analytics_{i}.id
  retention_in_days = 90
  total_retention_in_days = {_total_retention(days)}
}}
"""
    return out


LONG_RETENTION_DAYS = (1095, 1460, 1826, 2191, 2556, 2922, 3288, 3653, 4018, 4383)  # whole years, as Azure accepts them


def _total_retention(days: int) -> int:
    """Total retention Azure accepts: up to 730 days exactly, then whole years from three to twelve."""
    if days <= 730:
        return days
    return next((d for d in LONG_RETENTION_DAYS if d >= days), LONG_RETENTION_DAYS[-1])


def _backup_vault(c, i, ctx):
    """Long-term PostgreSQL backups: a weekly full backup kept in a vault for the retention period."""
    days = int(c.settings.get("retention_days", ctx.w.backup_retention_days))
    servers = [k for k, p in enumerate(ctx.of("postgres")) if not replica(p)]
    out = f'''
resource "azurerm_data_protection_backup_vault" "backup_vault_{i}" {{
  name = "bv-{ctx.name}-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  datastore_type = "VaultStore"
  redundancy = "{"GeoRedundant" if c.tier == "grs" else "LocallyRedundant"}"

  identity {{
    type = "SystemAssigned"
  }}
}}

resource "azurerm_data_protection_backup_policy_postgresql_flexible_server" "backup_vault_{i}" {{
  name = "weekly-{days}d"
  vault_id = azurerm_data_protection_backup_vault.backup_vault_{i}.id
  backup_repeating_time_intervals = ["R/2026-01-04T02:00:00+00:00/P1W"]

  default_retention_rule {{
    life_cycle {{
      duration = "P{days}D"
      data_store_type = "VaultStore"
    }}
  }}
}}

resource "azurerm_role_assignment" "backup_vault_{i}_reader" {{
  scope = azurerm_resource_group.main.id
  role_definition_name = "Reader"
  principal_id = azurerm_data_protection_backup_vault.backup_vault_{i}.identity[0].principal_id
}}
'''
    for k in servers:
        out += f'''
resource "azurerm_role_assignment" "backup_vault_{i}_postgres_{k}" {{
  scope = azurerm_postgresql_flexible_server.postgres_{k}.id
  role_definition_name = "PostgreSQL Flexible Server Long Term Retention Backup Role"
  principal_id = azurerm_data_protection_backup_vault.backup_vault_{i}.identity[0].principal_id
}}

resource "azurerm_data_protection_backup_instance_postgresql_flexible_server" "backup_vault_{i}_postgres_{k}" {{
  name = "postgres-{k}"
  location = "{c.region}"
  vault_id = azurerm_data_protection_backup_vault.backup_vault_{i}.id
  server_id = azurerm_postgresql_flexible_server.postgres_{k}.id
  backup_policy_id = azurerm_data_protection_backup_policy_postgresql_flexible_server.backup_vault_{i}.id
  depends_on = [azurerm_role_assignment.backup_vault_{i}_reader, azurerm_role_assignment.backup_vault_{i}_postgres_{k}]
}}
'''
    return out


def _entra_external_id(c, i, ctx):
    return """
# Microsoft Entra External ID (customer sign-in) is an external tenant created in the Microsoft Entra admin
# center; the azurerm provider has no resource for it. Register the app there and point App Service
# authentication at the tenant's issuer URL.
"""


def _search(c, i, ctx):
    return f'''
resource "azurerm_search_service" "ai_search_{i}" {{
  name = "srch-{ctx.name}-{i}-${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  sku = "{"basic" if c.tier == "basic" else "standard"}"
  replica_count = {c.instances}
  partition_count = 1
}}
'''


def _openai(c, i, ctx):
    model, version, sku = OPENAI[c.tier]
    return f'''
resource "azurerm_cognitive_account" "azure_openai_{i}" {{
  name = "oai-{ctx.name}-{i}-${{random_string.suffix.result}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  kind = "OpenAI"
  sku_name = "S0"
  custom_subdomain_name = "oai-{ctx.name}-{i}-${{random_string.suffix.result}}"

  identity {{
    type = "SystemAssigned"
  }}
}}

resource "azurerm_cognitive_deployment" "azure_openai_{i}" {{
  name = "{model}"
  cognitive_account_id = azurerm_cognitive_account.azure_openai_{i}.id

  model {{
    format = "OpenAI"
    name = "{model}"
    version = "{version}"
  }}

  sku {{
    name = "{sku}"
    capacity = 50
  }}
}}
'''


def _front_door(c, i, ctx):
    premium = c.tier == "premium"
    out = f'''
resource "azurerm_cdn_frontdoor_profile" "front_door_{i}" {{
  name = "afd-{ctx.name}-{i}"
  resource_group_name = azurerm_resource_group.main.name
  sku_name = "{"Premium" if premium else "Standard"}_AzureFrontDoor"
}}

resource "azurerm_cdn_frontdoor_endpoint" "front_door_{i}" {{
  name = "afd-{ctx.name}-{i}-${{random_string.suffix.result}}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.front_door_{i}.id
}}
'''
    hosts = ctx.hosts()
    if hosts:
        # The primary region serves traffic (priority 1); the DR region takes over when health probes fail.
        out += f"""
resource "azurerm_cdn_frontdoor_origin_group" "front_door_{i}" {{
  name = "apps"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.front_door_{i}.id

  load_balancing {{
    sample_size = 4
    successful_samples_required = 3
  }}

  health_probe {{
    path = "/"
    protocol = "Https"
    interval_in_seconds = 30
    request_type = "HEAD"
  }}
}}
"""
        for k, (h, host) in enumerate(hosts):
            out += f"""
resource "azurerm_cdn_frontdoor_origin" "front_door_{i}_{k}" {{
  name = "origin-{k}"
  cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.front_door_{i}.id
  enabled = true
  host_name = {host}
  origin_host_header = {host}
  certificate_name_check_enabled = true
  priority = {1 if h.region == ctx.design.region else 2}
  weight = 1000
}}
"""
        ids = ", ".join(f"azurerm_cdn_frontdoor_origin.front_door_{i}_{k}.id" for k in range(len(hosts)))
        out += f"""
resource "azurerm_cdn_frontdoor_route" "front_door_{i}" {{
  name = "default"
  cdn_frontdoor_endpoint_id = azurerm_cdn_frontdoor_endpoint.front_door_{i}.id
  cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.front_door_{i}.id
  cdn_frontdoor_origin_ids = [{ids}]
  supported_protocols = ["Http", "Https"]
  patterns_to_match = ["/*"]
  forwarding_protocol = "HttpsOnly"
  https_redirect_enabled = true
  link_to_default_domain = true
}}
"""
    if premium:
        out += f"""
resource "azurerm_cdn_frontdoor_firewall_policy" "front_door_{i}" {{
  name = "waf{ctx.name}{i}"
  resource_group_name = azurerm_resource_group.main.name
  sku_name = "Premium_AzureFrontDoor"
  enabled = true
  mode = "Prevention"

  managed_rule {{
    type = "Microsoft_DefaultRuleSet"
    version = "2.1"
    action = "Block"
  }}

  managed_rule {{
    type = "Microsoft_BotManagerRuleSet"
    version = "1.1"
    action = "Block"
  }}
}}

resource "azurerm_cdn_frontdoor_security_policy" "front_door_{i}" {{
  name = "waf"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.front_door_{i}.id

  security_policies {{
    firewall {{
      cdn_frontdoor_firewall_policy_id = azurerm_cdn_frontdoor_firewall_policy.front_door_{i}.id

      association {{
        patterns_to_match = ["/*"]

        domain {{
          cdn_frontdoor_domain_id = azurerm_cdn_frontdoor_endpoint.front_door_{i}.id
        }}
      }}
    }}
  }}
}}
"""
    return out


def _app_gateway(c, i, ctx):
    zones = '\n  zones = ["1", "2", "3"]' if c.zone_redundant else ""
    hosts = [h for _, h in ctx.hosts(c.region)]
    fqdns = f"\n    fqdns = [{', '.join(hosts)}]" if hosts else ""
    return f'''
resource "azurerm_public_ip" "app_gateway_{i}" {{
  name = "pip-agw-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  allocation_method = "Static"
  sku = "Standard"{zones}
}}

resource "azurerm_web_application_firewall_policy" "app_gateway_{i}" {{
  name = "waf-{ctx.name}-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name

  policy_settings {{
    enabled = true
    mode = "Prevention"
  }}

  managed_rules {{
    managed_rule_set {{
      type = "OWASP"
      version = "3.2"
    }}
  }}
}}

resource "azurerm_application_gateway" "app_gateway_{i}" {{
  name = "agw-{ctx.name}-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  firewall_policy_id = azurerm_web_application_firewall_policy.app_gateway_{i}.id{zones}

  sku {{
    name = "WAF_v2"
    tier = "WAF_v2"
  }}

  autoscale_configuration {{
    min_capacity = 1
    max_capacity = 10
  }}

  gateway_ip_configuration {{
    name = "gateway-ip"
    subnet_id = azurerm_subnet.gateway{ctx.sfx(c.region)}.id
  }}

  frontend_port {{
    name = "http"
    port = 80
  }}

  frontend_ip_configuration {{
    name = "public"
    public_ip_address_id = azurerm_public_ip.app_gateway_{i}.id
  }}

  backend_address_pool {{
    name = "app"{fqdns}
  }}

  backend_http_settings {{
    name = "app-https"
    cookie_based_affinity = "Disabled"
    port = 443
    protocol = "Https"
    request_timeout = 30
    pick_host_name_from_backend_address = true
  }}

  # HTTP until you add your TLS certificate (from Key Vault) and switch this listener to HTTPS on 443.
  http_listener {{
    name = "http"
    frontend_ip_configuration_name = "public"
    frontend_port_name = "http"
    protocol = "Http"
  }}

  request_routing_rule {{
    name = "app"
    priority = 100
    rule_type = "Basic"
    http_listener_name = "http"
    backend_address_pool_name = "app"
    backend_http_settings_name = "app-https"
  }}
}}
'''


def _aks(c, i, ctx):
    zones = '\n    zones = ["1", "2", "3"]' if c.zone_redundant else ""
    return f'''
resource "azurerm_kubernetes_cluster" "aks_{i}" {{
  name = "aks-{ctx.name}-{i}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  dns_prefix = "aks-{ctx.name}-{i}"
  sku_tier = "Standard"

  default_node_pool {{
    name = "system"
    vm_size = "{VM[c.tier]}"
    node_count = {c.instances}{zones}
  }}

  identity {{
    type = "SystemAssigned"
  }}
}}
'''


def _vm(c, i, ctx):
    zone = "\n  zone = tostring(count.index % 3 + 1)" if c.zone_redundant else ""  # spread across the three zones
    return f'''
resource "azurerm_network_interface" "vm_{i}" {{
  count = {c.instances}
  name = "nic-vm-{i}-${{count.index}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name

  ip_configuration {{
    name = "internal"
    subnet_id = azurerm_subnet.workloads{ctx.sfx(c.region)}.id
    private_ip_address_allocation = "Dynamic"
  }}
}}

resource "azurerm_linux_virtual_machine" "vm_{i}" {{
  count = {c.instances}
  name = "vm-{ctx.name}-{i}-${{count.index}}"
  location = "{c.region}"
  resource_group_name = azurerm_resource_group.main.name
  size = "{VM[c.tier]}"
  admin_username = "azureuser"
  network_interface_ids = [azurerm_network_interface.vm_{i}[count.index].id]{zone}

  admin_ssh_key {{
    username = "azureuser"
    public_key = var.vm_ssh_public_key
  }}

  os_disk {{
    caching = "ReadWrite"
    storage_account_type = "Premium_LRS"
  }}

  source_image_reference {{
    publisher = "Canonical"
    offer = "ubuntu-24_04-lts"
    sku = "server"
    version = "latest"
  }}
}}
'''


BUILDERS = {
    "app_service": _app_service,
    "container_apps": _container_apps,
    "functions": _functions,
    "postgres": _postgres,
    "azure_sql": _azure_sql,
    "cosmos_db": _cosmos,
    "blob_storage": _blob,
    "redis": _redis,
    "key_vault": _key_vault,
    "log_analytics": _logs,
    "ai_search": _search,
    "azure_openai": _openai,
    "front_door": _front_door,
    "app_gateway": _app_gateway,
    "aks": _aks,
    "vm": _vm,
    "backup_vault": _backup_vault,
    "entra_external_id": _entra_external_id,
}
