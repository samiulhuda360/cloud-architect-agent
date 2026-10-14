terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

variable "subscription_id" {
  type        = string
  description = "Azure subscription to deploy into"
}

variable "db_admin_password" {
  type      = string
  sensitive = true
  default   = null
}

variable "vm_ssh_public_key" {
  type    = string
  default = null
}

data "azurerm_client_config" "current" {}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

resource "azurerm_resource_group" "main" {
  name     = "rg-hollinwrenonlinesh"
  location = "uksouth"
  tags     = local.tags
}

variable "required_tags" {
  type        = map(string)
  description = "Tags the company policy requires on every resource: cost-centre, data-classification, owner"

  validation {
    condition     = alltrue([for k in ["cost-centre", "data-classification", "owner"] : length(lookup(var.required_tags, k, "")) > 0])
    error_message = "Set every required tag: cost-centre, data-classification, owner."
  }
}

locals {
  tags = merge({
    "environment" = "prod"
    "workload"    = "Hollin & Wren online shop"
  }, var.required_tags)
}

resource "azurerm_virtual_network" "main" {
  name                = "vnet-main-${random_string.suffix.result}"
  location            = "uksouth"
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.20.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "private_endpoints" {
  name                 = "snet-private-endpoints"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.20.1.0/24"]
}

resource "azurerm_subnet" "container_apps" {
  name                 = "snet-container-apps"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.20.4.0/23"]
}

resource "azurerm_virtual_network" "r1" {
  name                = "vnet-r1-${random_string.suffix.result}"
  location            = "ukwest"
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.21.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "private_endpoints_r1" {
  name                 = "snet-private-endpoints"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.r1.name
  address_prefixes     = ["10.21.1.0/24"]
}

resource "azurerm_private_dns_zone" "blob_storage" {
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob_storage_main" {
  name                  = "link-blob_storage-main"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.blob_storage.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob_storage_r1" {
  name                  = "link-blob_storage-r1"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.blob_storage.name
  virtual_network_id    = azurerm_virtual_network.r1.id
}

resource "azurerm_private_dns_zone" "postgres" {
  name                = "privatelink.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres_main" {
  name                  = "link-postgres-main"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres_r1" {
  name                  = "link-postgres-r1"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.r1.id
}

resource "azurerm_user_assigned_identity" "cmk" {
  name                = "id-cmk-hollinwrenonlinesh"
  location            = "uksouth"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# Whoever runs Terraform creates the key; the identity only wraps and unwraps data keys with it.
resource "azurerm_role_assignment" "cmk_officer" {
  scope                = azurerm_key_vault.key_vault_0.id
  role_definition_name = "Key Vault Crypto Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "cmk_user" {
  scope                = azurerm_key_vault.key_vault_0.id
  role_definition_name = "Key Vault Crypto Service Encryption User"
  principal_id         = azurerm_user_assigned_identity.cmk.principal_id
}

resource "azurerm_key_vault_key" "cmk" {
  name         = "cmk-data"
  key_vault_id = azurerm_key_vault.key_vault_0.id
  key_type     = "RSA"
  key_size     = 3072
  key_opts     = ["unwrapKey", "wrapKey"]
  depends_on   = [azurerm_role_assignment.cmk_officer]

  rotation_policy {
    expire_after         = "P2Y"
    notify_before_expiry = "P30D"

    automatic {
      time_before_expiry = "P30D"
    }
  }
}

# Geo-redundant backups of a server encrypted with a customer-managed key need a key in the paired region as well.
resource "azurerm_key_vault" "cmk_geo" {
  name                       = "kvgeo${random_string.suffix.result}"
  location                   = "ukwest"
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  purge_protection_enabled   = true
  soft_delete_retention_days = 90
  rbac_authorization_enabled = true
  tags                       = local.tags
}

resource "azurerm_user_assigned_identity" "cmk_geo" {
  name                = "id-cmk-geo-hollinwrenonlinesh"
  location            = "ukwest"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# Whoever runs Terraform creates the key; the identity only wraps and unwraps data keys with it.
resource "azurerm_role_assignment" "cmk_geo_officer" {
  scope                = azurerm_key_vault.cmk_geo.id
  role_definition_name = "Key Vault Crypto Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "cmk_geo_user" {
  scope                = azurerm_key_vault.cmk_geo.id
  role_definition_name = "Key Vault Crypto Service Encryption User"
  principal_id         = azurerm_user_assigned_identity.cmk_geo.principal_id
}

resource "azurerm_key_vault_key" "cmk_geo" {
  name         = "cmk-geo-data"
  key_vault_id = azurerm_key_vault.cmk_geo.id
  key_type     = "RSA"
  key_size     = 3072
  key_opts     = ["unwrapKey", "wrapKey"]
  depends_on   = [azurerm_role_assignment.cmk_geo_officer]

  rotation_policy {
    expire_after         = "P2Y"
    notify_before_expiry = "P30D"

    automatic {
      time_before_expiry = "P30D"
    }
  }
}

resource "azurerm_container_app_environment" "container_apps_0" {
  name                       = "cae-hollinwrenonlinesh-0"
  location                   = "uksouth"
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.log_analytics_0.id
  infrastructure_subnet_id   = azurerm_subnet.container_apps.id
  zone_redundancy_enabled    = true
  tags                       = local.tags
}

resource "azurerm_container_app" "container_apps_0" {
  name                         = "ca-hollinwrenonlinesh-0"
  container_app_environment_id = azurerm_container_app_environment.container_apps_0.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"

  identity {
    type = "SystemAssigned"
  }

  template {
    min_replicas = 3
    max_replicas = 9

    container {
      name   = "app"
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 1
      memory = "2Gi"
    }
  }

  ingress {
    external_enabled = true
    target_port      = 80

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  tags = local.tags
}

resource "azurerm_storage_account" "functions_0" {
  name                     = "stfn0${random_string.suffix.result}"
  location                 = "uksouth"
  resource_group_name      = azurerm_resource_group.main.name
  account_tier             = "Standard"
  account_replication_type = "ZRS"
  min_tls_version          = "TLS1_2"
  tags                     = local.tags
}

resource "azurerm_storage_container" "functions_0" {
  name               = "deployments"
  storage_account_id = azurerm_storage_account.functions_0.id
}

resource "azurerm_service_plan" "functions_0" {
  name                   = "asp-fn-hollinwrenonlinesh-0"
  location               = "uksouth"
  resource_group_name    = azurerm_resource_group.main.name
  os_type                = "Linux"
  sku_name               = "FC1"
  zone_balancing_enabled = true
  tags                   = local.tags
}

resource "azurerm_function_app_flex_consumption" "functions_0" {
  name                        = "fn-hollinwrenonlinesh-0-${random_string.suffix.result}"
  location                    = "uksouth"
  resource_group_name         = azurerm_resource_group.main.name
  service_plan_id             = azurerm_service_plan.functions_0.id
  storage_container_type      = "blobContainer"
  storage_container_endpoint  = "${azurerm_storage_account.functions_0.primary_blob_endpoint}${azurerm_storage_container.functions_0.name}"
  storage_authentication_type = "SystemAssignedIdentity"
  runtime_name                = "python"
  runtime_version             = "3.12"
  maximum_instance_count      = 40
  instance_memory_in_mb       = 2048

  app_settings = {
    AzureWebJobsStorage__accountName = azurerm_storage_account.functions_0.name
  }

  identity {
    type = "SystemAssigned"
  }

  site_config {}
  tags = local.tags
}

# The app reaches its storage with its own identity, so no storage key appears anywhere.
resource "azurerm_role_assignment" "functions_0_blob" {
  scope                = azurerm_storage_account.functions_0.id
  role_definition_name = "Storage Blob Data Owner"
  principal_id         = azurerm_function_app_flex_consumption.functions_0.identity[0].principal_id
}

resource "azurerm_role_assignment" "functions_0_queue" {
  scope                = azurerm_storage_account.functions_0.id
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = azurerm_function_app_flex_consumption.functions_0.identity[0].principal_id
}

resource "azurerm_postgresql_flexible_server" "postgres_0" {
  name                          = "psql-hollinwrenonlinesh-0-${random_string.suffix.result}"
  location                      = "uksouth"
  resource_group_name           = azurerm_resource_group.main.name
  version                       = "16"
  sku_name                      = "GP_Standard_D4ds_v5"
  storage_mb                    = 262144
  public_network_access_enabled = false
  zone                          = "1"
  administrator_login           = "pgadmin"
  administrator_password        = var.db_admin_password
  backup_retention_days         = 35
  geo_redundant_backup_enabled  = true
  auto_grow_enabled             = true

  high_availability {
    mode                      = "ZoneRedundant"
    standby_availability_zone = "2"
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.cmk.id, azurerm_user_assigned_identity.cmk_geo.id]
  }

  customer_managed_key {
    key_vault_key_id                     = azurerm_key_vault_key.cmk.id
    primary_user_assigned_identity_id    = azurerm_user_assigned_identity.cmk.id
    geo_backup_key_vault_key_id          = azurerm_key_vault_key.cmk_geo.id
    geo_backup_user_assigned_identity_id = azurerm_user_assigned_identity.cmk_geo.id
  }

  tags = local.tags
}

resource "azurerm_monitor_diagnostic_setting" "postgres_0_audit" {
  name                       = "audit-to-log-analytics"
  target_resource_id         = azurerm_postgresql_flexible_server.postgres_0.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.log_analytics_0.id

  enabled_log {
    category_group = "allLogs"
  }
}

resource "azurerm_private_endpoint" "postgres_0" {
  name                = "pe-postgres-0"
  location            = "uksouth"
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id

  private_service_connection {
    name                           = "psc-postgres-0"
    private_connection_resource_id = azurerm_postgresql_flexible_server.postgres_0.id
    subresource_names              = ["postgresqlServer"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.postgres.id]
  }

  tags = local.tags
}

resource "azurerm_storage_account" "blob_storage_0" {
  name                            = "st0${random_string.suffix.result}"
  location                        = "uksouth"
  resource_group_name             = azurerm_resource_group.main.name
  account_tier                    = "Standard"
  account_kind                    = "StorageV2"
  account_replication_type        = "GRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  public_network_access_enabled   = false
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.cmk.id]
  }

  tags = local.tags
}

resource "azurerm_storage_account_customer_managed_key" "blob_storage_0" {
  storage_account_id        = azurerm_storage_account.blob_storage_0.id
  key_vault_id              = azurerm_key_vault.key_vault_0.id
  key_name                  = azurerm_key_vault_key.cmk.name
  user_assigned_identity_id = azurerm_user_assigned_identity.cmk.id
}

# Records are kept for the retention period and then deleted.
resource "azurerm_storage_management_policy" "blob_storage_0" {
  storage_account_id = azurerm_storage_account.blob_storage_0.id

  rule {
    name    = "delete-after-retention"
    enabled = true

    filters {
      blob_types = ["blockBlob"]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 2190
      }
    }
  }
}

resource "azurerm_monitor_diagnostic_setting" "blob_storage_0_audit" {
  name                       = "audit-to-log-analytics"
  target_resource_id         = "${azurerm_storage_account.blob_storage_0.id}/blobServices/default"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.log_analytics_0.id

  enabled_log {
    category_group = "audit"
  }
}

resource "azurerm_private_endpoint" "blob_storage_0" {
  name                = "pe-blob_storage-0"
  location            = "uksouth"
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id

  private_service_connection {
    name                           = "psc-blob_storage-0"
    private_connection_resource_id = azurerm_storage_account.blob_storage_0.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob_storage.id]
  }

  tags = local.tags
}

resource "azurerm_redis_cache" "redis_0" {
  name                 = "redis-hollinwrenonlinesh-0-${random_string.suffix.result}"
  location             = "uksouth"
  resource_group_name  = azurerm_resource_group.main.name
  capacity             = 0
  family               = "C"
  sku_name             = "Standard"
  minimum_tls_version  = "1.2"
  non_ssl_port_enabled = false
  tags                 = local.tags
}

# Microsoft Entra External ID (customer sign-in) is an external tenant created in the Microsoft Entra admin
# center; the azurerm provider has no resource for it. Register the app there and point App Service
# authentication at the tenant's issuer URL.

resource "azurerm_container_app_environment" "container_apps_1" {
  name                       = "cae-hollinwrenonlinesh-1"
  location                   = "ukwest"
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.log_analytics_0.id
  tags                       = local.tags
}

resource "azurerm_container_app" "container_apps_1" {
  name                         = "ca-hollinwrenonlinesh-1"
  container_app_environment_id = azurerm_container_app_environment.container_apps_1.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"

  identity {
    type = "SystemAssigned"
  }

  template {
    min_replicas = 1
    max_replicas = 3

    container {
      name   = "app"
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 1
      memory = "2Gi"
    }
  }

  ingress {
    external_enabled = true
    target_port      = 80

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  tags = local.tags
}

resource "azurerm_postgresql_flexible_server" "postgres_1" {
  name                          = "psql-hollinwrenonlinesh-1-${random_string.suffix.result}"
  location                      = "ukwest"
  resource_group_name           = azurerm_resource_group.main.name
  version                       = "16"
  sku_name                      = "GP_Standard_D4ds_v5"
  storage_mb                    = 262144
  public_network_access_enabled = false
  create_mode                   = "Replica"
  source_server_id              = azurerm_postgresql_flexible_server.postgres_0.id
  tags                          = local.tags
}

resource "azurerm_private_endpoint" "postgres_1" {
  name                = "pe-postgres-1"
  location            = "ukwest"
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints_r1.id

  private_service_connection {
    name                           = "psc-postgres-1"
    private_connection_resource_id = azurerm_postgresql_flexible_server.postgres_1.id
    subresource_names              = ["postgresqlServer"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.postgres.id]
  }

  tags = local.tags
}

resource "azurerm_cdn_frontdoor_profile" "front_door_0" {
  name                = "afd-hollinwrenonlinesh-0"
  resource_group_name = azurerm_resource_group.main.name
  sku_name            = "Premium_AzureFrontDoor"
  tags                = local.tags
}

resource "azurerm_cdn_frontdoor_endpoint" "front_door_0" {
  name                     = "afd-hollinwrenonlinesh-0-${random_string.suffix.result}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.front_door_0.id
}

resource "azurerm_cdn_frontdoor_origin_group" "front_door_0" {
  name                     = "apps"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.front_door_0.id

  load_balancing {
    sample_size                 = 4
    successful_samples_required = 3
  }

  health_probe {
    path                = "/"
    protocol            = "Https"
    interval_in_seconds = 30
    request_type        = "HEAD"
  }
}

resource "azurerm_cdn_frontdoor_origin" "front_door_0_0" {
  name                           = "origin-0"
  cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.front_door_0.id
  enabled                        = true
  host_name                      = azurerm_container_app.container_apps_0.ingress[0].fqdn
  origin_host_header             = azurerm_container_app.container_apps_0.ingress[0].fqdn
  certificate_name_check_enabled = true
  priority                       = 1
  weight                         = 1000
}

resource "azurerm_cdn_frontdoor_origin" "front_door_0_1" {
  name                           = "origin-1"
  cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.front_door_0.id
  enabled                        = true
  host_name                      = azurerm_container_app.container_apps_1.ingress[0].fqdn
  origin_host_header             = azurerm_container_app.container_apps_1.ingress[0].fqdn
  certificate_name_check_enabled = true
  priority                       = 2
  weight                         = 1000
}

resource "azurerm_cdn_frontdoor_route" "front_door_0" {
  name                          = "default"
  cdn_frontdoor_endpoint_id     = azurerm_cdn_frontdoor_endpoint.front_door_0.id
  cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.front_door_0.id
  cdn_frontdoor_origin_ids      = [azurerm_cdn_frontdoor_origin.front_door_0_0.id, azurerm_cdn_frontdoor_origin.front_door_0_1.id]
  supported_protocols           = ["Http", "Https"]
  patterns_to_match             = ["/*"]
  forwarding_protocol           = "HttpsOnly"
  https_redirect_enabled        = true
  link_to_default_domain        = true
}

resource "azurerm_cdn_frontdoor_firewall_policy" "front_door_0" {
  name                = "wafhollinwrenonlinesh0"
  resource_group_name = azurerm_resource_group.main.name
  sku_name            = "Premium_AzureFrontDoor"
  enabled             = true
  mode                = "Prevention"

  managed_rule {
    type    = "Microsoft_DefaultRuleSet"
    version = "2.1"
    action  = "Block"
  }

  managed_rule {
    type    = "Microsoft_BotManagerRuleSet"
    version = "1.1"
    action  = "Block"
  }
}

resource "azurerm_cdn_frontdoor_security_policy" "front_door_0" {
  name                     = "waf"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.front_door_0.id

  security_policies {
    firewall {
      cdn_frontdoor_firewall_policy_id = azurerm_cdn_frontdoor_firewall_policy.front_door_0.id

      association {
        patterns_to_match = ["/*"]

        domain {
          cdn_frontdoor_domain_id = azurerm_cdn_frontdoor_endpoint.front_door_0.id
        }
      }
    }
  }
}

resource "azurerm_key_vault" "key_vault_0" {
  name                       = "kv0${random_string.suffix.result}"
  location                   = "uksouth"
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  purge_protection_enabled   = true
  soft_delete_retention_days = 90
  rbac_authorization_enabled = true
  tags                       = local.tags
}

resource "azurerm_monitor_diagnostic_setting" "key_vault_0_audit" {
  name                       = "audit-to-log-analytics"
  target_resource_id         = azurerm_key_vault.key_vault_0.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.log_analytics_0.id

  enabled_log {
    category_group = "audit"
  }
}

resource "azurerm_log_analytics_workspace" "log_analytics_0" {
  name                = "log-${random_string.suffix.result}-0"
  location            = "uksouth"
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 90
  tags                = local.tags
}

# Interactive for 90 days, then long-term retention up to 365 days in total.
resource "azurerm_log_analytics_workspace_table" "log_analytics_0_diagnostics" {
  name                    = "AzureDiagnostics"
  workspace_id            = azurerm_log_analytics_workspace.log_analytics_0.id
  retention_in_days       = 90
  total_retention_in_days = 365
}
