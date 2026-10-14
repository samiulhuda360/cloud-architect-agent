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
  name     = "rg-kestrelmooreclient"
  location = "australiaeast"
  tags     = local.tags
}

locals {
  tags = {
    "environment" = "prod"
    "workload"    = "Kestrel & Moore client portal"
  }
}

resource "azurerm_virtual_network" "main" {
  name                = "vnet-main-${random_string.suffix.result}"
  location            = "australiaeast"
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

resource "azurerm_subnet" "gateway" {
  name                 = "snet-app-gateway"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.20.3.0/24"]
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

resource "azurerm_service_plan" "app_service_0" {
  name                   = "asp-kestrelmooreclient-0"
  location               = "australiaeast"
  resource_group_name    = azurerm_resource_group.main.name
  os_type                = "Linux"
  sku_name               = "S1"
  worker_count           = 1
  zone_balancing_enabled = false
  tags                   = local.tags
}

resource "azurerm_linux_web_app" "app_service_0" {
  name                      = "app-kestrelmooreclient-0-${random_string.suffix.result}"
  location                  = "australiaeast"
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
  location                 = "australiaeast"
  resource_group_name      = azurerm_resource_group.main.name
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"
  tags                     = local.tags
}

resource "azurerm_storage_container" "functions_0" {
  name               = "deployments"
  storage_account_id = azurerm_storage_account.functions_0.id
}

resource "azurerm_service_plan" "functions_0" {
  name                = "asp-fn-kestrelmooreclient-0"
  location            = "australiaeast"
  resource_group_name = azurerm_resource_group.main.name
  os_type             = "Linux"
  sku_name            = "FC1"
  tags                = local.tags
}

resource "azurerm_function_app_flex_consumption" "functions_0" {
  name                        = "fn-kestrelmooreclient-0-${random_string.suffix.result}"
  location                    = "australiaeast"
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
  name                          = "psql-kestrelmooreclient-0-${random_string.suffix.result}"
  location                      = "australiaeast"
  resource_group_name           = azurerm_resource_group.main.name
  version                       = "16"
  sku_name                      = "GP_Standard_D2ds_v5"
  storage_mb                    = 32768
  public_network_access_enabled = false
  zone                          = "1"
  administrator_login           = "pgadmin"
  administrator_password        = var.db_admin_password
  backup_retention_days         = 30
  geo_redundant_backup_enabled  = false
  auto_grow_enabled             = true
  tags                          = local.tags
}

resource "azurerm_private_endpoint" "postgres_0" {
  name                = "pe-postgres-0"
  location            = "australiaeast"
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
  location                        = "australiaeast"
  resource_group_name             = azurerm_resource_group.main.name
  account_tier                    = "Standard"
  account_kind                    = "StorageV2"
  account_replication_type        = "LRS"
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
        delete_after_days_since_modification_greater_than = 2555
      }
    }
  }
}

resource "azurerm_private_endpoint" "blob_storage_0" {
  name                = "pe-blob_storage-0"
  location            = "australiaeast"
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

resource "azurerm_public_ip" "app_gateway_0" {
  name                = "pip-agw-0"
  location            = "australiaeast"
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
}

resource "azurerm_web_application_firewall_policy" "app_gateway_0" {
  name                = "waf-kestrelmooreclient-0"
  location            = "australiaeast"
  resource_group_name = azurerm_resource_group.main.name

  policy_settings {
    enabled = true
    mode    = "Prevention"
  }

  managed_rules {
    managed_rule_set {
      type    = "OWASP"
      version = "3.2"
    }
  }

  tags = local.tags
}

resource "azurerm_application_gateway" "app_gateway_0" {
  name                = "agw-kestrelmooreclient-0"
  location            = "australiaeast"
  resource_group_name = azurerm_resource_group.main.name
  firewall_policy_id  = azurerm_web_application_firewall_policy.app_gateway_0.id

  sku {
    name = "WAF_v2"
    tier = "WAF_v2"
  }

  autoscale_configuration {
    min_capacity = 1
    max_capacity = 10
  }

  gateway_ip_configuration {
    name      = "gateway-ip"
    subnet_id = azurerm_subnet.gateway.id
  }

  frontend_port {
    name = "http"
    port = 80
  }

  frontend_ip_configuration {
    name                 = "public"
    public_ip_address_id = azurerm_public_ip.app_gateway_0.id
  }

  backend_address_pool {
    name  = "app"
    fqdns = [azurerm_linux_web_app.app_service_0.default_hostname]
  }

  backend_http_settings {
    name                                = "app-https"
    cookie_based_affinity               = "Disabled"
    port                                = 443
    protocol                            = "Https"
    request_timeout                     = 30
    pick_host_name_from_backend_address = true
  }

  # HTTP until you add your TLS certificate (from Key Vault) and switch this listener to HTTPS on 443.
  http_listener {
    name                           = "http"
    frontend_ip_configuration_name = "public"
    frontend_port_name             = "http"
    protocol                       = "Http"
  }

  request_routing_rule {
    name                       = "app"
    priority                   = 100
    rule_type                  = "Basic"
    http_listener_name         = "http"
    backend_address_pool_name  = "app"
    backend_http_settings_name = "app-https"
  }

  tags = local.tags
}

resource "azurerm_key_vault" "key_vault_0" {
  name                       = "kv0${random_string.suffix.result}"
  location                   = "australiaeast"
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  purge_protection_enabled   = true
  soft_delete_retention_days = 90
  rbac_authorization_enabled = true
  tags                       = local.tags
}

resource "azurerm_log_analytics_workspace" "log_analytics_0" {
  name                = "log-${random_string.suffix.result}-0"
  location            = "australiaeast"
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}
