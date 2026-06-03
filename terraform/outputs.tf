# ================================================================
# Outputs
# ================================================================

output "resource_group_name" {
  description = "Resource group name"
  value       = azurerm_resource_group.main.name
}

output "location" {
  description = "Azure region"
  value       = azurerm_resource_group.main.location
}

# Storage
output "storage_account_name" {
  description = "Storage account name"
  value       = azurerm_storage_account.main.name
}

output "storage_account_primary_connection_string" {
  description = "Storage account connection string"
  value       = azurerm_storage_account.main.primary_connection_string
  sensitive   = true
}

output "blob_containers" {
  description = "Blob container names"
  value = {
    raw_documents  = azurerm_storage_container.raw_documents.name
    graphrag_input = azurerm_storage_container.graphrag_input.name
    graphrag_output = azurerm_storage_container.graphrag_output.name
  }
}

# PostgreSQL
output "postgres_server_name" {
  description = "PostgreSQL server name"
  value       = azurerm_postgresql_flexible_server.main.name
}

output "postgres_server_fqdn" {
  description = "PostgreSQL server FQDN"
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "postgres_database_name" {
  description = "PostgreSQL database name"
  value       = azurerm_postgresql_flexible_server_database.main.name
}

output "postgres_connection_string" {
  description = "PostgreSQL connection string"
  value       = "postgresql://${var.postgres_admin_username}@${azurerm_postgresql_flexible_server.main.name}:${var.postgres_admin_password}@${azurerm_postgresql_flexible_server.main.fqdn}:5432/${var.postgres_database_name}?sslmode=require"
  sensitive   = true
}

# Container Registry
output "acr_name" {
  description = "Container Registry name"
  value       = azurerm_container_registry.main.name
}

output "acr_login_server" {
  description = "Container Registry login server"
  value       = azurerm_container_registry.main.login_server
}

output "acr_admin_username" {
  description = "Container Registry admin username"
  value       = azurerm_container_registry.main.admin_username
  sensitive   = true
}

# Container Apps
output "container_app_environment_name" {
  description = "Container Apps environment name"
  value       = azurerm_container_app_environment.main.name
}

output "container_app_name" {
  description = "Container App name"
  value       = azurerm_container_app.main.name
}

output "container_app_url" {
  description = "Container App URL"
  value       = "https://${azurerm_container_app.main.ingress[0].fqdn}"
}

output "container_app_fqdn" {
  description = "Container App FQDN"
  value       = azurerm_container_app.main.latest_revision_fqdn
}

# Summary
output "deployment_summary" {
  description = "Deployment summary"
  value = {
    resource_group = azurerm_resource_group.main.name
    region         = azurerm_resource_group.main.location
    app_url        = "https://${azurerm_container_app.main.ingress[0].fqdn}"
    dashboard_url  = "https://${azurerm_container_app.main.ingress[0].fqdn}/dashboard"
  }
}
