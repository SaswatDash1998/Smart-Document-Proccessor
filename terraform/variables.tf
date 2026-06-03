variable "subscription_id" {
  description = "Azure Subscription ID"
  type        = string
}

variable "resource_group_location" {
  description = "Azure region for Resource Group and Container Registry"
  type        = string
  default     = "westeurope"
}

variable "resources_location" {
  description = "Azure region for PostgreSQL, Storage, and Container Apps"
  type        = string
  default     = "northeurope"
}

variable "resource_group_name" {
  description = "Resource group name"
  type        = string
  default     = "rg-docint"
}

# Storage Account
variable "storage_account_name" {
  description = "Storage account name"
  type        = string
  default     = "stadocint"
}

# PostgreSQL
variable "postgres_server_name" {
  description = "PostgreSQL server name"
  type        = string
  default     = "pgdocint"
}

variable "postgres_admin_username" {
  description = "PostgreSQL admin username"
  type        = string
  default     = "pgadmin"
}

variable "postgres_admin_password" {
  description = "PostgreSQL admin password"
  type        = string
  sensitive   = true
}

variable "postgres_database_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "docint"
}

# Container Registry
variable "acr_name" {
  description = "Azure Container Registry name"
  type        = string
  default     = "acrdocintdsasw"
}

# Container Apps
variable "container_app_env_name" {
  description = "Container Apps environment name"
  type        = string
  default     = "cae-docint"
}

variable "container_app_name" {
  description = "Container App name"
  type        = string
  default     = "ca-docint"
}

# Key Vault
variable "keyvault_name" {
  description = "Key Vault name"
  type        = string
  default     = "kv-docint"
}

# Secrets
variable "cerebras_api_key" {
  description = "Cerebras API key"
  type        = string
  sensitive   = true
}

variable "gemini_api_key" {
  description = "Gemini API key"
  type        = string
  sensitive   = true
}

variable "azure_storage_connection_string" {
  description = "Azure Storage connection string"
  type        = string
  sensitive   = true
}
