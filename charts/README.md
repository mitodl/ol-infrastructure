# Local Helm Charts

This directory contains Helm charts that are vendored locally to work around issues with remote chart repositories.

## dagster-user-deployments

**Why local?** The official Dagster Helm chart includes a `values.schema.json` file that references external JSON schemas hosted at `https://kubernetesjsonschema.dev`. These URLs return 404 errors when accessed from the Concourse CI environment due to network restrictions or firewall rules.

**How it works:**
1. The chart is fetched using `bin/fetch-dagster-user-deployments-chart.sh`
2. The script removes the `values.schema.json` file that causes the issue
3. Pulumi references the local chart path instead of the remote repository

**Updating the chart:**
```bash
# Update the version in src/bridge/lib/versions.py (DAGSTER_CHART_VERSION)
# Then run:
./bin/fetch-dagster-user-deployments-chart.sh <version>
git add charts/dagster-user-deployments
git commit -m "Update dagster-user-deployments chart to <version>"
```

**Current version:** See `charts/dagster-user-deployments/Chart.yaml` for the version in use.

## Why not use Pulumi's skip_schema_validation?

As of Pulumi Kubernetes provider v4.24.0, there is no parameter to skip Helm's client-side schema validation (only `disable_openapi_validation` which skips Kubernetes API validation). The schema validation happens in the Pulumi provider binary before deployment.

## Error message we're avoiding

```
error: values don't meet the specifications of the schema(s) in the following chart(s):
dagster-user-deployments:
  failing loading "https://kubernetesjsonschema.dev/v1.18.0/_definitions.json": invalid file url
```

This error occurs because:
1. Pulumi's embedded Helm library validates values against the chart's schema
2. The schema references external URLs via `$ref`
3. These URLs are inaccessible from the CI environment
4. The JSON schema validator treats the 404 as an "invalid file url"
