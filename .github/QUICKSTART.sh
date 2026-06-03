#!/bin/bash

# ============================================
# GitHub Actions Setup - Quick Start Script
# ============================================
# This script helps you collect all the values
# needed for GitHub Secrets configuration
# ============================================

set -e

echo "================================"
echo "🚀 GitHub Actions Setup Helper"
echo "================================"
echo ""
echo "This script will help you collect all the values"
echo "needed to configure GitHub Secrets."
echo ""
echo "⚠️  IMPORTANT: Keep this output secure!"
echo "    Don't share or commit these values."
echo ""
read -p "Press Enter to continue..."
echo ""

# ============================================
# Check Azure CLI is installed
# ============================================
if ! command -v az &> /dev/null; then
    echo "❌ Azure CLI is not installed."
    echo "   Install from: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli"
    exit 1
fi

echo "✅ Azure CLI found"
echo ""

# ============================================
# Check logged in to Azure
# ============================================
if ! az account show &> /dev/null; then
    echo "❌ Not logged in to Azure."
    echo "   Run: az login"
    exit 1
fi

echo "✅ Logged in to Azure"
echo ""

# ============================================
# Get Subscription ID
# ============================================
echo "📋 Step 1: Getting Subscription ID..."
SUBSCRIPTION_ID=$(az account show --query id --output tsv)
echo "   Subscription ID: $SUBSCRIPTION_ID"
echo ""

# ============================================
# Create Service Principal
# ============================================
echo "📋 Step 2: Creating Service Principal..."
echo "   This will create 'github-actions-docint' with contributor access"
read -p "   Continue? (y/n): " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo ""
    echo "   Creating service principal..."
    
    SP_OUTPUT=$(az ad sp create-for-rbac \
        --name "github-actions-docint" \
        --role contributor \
        --scopes /subscriptions/$SUBSCRIPTION_ID/resourceGroups/rg-docint \
        --sdk-auth 2>&1)
    
    if [ $? -eq 0 ]; then
        echo "   ✅ Service Principal created successfully!"
    else
        echo "   ⚠️  Service Principal might already exist, or there was an error."
        echo "   Error: $SP_OUTPUT"
    fi
    echo ""
else
    echo "   Skipped service principal creation."
    echo "   ⚠️  You'll need to create it manually or use existing one."
    SP_OUTPUT="<MANUALLY_CREATE_THIS>"
    echo ""
fi

# ============================================
# Get Terraform Outputs
# ============================================
echo "📋 Step 3: Getting Terraform outputs..."

if [ ! -f "terraform/terraform.tfstate" ]; then
    echo "   ⚠️  Terraform state not found."
    echo "   Make sure you've run 'terraform apply' first."
    echo "   Or navigate to the terraform directory."
    DB_URL="<RUN_TERRAFORM_FIRST>"
    STORAGE_CONN="<RUN_TERRAFORM_FIRST>"
else
    cd terraform
    
    echo "   Getting database connection string..."
    DB_URL=$(terraform output -raw postgres_connection_string 2>&1 || echo "<ERROR_GETTING_OUTPUT>")
    
    echo "   Getting storage connection string..."
    STORAGE_CONN=$(terraform output -raw storage_account_primary_connection_string 2>&1 || echo "<ERROR_GETTING_OUTPUT>")
    
    cd ..
    echo "   ✅ Terraform outputs collected"
fi
echo ""

# ============================================
# API Keys Reminder
# ============================================
echo "📋 Step 4: API Keys"
echo "   You'll need to get these from your service dashboards:"
echo "   - Cerebras API Key: https://cloud.cerebras.ai/"
echo "   - Google API Key: https://console.cloud.google.com/"
echo ""

# ============================================
# Generate Summary
# ============================================
echo "================================"
echo "📝 GITHUB SECRETS CONFIGURATION"
echo "================================"
echo ""
echo "Add these as GitHub Secrets (Settings → Secrets → Actions):"
echo ""
echo "-------------------------------------------"
echo "Secret 1: AZURE_CREDENTIALS"
echo "-------------------------------------------"
echo "$SP_OUTPUT"
echo ""
echo "-------------------------------------------"
echo "Secret 2: AZURE_SUBSCRIPTION_ID"
echo "-------------------------------------------"
echo "$SUBSCRIPTION_ID"
echo ""
echo "-------------------------------------------"
echo "Secret 3: DATABASE_URL"
echo "-------------------------------------------"
echo "$DB_URL"
echo ""
echo "-------------------------------------------"
echo "Secret 4: AZURE_STORAGE_CONNECTION_STRING"
echo "-------------------------------------------"
echo "$STORAGE_CONN"
echo ""
echo "-------------------------------------------"
echo "Secret 5: CEREBRAS_API_KEY"
echo "-------------------------------------------"
echo "<GET_FROM_CEREBRAS_DASHBOARD>"
echo ""
echo "-------------------------------------------"
echo "Secret 6: GOOGLE_API_KEY"
echo "-------------------------------------------"
echo "<GET_FROM_GOOGLE_CLOUD_CONSOLE>"
echo ""
echo "================================"
echo ""

# ============================================
# Save to file
# ============================================
read -p "💾 Save this output to a file? (y/n): " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    OUTPUT_FILE="github_secrets_$(date +%Y%m%d_%H%M%S).txt"
    
    {
        echo "GitHub Secrets Configuration"
        echo "Generated: $(date)"
        echo "================================"
        echo ""
        echo "AZURE_CREDENTIALS:"
        echo "$SP_OUTPUT"
        echo ""
        echo "AZURE_SUBSCRIPTION_ID:"
        echo "$SUBSCRIPTION_ID"
        echo ""
        echo "DATABASE_URL:"
        echo "$DB_URL"
        echo ""
        echo "AZURE_STORAGE_CONNECTION_STRING:"
        echo "$STORAGE_CONN"
        echo ""
        echo "CEREBRAS_API_KEY:"
        echo "<GET_FROM_CEREBRAS_DASHBOARD>"
        echo ""
        echo "GOOGLE_API_KEY:"
        echo "<GET_FROM_GOOGLE_CLOUD_CONSOLE>"
    } > "$OUTPUT_FILE"
    
    echo "✅ Saved to: $OUTPUT_FILE"
    echo "⚠️  SECURE THIS FILE - It contains sensitive credentials!"
    echo "⚠️  Add to .gitignore: echo '*.txt' >> .gitignore"
else
    echo "Not saved. Copy the values above manually."
fi

echo ""
echo "================================"
echo "🎯 NEXT STEPS"
echo "================================"
echo "1. Go to GitHub repo → Settings → Secrets → Actions"
echo "2. Add all 6 secrets listed above"
echo "3. Go to Actions tab → Deploy to Azure Container Apps"
echo "4. Click 'Run workflow' to test"
echo ""
echo "Need help? Check .github/SETUP_GUIDE.md"
echo ""
echo "🎉 Setup complete! Happy deploying!"
echo "================================"
