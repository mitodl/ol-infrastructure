#!/usr/bin/env bash
set -euo pipefail

# Script to vendor Dagster Helm charts with JSON schema files removed
# This works around a Pulumi/Helm SDK bug where schema validation fails on external $ref URLs

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHARTS_DIR="${SCRIPT_DIR}/helm-charts"
VERSIONS_FILE="${SCRIPT_DIR}/../../../bridge/lib/versions.py"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Extract Dagster chart version from versions.py
get_chart_version() {
    if [[ ! -f "${VERSIONS_FILE}" ]]; then
        log_error "Cannot find versions.py at ${VERSIONS_FILE}"
        exit 1
    fi

    version=$(grep 'DAGSTER_CHART_VERSION' "${VERSIONS_FILE}" | cut -d'"' -f2)
    if [[ -z "${version}" ]]; then
        log_error "Could not extract DAGSTER_CHART_VERSION from ${VERSIONS_FILE}"
        exit 1
    fi

    echo "${version}"
}

# Vendor a single chart
vendor_chart() {
    local chart_name=$1
    local version=$2
    local temp_dir

    temp_dir=$(mktemp -d)
    # shellcheck disable=SC2064
    trap "rm -rf ${temp_dir}" EXIT

    log_info "Pulling ${chart_name}:${version}..."

    if ! helm pull "dagster-io/${chart_name}" \
        --version "${version}" \
        --untar \
        --untardir "${temp_dir}"; then
        log_error "Failed to pull chart ${chart_name}:${version}"
        return 1
    fi

    log_info "Removing JSON schema files from ${chart_name}..."

    # Remove all values.schema.json files (including from subcharts)
    find "${temp_dir}/${chart_name}" -name "values.schema.json" -delete

    local schema_count
    schema_count=$(find "${temp_dir}/${chart_name}" -name "values.schema.json" | wc -l)

    if [[ ${schema_count} -gt 0 ]]; then
        log_warn "Warning: ${schema_count} schema files still remain"
    else
        log_info "All schema files removed successfully"
    fi

    log_info "Packaging ${chart_name}-${version}-noschema.tgz..."

    local output_file="${CHARTS_DIR}/${chart_name}-${version}-noschema.tgz"

    if ! tar czf "${output_file}" -C "${temp_dir}" "${chart_name}"; then
        log_error "Failed to package chart"
        return 1
    fi

    local size
    size=$(du -h "${output_file}" | cut -f1)
    log_info "Created ${output_file} (${size})"

    rm -rf "${temp_dir}"
    trap - EXIT
}

# Main function
main() {
    log_info "Dagster Helm Chart Vendoring Script"
    echo

    # Create charts directory if it doesn't exist
    mkdir -p "${CHARTS_DIR}"

    # Get the chart version from versions.py
    CHART_VERSION=$(get_chart_version)
    log_info "Using Dagster chart version: ${CHART_VERSION}"
    echo

    # Add Dagster Helm repo if not already added
    if ! helm repo list | grep -q "dagster-io"; then
        log_info "Adding dagster-io Helm repository..."
        helm repo add dagster-io https://dagster-io.github.io/helm
    fi

    log_info "Updating Helm repositories..."
    helm repo update dagster-io
    echo

    # Vendor both charts
    vendor_chart "dagster" "${CHART_VERSION}"
    echo
    vendor_chart "dagster-user-deployments" "${CHART_VERSION}"
    echo

    log_info "Chart vendoring complete!"
    echo
    log_info "Vendored charts:"
    ls -lh "${CHARTS_DIR}"/*.tgz 2>/dev/null || log_warn "No charts found in ${CHARTS_DIR}"
    echo
    log_info "Next steps:"
    echo "  1. Commit the vendored charts to the repository"
    echo "  2. Deploy using Pulumi (it will automatically use the local charts)"
}

# Run main function
main "$@"
