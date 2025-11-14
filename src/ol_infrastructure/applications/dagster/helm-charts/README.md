# Dagster Helm Charts

This directory contains vendored Dagster Helm charts with JSON schema files removed.

## Why Vendored Charts?

The official Dagster Helm charts (v1.12.1) contain `values.schema.json` files with external `$ref` URLs pointing to `https://kubernetesjsonschema.dev/v1.18.0/_definitions.json`. When deploying via Pulumi, the Helm SDK attempts to validate values against these schemas and fails with:

```
failing loading "https://kubernetesjsonschema.dev/v1.18.0/_definitions.json": invalid file url
```

This is a bug in the Helm Go SDK's schema validation that doesn't properly handle HTTPS URLs in `$ref` fields. Pulumi does not expose the `--skip-schema-validation` flag, and environment variables do not disable this validation.

**Solution:** We vendor the charts locally with schema files removed. The charts are functionally identical to the upstream versions, just without the problematic schema validation files.

## Directory Contents

- `dagster-{VERSION}-noschema.tgz` - Main Dagster chart (control plane)
- `dagster-user-deployments-{VERSION}-noschema.tgz` - User code deployments chart
- `../vendor_charts.sh` - Script to automatically pull and patch charts

## Updating Charts

When updating the Dagster chart version in `src/bridge/lib/versions.py`:

1. Run the vendor script:
   ```bash
   cd src/ol_infrastructure/applications/dagster
   ./vendor_charts.sh
   ```

2. Commit the new vendored charts:
   ```bash
   git add helm-charts/
   git commit -m "vendor: Update Dagster charts to vX.Y.Z"
   ```

3. Deploy with Pulumi as normal - it will automatically use the local charts.

## What the Vendor Script Does

1. Reads `DAGSTER_CHART_VERSION` from `versions.py`
2. Pulls `dagster` and `dagster-user-deployments` charts from `https://dagster-io.github.io/helm`
3. Recursively removes all `values.schema.json` files (including from subcharts)
4. Packages charts as `.tgz` files with `-noschema` suffix
5. Places them in this directory

## Verification

The vendored charts are byte-for-byte identical to upstream except for the removed schema files. You can verify by:

```bash
# Pull upstream chart
helm pull dagster-io/dagster --version 1.12.1 --untar

# Compare (schema files will differ)
diff -r dagster/ <extracted-noschema-chart>
```

## Chart Version

Current version: **1.12.1** (defined in `src/bridge/lib/versions.py`)

## Related Issues

- Pulumi Kubernetes Provider does not expose `--skip-schema-validation`: https://github.com/pulumi/pulumi-kubernetes/issues/2943
- Helm schema validation with external refs: https://github.com/helm/helm/issues/12436
