# Monitoring Overview

We primarily use [Vector](https://vector.dev/) for collecting both metrics and logs from our applications. Vector is pretty flexible and offers `sources` and `sinks` to/from many different systems.

## Metrics

Ultimately we utilize [Prometheus](https://prometheus.io/), but our stack is a little non-traditional.

For our applications in EC2, we use Vector as a middleman to perform the 'prometheus-scrape' action, rather than performing the scrape with an actual prometheus instance that we would have to run. Right at scrape time, on the node performing the scrape, we can perform `transforms` on the data to drop timeseries that we are not interested in or add labels + metadata that would be helpful. This is good, because we are charged by the timeseries in GrafanaCloud and many prometheus endpoints produce lots of not-interesting data. The more we can drop early, the better. Once it is in GrafanaCloud, we pay for it.

After the transforms, Vector ships the data straight to GrafanaCloud where it is stored in Cortex, which is actually *not* prometheus but it is API compatible and the distinction isn't important to us.

## Logs

For log collection and aggregation, we again are using Vector and GrafanaCloud. Vector is also very good at collecting and parsing logfiles and again we can utilize its ability to `transform` the data to drop not-interesting data and save a little money.

After any transforms on the log data are completed, Vector ships the data straight to GrafanaCloud again and it is stored in [Loki](https://grafana.com/oss/loki/). Loki is like a hybrid of ElasticSearch and Prometheus and it has its own [query language](https://grafana.com/docs/loki/latest/logql/).

## Vector Patterns

There are a couple different patterns at play in our environment.

### Applications deployed into EC2

> The statements below do not necessarily apply to EC2 instances managed with salt-stack

For applications deployed in EC2, we install and configure Vector at AMI build time. We pull in global secrets (values that are the same regardless of where the AMI is running) at build time. Other secrets are interpolated runtime secrets using environment variables populated via `/etc/default/` files and vault-template.

Examples:
- Config files laid down at AMI build time [here](https://github.com/mitodl/ol-infrastructure/blob/main/src/bilder/images/concourse/templates/vector)
- `/etc/default` secrets from runtime [here](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_infrastructure/applications/concourse/__main__.py)

### Applications deployed in Heroku

Specifics of collecting logs out of Heroku are covered [here](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_infrastructure/infrastructure/vector_log_proxy/README.md)

### Applications deployed into ECS

Coming soon!
