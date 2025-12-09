
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

#### Fastly HTTPS Log Streaming Requirements

This service implements Fastly's HTTPS log streaming specification (RFC 8615):

1. **HTTPS Endpoint**: TLS-secured endpoint with valid certificate (via Gateway API + Let's Encrypt)
2. **Basic Authentication**: Username/password authentication configured in Vector
3. **POST Method**: Accepts HTTP POST requests from Fastly's log streaming service
4. **Content-Type**: Accepts `application/json` payloads
5. **Domain Verification**: RFC 8615 challenge endpoint at `/.well-known/fastly/logging/challenge`

Reference: https://www.fastly.com/documentation/guides/integrations/logging-endpoints/log-streaming-https/

#### Domain Ownership Verification

Fastly requires proof of domain ownership via RFC 8615 challenge-response:
- Challenge path: `/.well-known/fastly/logging/challenge`
- Response: SHA-256 hex digest of each Fastly Service ID (one per line)
- Implementation: Python sidecar container queries Fastly API dynamically

The challenge server automatically:
1. Queries the Fastly API using the admin API key from Vault
2. Retrieves all service IDs from the Fastly account
3. Computes SHA-256 hashes for each service ID
4. Returns hashes as newline-delimited text (plus `*` wildcard)

No manual configuration needed - new Fastly services are automatically discovered.
