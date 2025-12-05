
## vector-log-proxy

### Introduction

This service sets up a HA vector server running in Kubernetes that can be used to proxy log feeds from Heroku and Fastly to Grafana Cloud.

```
Heroku -> Traefik Gateway -> vector-log-proxy -> Grafana Cloud (Loki)
Fastly -> Traefik Gateway -> vector-log-proxy -> Grafana Cloud (Loki)
```

URLs for the services are as follows:

| Env | URL |
| --- | --- |
| CI  | log-proxy-ci.odl.mit.edu |
| QA/RC | log-proxy-qa.odl.mit.edu |
| Production | log-proxy.odl.mit.edu |

This service uses path-based routing through a shared Traefik gateway:
- Heroku logs: `https://<domain>/heroku` (standard HTTPS port 443)
- Fastly logs: `https://<domain>/fastly` (standard HTTPS port 443)

### Alternatives

In theory, Development could have implemented one of the open source logging providers which would have enabled sending logs directly from the application to Loki. This was an unknown effort and this proxy service roughly recreates what we had implemented via `fluentd` in the old salt-stack code.

### Useful Links:

- [Vector Sink Configuration](https://vector.dev/docs/reference/configuration/sources/heroku_logs/)
- [Heroku Documentation](https://devcenter.heroku.com/articles/logplex)

### Create a log-drain in Heroku

The command looks something like this:

```bash
heroku drains:add -a '<Name of the heroku application>' "https://<HEROKU_PROXY_USERNAME>:<HEROKU_PROXY_PASSWORD>@<Public URL for the vector log proxy service of the environment in question>/heroku?app_name=<Name of the heroku application again>&environment=<environment level>&service=<service name>"
```

**Example:**
```bash
heroku drains:add -a 'micromasters-qa' "https://username:password@log-proxy-qa.odl.mit.edu/heroku?app_name=micromasters-qa&environment=qa&service=micromasters"  # pragma: allowlist secret
```

This needs to be done for every app and every *instance* of that app. So `micromasters`, `micromasters-rc`, `micromasters-ci`... And so on. The parameters on the URL directly map to *labels* in Grafana Cloud which are used to find the appropriate logs. They are important so don't omit them.

If you mess one up, you can remove it easily enough with the same command but `drains:remove`.

### Fastly Logging

Fastly logging is configured automatically via Pulumi in each application's infrastructure code. The logs are sent to `https://<domain>/fastly` with basic authentication.

#### sha256sum

To add a new service to the fastly -> vector -> grafana pipeline, you will need to get the service ID hash and add it to the appropriate environments.

```bash
echo -n "<service id from fastly>" | sha256sum
```

The `-n` is important, otherwise it will add a newline to your echo'd string and the hash will not be correct. You can get `sha256sum` on macOS from the `brew` package `coreutils`.
