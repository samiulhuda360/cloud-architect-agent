"""Turn a design into Terraform for the azurerm provider (v4).

The output is a starting point a platform team can review and apply: one resource group; every
component with its tier, zone redundancy and instance count; managed identities and Key Vault;
private endpoints with private DNS zones, a virtual network in each region that needs one, and App
Service VNet integration so the apps can reach them; and, when the design has a DR region, database
replicas there and Front Door with priority failover between the regions. Secrets are input
variables, never literals. Every generated file passes `terraform validate` and `terraform fmt`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .catalogue import pg_storage_gb
from .models import Component, Design, Workload

PG_SKU = {"B1ms": "B_Standard_B1ms", "B2s": "B_Standard_B2s", "D2ds_v5": "GP_Standard_D2ds_v5", "D4ds_v5": "GP_Standard_D4ds_v5"}
SQL_SKU = {"S0": "S0", "S2": "S2", "GP_2vcore": "GP_Gen5_2"}
APP_SKU = {"B1": "B1", "S1": "S1", "P0v3": "P0v3", "P1v3": "P1v3", "P2v3": "P2v3"}
REDIS = {"basic_c0": ("Basic", 0), "standard_c0": ("Standard", 0), "standard_c1": ("Standard", 1)}
STORAGE = {"hot_lrs": "LRS", "hot_zrs": "ZRS", "hot_grs": "GRS"}
CA = {"0.5vcpu": (0.5, "1Gi"), "1vcpu": (1, "2Gi"), "2vcpu": (2, "4Gi")}
OPENAI = {"gpt-4o-mini_global": ("gpt-4o-mini", "2024-07-18", "GlobalStandard"), "o4-mini_datazone": ("o4-mini", "2025-04-16", "DataZoneStandard")}
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
    ctx = Ctx(design, w, _slug(w.name), index, vnets, private)

    blocks = [HEADER.format(name=ctx.name, region=design.region)]
    blocks += [_vnet(vnets[r], r, n, subnets[r]) for n, r in enumerate(regions)]
    blocks += [_dns_zone(s, list(vnets.values())) for s in sorted({c.service for c in comps if _private(c)})]
    if not ctx.of("log_analytics") and ctx.of("container_apps"):
        blocks.append(LOGS.format(i=0, region=design.region))
    for c in comps:
        i = index[id(c)]
        if c.service in BUILDERS:
            blocks.append(BUILDERS[c.service](c, i, ctx))
        if _private(c):
            blocks.append(
                PRIVATE_ENDPOINT.format(
                    svc=c.service, i=i, group=PE_GROUP[c.service], target=TARGET[c.service].format(i=i), region=c.region, sfx=ctx.sfx(c.region)
                )
            )
    return _fmt("\n".join(b.strip() + "\n" for b in blocks if b.strip()))


_ATTR = re.compile(r"^( *)([A-Za-z_][\w-]*) *= *(.*)$")


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
  retention_in_days = 30
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
    return (
        head
        + '\n  zone = "1"\n  administrator_login = "pgadmin"\n  administrator_password = var.db_admin_password\n  backup_retention_days = 14'
        + ha
        + "\n}\n"
    )


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
  public_network_access_enabled = {str(not _private(c)).lower()}
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
  zone_redundant = {str(c.zone_redundant).lower()}{secondary}
}}
'''
    )


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
  public_network_access_enabled = {str(not _private(c)).lower()}{failover}

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
  public_network_access_enabled = {str(not _private(c)).lower()}
}}
'''


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
}}
'''


def _logs(c, i, ctx):
    return LOGS.format(i=i, region=c.region)


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
}
