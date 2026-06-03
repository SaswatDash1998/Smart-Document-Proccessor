# ================================================================
# Resource Group
# ================================================================

resource "azurerm_resource_group" "main" {
  name     = var.resource_group_name
  location = var.resource_group_location

  tags = {
    Environment = "Production"
    Project     = "DocumentIntelligence"
    ManagedBy   = "Terraform"
  }
}

# ================================================================
# Storage Account + Blob Containers
# ================================================================

resource "azurerm_storage_account" "main" {
  name                     = var.storage_account_name
  resource_group_name      = azurerm_resource_group.main.name
  location                 = var.resources_location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  account_kind             = "StorageV2"
  allow_nested_items_to_be_public  = false
  cross_tenant_replication_enabled = false
  min_tls_version                  = "TLS1_2"

  tags = {
    Environment = "Production"
    Project     = "DocumentIntelligence"
  }
}

resource "azurerm_storage_container" "raw_documents" {
  name                  = "raw-documents"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}

resource "azurerm_storage_container" "graphrag_input" {
  name                  = "graphrag-input"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}

resource "azurerm_storage_container" "graphrag_output" {
  name                  = "graphrag-output"
  storage_account_name  = azurerm_storage_account.main.name
  container_access_type = "private"
}

# ================================================================
# PostgreSQL Flexible Server + Database
# ================================================================

resource "azurerm_postgresql_flexible_server" "main" {
  name                   = var.postgres_server_name
  resource_group_name    = azurerm_resource_group.main.name
  location               = var.resources_location
  version                = "16"
  administrator_login    = var.postgres_admin_username
  administrator_password = var.postgres_admin_password
  
  storage_mb = 32768
  sku_name   = "B_Standard_B1ms"

  zone = "3"

  backup_retention_days        = 7
  geo_redundant_backup_enabled = false

  tags = {
    Environment = "Production"
    Project     = "DocumentIntelligence"
  }
}

# Allow access from Azure services
resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure_services" {
  name = "AllowAllAzureServicesAndResourcesWithinAzureIps_2026-5-15_21-55-53"
  server_id        = azurerm_postgresql_flexible_server.main.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# Allow all IPs for development (update this for production)
/*resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_all" {
  name             = "AllowAll"
  server_id        = azurerm_postgresql_flexible_server.main.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "255.255.255.255"
}*/

# Enable pgvector extension
resource "azurerm_postgresql_flexible_server_configuration" "vector_extension" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "VECTOR"
}

resource "azurerm_postgresql_flexible_server_database" "main" {
  name      = var.postgres_database_name
  server_id = azurerm_postgresql_flexible_server.main.id
  collation = "en_US.utf8"
  charset   = "utf8"
}

# ================================================================
# Container Registry
# ================================================================

resource "azurerm_container_registry" "main" {
  name                = var.acr_name
  resource_group_name = azurerm_resource_group.main.name
  location            = var.resource_group_location
  sku                 = "Basic"
  admin_enabled       = true

  tags = {
    Environment = "Production"
    Project     = "DocumentIntelligence"
  }
}

# ================================================================
# Container Apps Environment + App
# ================================================================

resource "azurerm_container_app_environment" "main" {
  name                = var.container_app_env_name
  location            = var.resources_location
  resource_group_name = azurerm_resource_group.main.name

  tags = {
    Environment = "Production"
    Project     = "DocumentIntelligence"
  }
}

resource "azurerm_container_app" "main" {
  name                         = var.container_app_name
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"

  registry {
    server               = azurerm_container_registry.main.login_server
    username             = azurerm_container_registry.main.admin_username
    password_secret_name = "registry-password"
  }

  secret {
    name  = "registry-password"
    value = azurerm_container_registry.main.admin_password
  }

  secret {
    name  = "cerebras-api-key"
    value = var.cerebras_api_key
  }

  secret {
    name  = "db-password"
    value = var.postgres_admin_password
  }

  secret {
    name  = "storage-conn-str"
    value = var.azure_storage_connection_string
  }

  secret {
    name  = "gemini-api-key"
    value = var.gemini_api_key
  }

  template {
    container {
      name   = "ca-docint"
      image  = "${azurerm_container_registry.main.login_server}/docint:v06"
      cpu    = 1.0
      memory = "2Gi"

      env {
        name  = "DB_HOST"
        value = azurerm_postgresql_flexible_server.main.fqdn
      }

      env {
        name  = "DB_PORT"
        value = "5432"
      }

      env {
        name  = "DB_USER"
        value = var.postgres_admin_username
      }

      env {
        name  = "DB_NAME"
        value = var.postgres_database_name
      }

      env {
        name        = "DB_PASSWORD"
        secret_name = "db-password"
      }

      env {
        name  = "DB_SSL"
        value = "require"
      }

      env {
        name        = "CEREBRAS_API_KEY"
        secret_name = "cerebras-api-key"
      }

      env {
        name        = "CERABRAS_API_KEY"
        secret_name = "cerebras-api-key"
      }

      env {
        name        = "GEMINI_API_KEY"
        secret_name = "gemini-api-key"
      }

      env {
        name        = "AZURE_STORAGE_CONNECTION_STRING"
        secret_name = "storage-conn-str"
      }

      env {
        name  = "GRAPHRAG_ROOT"
        value = "/app/data"
      }

    }

    min_replicas = 1
    max_replicas = 3
  }

  ingress {
    external_enabled = true
    target_port      = 8000

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = {
    Environment = "Production"
    Project     = "DocumentIntelligence"
  }
}
