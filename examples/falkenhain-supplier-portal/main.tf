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
  name     = "rg-falkenhainsupplier"
  location = "germanywestcentral"
  tags     = local.tags
}

locals {
  tags = {
    "environment" = "prod"
    "workload"    = "Falkenhain supplier portal"
  }
}

resource "azurerm_virtual_network" "main" {
  name                = "vnet-main-${random_string.suffix.result}"
  location            = "germanywestcentral"
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

resource "azurerm_subnet" "app_integration" {
  name                 = "snet-app-integration"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.20.6.0/24"]

  delegation {
    name = "delegation"

    service_delegation {
      name    = "Microsoft.Web/serverFarms"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }
  }
}

resource "azurerm_virtual_network" "r1" {
  name                = "vnet-r1-${random_string.suffix.result}"
  location            = "francecentral"
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

resource "azurerm_subnet" "app_integration_r1" {
  name                 = "snet-app-integration"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.r1.name
  address_prefixes     = ["10.21.6.0/24"]

  delegation {
    name = "delegation"

    service_delegation {
      name    = "Microsoft.Web/serverFarms"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }
  }
}

resource "azurerm_private_dns_zone" "azure_sql" {
  name                = "privatelink.database.windows.net"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "azure_sql_main" {
  name                  = "link-azure_sql-main"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.azure_sql.name
  virtual_network_id    = azurerm_virtual_network.main.id
}

resource "azurerm_private_dns_zone_virtual_network_link" "azure_sql_r1" {
  name                  = "link-azure_sql-r1"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.azure_sql.name
  virtual_network_id    = azurerm_virtual_network.r1.id
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

resource "azurerm_service_plan" "app_service_0" {
  name                   = "asp-falkenhainsupplier-0"
  location               = "germanywestcentral"
  resource_group_name    = azurerm_resource_group.main.name
  os_type                = "Linux"
  sku_name               = "P1v3"
  worker_count           = 3
  zone_balancing_enabled = true
  tags                   = local.tags
}

resource "azurerm_linux_web_app" "app_service_0" {
  name                      = "app-falkenhainsupplier-0-${random_string.suffix.result}"
  location                  = "germanywestcentral"
  resource_group_name       = azurerm_resource_group.main.name
  service_plan_id           = azurerm_service_plan.app_service_0.id
  https_only                = true
  virtual_network_subnet_id = azurerm_subnet.app_integration.id

  identity {
    type = "SystemAssigned"
  }

  site_config {
    always_on           = true
    minimum_tls_version = "1.2"
  }

  tags = local.tags
}

resource "azurerm_storage_account" "functions_0" {
  name                     = "stfn0${random_string.suffix.result}"
  location                 = "germanywestcentral"
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
  name                   = "asp-fn-falkenhainsupplier-0"
  location               = "germanywestcentral"
  resource_group_name    = azurerm_resource_group.main.name
  os_type                = "Linux"
  sku_name               = "FC1"
  zone_balancing_enabled = true
  tags                   = local.tags
}

resource "azurerm_function_app_flex_consumption" "functions_0" {
  name                        = "fn-falkenhainsupplier-0-${random_string.suffix.result}"
  location                    = "germanywestcentral"
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

resource "azurerm_mssql_server" "azure_sql_0" {
  name                          = "sql-falkenhainsupplier-0-${random_string.suffix.result}"
  location                      = "germanywestcentral"
  resource_group_name           = azurerm_resource_group.main.name
  version                       = "12.0"
  administrator_login           = "sqladmin"
  administrator_login_password  = var.db_admin_password
  minimum_tls_version           = "1.2"
  public_network_access_enabled = false
  tags                          = local.tags
}

resource "azurerm_mssql_database" "azure_sql_0" {
  name                 = "db-falkenhainsupplier"
  server_id            = azurerm_mssql_server.azure_sql_0.id
  sku_name             = "GP_Gen5_2"
  zone_redundant       = true
  storage_account_type = "Geo"

  short_term_retention_policy {
    retention_days = 35
  }

  tags = local.tags
}

resource "azurerm_monitor_diagnostic_setting" "azure_sql_0_audit" {
  name                       = "audit-to-log-analytics"
  target_resource_id         = azurerm_mssql_database.azure_sql_0.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.log_analytics_0.id

  enabled_log {
    category_group = "audit"
  }
}

resource "azurerm_private_endpoint" "azure_sql_0" {
  name                = "pe-azure_sql-0"
  location            = "germanywestcentral"
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id

  private_service_connection {
    name                           = "psc-azure_sql-0"
    private_connection_resource_id = azurerm_mssql_server.azure_sql_0.id
    subresource_names              = ["sqlServer"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.azure_sql.id]
  }

  tags = local.tags
}

resource "azurerm_storage_account" "blob_storage_0" {
  name                            = "st0${random_string.suffix.result}"
  location                        = "germanywestcentral"
  resource_group_name             = azurerm_resource_group.main.name
  account_tier                    = "Standard"
  account_kind                    = "StorageV2"
  account_replication_type        = "GRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  public_network_access_enabled   = false
  tags                            = local.tags
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
        delete_after_days_since_modification_greater_than = 3650
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
  location            = "germanywestcentral"
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

# Microsoft Entra External ID (customer sign-in) is an external tenant created in the Microsoft Entra admin
# center; the azurerm provider has no resource for it. Register the app there and point App Service
# authentication at the tenant's issuer URL.

resource "azurerm_service_plan" "app_service_1" {
  name                   = "asp-falkenhainsupplier-1"
  location               = "francecentral"
  resource_group_name    = azurerm_resource_group.main.name
  os_type                = "Linux"
  sku_name               = "P1v3"
  worker_count           = 1
  zone_balancing_enabled = false
  tags                   = local.tags
}

resource "azurerm_linux_web_app" "app_service_1" {
  name                      = "app-falkenhainsupplier-1-${random_string.suffix.result}"
  location                  = "francecentral"
  resource_group_name       = azurerm_resource_group.main.name
  service_plan_id           = azurerm_service_plan.app_service_1.id
  https_only                = true
  virtual_network_subnet_id = azurerm_subnet.app_integration_r1.id

  identity {
    type = "SystemAssigned"
  }

  site_config {
    always_on           = true
    minimum_tls_version = "1.2"
  }

  tags = local.tags
}

resource "azurerm_mssql_server" "azure_sql_1" {
  name                          = "sql-falkenhainsupplier-1-${random_string.suffix.result}"
  location                      = "francecentral"
  resource_group_name           = azurerm_resource_group.main.name
  version                       = "12.0"
  administrator_login           = "sqladmin"
  administrator_login_password  = var.db_admin_password
  minimum_tls_version           = "1.2"
  public_network_access_enabled = false
  tags                          = local.tags
}

resource "azurerm_mssql_database" "azure_sql_1" {
  name                        = "db-falkenhainsupplier"
  server_id                   = azurerm_mssql_server.azure_sql_1.id
  sku_name                    = "GP_Gen5_2"
  zone_redundant              = false
  create_mode                 = "Secondary"
  creation_source_database_id = azurerm_mssql_database.azure_sql_0.id
  tags                        = local.tags
}

resource "azurerm_private_endpoint" "azure_sql_1" {
  name                = "pe-azure_sql-1"
  location            = "francecentral"
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints_r1.id

  private_service_connection {
    name                           = "psc-azure_sql-1"
    private_connection_resource_id = azurerm_mssql_server.azure_sql_1.id
    subresource_names              = ["sqlServer"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.azure_sql.id]
  }

  tags = local.tags
}

resource "azurerm_cdn_frontdoor_profile" "front_door_0" {
  name                = "afd-falkenhainsupplier-0"
  resource_group_name = azurerm_resource_group.main.name
  sku_name            = "Premium_AzureFrontDoor"
  tags                = local.tags
}

resource "azurerm_cdn_frontdoor_endpoint" "front_door_0" {
  name                     = "afd-falkenhainsupplier-0-${random_string.suffix.result}"
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
  host_name                      = azurerm_linux_web_app.app_service_0.default_hostname
  origin_host_header             = azurerm_linux_web_app.app_service_0.default_hostname
  certificate_name_check_enabled = true
  priority                       = 1
  weight                         = 1000
}

resource "azurerm_cdn_frontdoor_origin" "front_door_0_1" {
  name                           = "origin-1"
  cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.front_door_0.id
  enabled                        = true
  host_name                      = azurerm_linux_web_app.app_service_1.default_hostname
  origin_host_header             = azurerm_linux_web_app.app_service_1.default_hostname
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
  name                = "waffalkenhainsupplier0"
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
  location                   = "germanywestcentral"
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
  location            = "germanywestcentral"
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}
