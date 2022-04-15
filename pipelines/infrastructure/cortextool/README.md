# Automated Alert Configuration

This pipeline is created the keep the contents of [this repo](https://github.com/mitodl/grafana-alerts) sync'd out to the various MITODL grafana environments.

## How it Works

There is only one set of configs, those configs are applied the same to all environments. So if you desire different behaviors between RC and Production, be sure to write that into the expressions that create.

## To apply this pipeline

```
fly -t pr sp -p cortextool-sync-managed-alert-configs -c cortextool.yaml
```

## Manage another folder of dashboards

If you had another folder you wanted sync from CI->QA->Production, you will need to create a second instance of the pipeline. For example, in the CI environment you have a folder of dashboards called 'Core'.

First, make a vars file for that. You will need to find the UID which you can get from the URL in Grafana.

# Additional Secrets for alertmanager.yaml

If you have another ops-genie API key that you want to add to receivers, or email addresses, or whatever might be sensitive, you will need to add it to the secret-concourse/cortextool/ key in grafana and then add additional sed statements to the `sync-alertmanager-config-to-*` jobs. Try to follow the same `%% <WHATEVER> %%` format of the existing config items so it is clear those values are going to be interpolated at deploy time.
fly -t pr -sp -p grizzly-core-folder -c grizzly.yaml -l core_vars.yaml

## Caveats

* These pipelines __WILL__ overwrite any and all of the items that created by hand in the UI. If you make an alert rule by hand in Grafana, it will get deleted the next time the appropriate job runs. So, test with the UI if you want, but make sure to codify it when you're ready.
* These pipelines will only work from the `infrastructure` team on the _Production_ Concourse environment. This is the only place that vault has the secrets required by this pipeline definition.
* The secrets in vault contain an `admin` API token for each Grafana environment.
* There is a linter step that runs against cortext-rules and loki-rules and it is picky about spacing in yaml files. The format it desires isn't consistent with other things we do in MITODL but I think we are okay to deviate a bit to take advantage of the issues it will identify and fix automatically for us in regards to expression and rule formatting.
