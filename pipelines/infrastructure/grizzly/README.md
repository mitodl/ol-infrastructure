# Grafana automated dashboard management

The idea here is to take a folder of dashboards from Grafana in the CI environment and sync that from CI->QA->Production. Right now, the pipeline is configured to sync just one folder: `Managed`.

## To apply this pipeline

```
fly -t pr sp -p grizzly-managed-folder -c grizzly.yaml -l managed_vars.yaml
```

## Manage another folder of dashboards

If you had another folder you wanted sync from CI->QA->Production, you will need to create a second instance of the pipeline. For example, in the CI environment you have a folder of dashboards called 'Core'.

First, make a vars file for that. You will need to find the UID which you can get from the URL in Grafana.

``` core_vars.yaml
---
grafana-folder-uid: oLE4pmT7z  # Whatever it actually is...
grafana-folder-name: core
```

Then apply the new pipeline.
```
fly -t pr -sp -p grizzly-core-folder -c grizzly.yaml -l core_vars.yaml
```

## Caveats

* These pipelines will only work in from the `infrastructure` team on the _Production_ Concourse environment. This is the only place that vault has the secrets required by this pipeline.
* The secrets in value contain an `admin` API token for each Grafana environment. By default these expire just 24 hours after creation, so if you need to recreate them for some reason, pay attention to the TTL.
* Right now, the process will only synchronize one DashboardFolder and the dashboards it contains. No other resources are copied from one environment to another.

## For best results

For the best results when using one dashbaord across all grafana environments you want to setup your datasource(s) in a specific way. For example, if your dashboard will use Prometheus (Cortex) metrics

* Dashboard Settings -> Variables -> Make a new variable
  * General - Name: `datasource_prom`  --- All lowercase
  * General - Type: Data Source
  * General - Hide: Variable
  * Data source options - Type: `Prometheus`
  * Data source options - Instance name filter: `grafanacloud-mitol.*`
  * Preview of Values - Should contain only one datasource.
* When you create panels, be sure to select `$datasource_prom` for the panel, rather than explicitly specifying the Prometheus datasource.

And of course, you can create two variables, one for the Prometheus datasource and one for the Loki datasource and use both within your dashboard.
