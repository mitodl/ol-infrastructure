#!/bin/bash
# Fetch the dagster-user-deployments Helm chart and remove the problematic
# values.schema.json file that references external URLs which are blocked
# in the Concourse CI environment.
#
# This chart must be committed to the repository so it's available during
# Pulumi deployments in CI/CD pipelines.

set -euo pipefail

CHART_VERSION="${1:-1.12.1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHART_DIR="$REPO_ROOT/charts/dagster-user-deployments"

echo "Fetching dagster-user-deployments chart version $CHART_VERSION..."

# Remove existing chart
rm -rf "$CHART_DIR"

# Create parent directory
mkdir -p "$(dirname "$CHART_DIR")"

# Fetch the chart
cd "$(dirname "$CHART_DIR")"
helm pull dagster-io/dagster-user-deployments --version "$CHART_VERSION" --untar

# Rename if needed (helm pull creates directory with chart name)
if [ -d "dagster-user-deployments" ] && [ ! -d "$CHART_DIR" ]; then
    mv dagster-user-deployments "$CHART_DIR"
fi

# Remove the problematic schema file that tries to fetch external URLs
# These URLs (https://kubernetesjsonschema.dev) return 404 in CI environment
if [ -f "$CHART_DIR/values.schema.json" ]; then
    echo "Removing values.schema.json to avoid external URL fetching..."
    rm -f "$CHART_DIR/values.schema.json"
fi

echo "Chart fetched successfully to: $CHART_DIR"
echo "Note: values.schema.json removed to prevent schema validation errors in CI"
echo ""
echo "To update the chart version, edit src/bridge/lib/versions.py and run:"
echo "  $0 <new_version>"
