# Deployment plan: Stripe PayPal adapter on the applications EKS cluster

Status: plan, ready for review
Date: 2026-10-30
Issue: https://github.com/mitodl/hq/issues/14186
Vendor docs and the Fargate stack they ship: https://github.com/mitodl/paypal-adapter-setup (private)

## Scope

How the Stripe-written PayPal adapter (`stripe/orchestration-adapters`, version 2.4.2) is run
on our EKS infrastructure, how it gets its secrets, how it is reached, and how it is kept healthy
and current. It covers this repository only. The mitxonline-side Checkout changes and the
Stripe/PayPal account work are listed at the end as dependencies, with owners, but are not
designed here.

Adapter facts below are from `docs/01-deploy-and-manage-adapter.md` and `docs/00-paypal.md` in
the setup repo and from `orchestration-adapters-2.4.2/fargate-stack.yaml`. Repo facts are from
`main` at `9522c1a71`.

## What the adapter is, in one paragraph

A stateless HTTP service on port 8080 that sits in the middle of every PayPal payment. Stripe
calls it (`POST /paypal/payments`) to create, confirm, save and refund PayPal orders; PayPal
calls it (`POST /paypal/webhook`) with vault-token events; the customer's browser is redirected
through it (`GET /paypal/approved`, `GET /paypal/canceled`) on the way back from PayPal to
Stripe Checkout. It calls out to `api.stripe.com`, PayPal's API and
`adapter-observability.stripe.com`. Our applications never talk to it: mitxonline keeps talking
to Stripe and only names the custom payment method on the Checkout Session. Its whole
configuration is one `config.json` read at startup. If it is down, PayPal checkouts fail,
PayPal webhooks are lost, subscription renewals fail, and Stripe may switch PayPal off at
checkout until it is healthy again.

## Decisions

| # | Decision |
|---|---|
| P1 | Run on the `applications` EKS cluster, namespace `paypal-adapter`, next to mitxonline, which is the consumer. |
| P2 | New Pulumi project `applications/paypal_adapter/` (`ol-application-paypal-adapter`), stacks `QA` and `Production`. No `CI` stack to start: each stack needs its own custom payment method in a Stripe account we do not control, and CI has no checkout to exercise it. |
| P3 | Deploy the vendor image as is through `OLApplicationK8s` with the Django parts switched off (`import_nginx_config=False`, `init_migrations=False`, `init_collectstatic=False`), the edx_notes precedent. Fall back to a hand-written Deployment only if the spike in step 1 shows the component fights a non-Django container. |
| P4 | Pull `stripe/orchestration-adapters:2.4.2@sha256:89ef36f0…` by digest through the ECR Docker Hub pull-through cache (`registry="dockerhub"`). Tag and digest pinned in `bridge/lib/versions.py`. No auto-updater: upgrades are a reviewed PR, QA before Production. |
| P5 | Secrets live in SOPS under `src/bridge/secrets/paypal_adapter/`, are written by Pulumi to a per-app Vault KV mount `secret-paypal-adapter`, and reach the pod as a `config.json` rendered by a `VaultStaticSecret` template, mounted at `/run/secrets/config.json`. The `VaultStaticSecret` restart-targets the Deployment, because the adapter only reads config at startup. |
| P6 | Public hostnames `paypal-adapter-qa.ol.mit.edu` and `paypal-adapter.ol.mit.edu` on the existing APISIX gateway, cert-manager certificate with an `ApisixTls`, DNS by external-dns through `eks:apisix_domains`. The hostname is what Stripe's dashboard needs, so it goes to the Stripe-side owner before anything is deployed. |
| P7 | Four routes, nothing else: `/paypal/payments` restricted to Stripe's published webhook IPs with the `ip-restriction` plugin; `/paypal/webhook`, `/paypal/approved`, `/paypal/canceled` public. `/monitor/*` has no route, so the gateway answers 404; probes and the PodMonitor hit the pod directly. |
| P8 | Three to six replicas with pod anti-affinity, a PodDisruptionBudget of one unavailable, the component's CPU HPA, and `AlertTier.page` in Production (`notify` in QA). This matches the vendor stack's 3-10 tasks and its "always available" requirement. |
| P9 | Metrics from `/monitor/metrics` through a hand-written PodMonitor; logs through the cluster's existing pipeline; alerting through the existing tier-labelled deployment rules. No new dashboards until there is traffic to look at. |

## P3: why the shared component and not a raw Deployment

`OLApplicationK8s` already produces what the vendor's Fargate stack produces and what Stripe
asks for: several replicas spread across nodes, an HPA, a PDB, a preStop sleep so the gateway
drains a pod before it stops, labels that the alert rules and Rootly routing read, and a
config-hash annotation that restarts pods when configuration changes. edx_notes
(`applications/edx_notes/__main__.py:306-308`) already runs it without the Django init
containers and the nginx sidecar, and `probe_configs`, `application_cmd_array`,
`application_port`, `extra_volumes` and `extra_volume_mounts` cover everything the adapter
needs. Writing the same ten resources by hand for one app would be the second copy of this
pattern in the repo, and would miss the alert tier labelling.

Two things the component does not do for this image:

- Its PodMonitor is for the Granian metrics port. The adapter serves Prometheus metrics on the
  application port at `/monitor/metrics`, so the PodMonitor is written in the stack with
  `manage_pod_monitor=False` on the component.
- Its default probes are django-health-check paths. `probe_configs` supplies readiness and
  liveness on `GET /monitor/health` port 8080, which the vendor stack also uses
  (`fargate-stack.yaml:481`), with a startup probe so a slow pull or Vault fetch is not a
  crash loop.

`env_from_secret_names` is required by the model but may be empty; the adapter takes a file,
not environment variables. `application_config` carries `CONFIG_PATH=/run/secrets/config.json`
only.

## P4: image, registry and upgrades

The image is multi-arch (`linux/amd64`, `linux/arm64`, checked against the manifest list of the
pinned digest on 2026-10-30), so it runs on the cluster's m8i node groups. The vendor stack pins
ARM64 only because Fargate charges less for it.

The vendor stack ships a Lambda that polls Stripe's version API every five minutes and
redeploys ECS on a new release (`fargate-stack.yaml:867-1020`). It is optional there
(`AutoUpdateEnabled`) and we do not carry it over: an unreviewed image change in the payment
path is the wrong default here, and the terms only ask that updates land "in a timely manner".
Instead:

- `bridge/lib/versions.py` gets `PAYPAL_ADAPTER_VERSION = "2.4.2"` under a
  `# renovate: datasource=docker depName=orchestration-adapters packageName=stripe/orchestration-adapters`
  marker, so a new tag shows up as a Renovate PR, and `PAYPAL_ADAPTER_IMAGE_DIGEST` next to
  it. The digest is updated by hand from the adapter changelog or
  `docker buildx imagetools inspect`, and `cosign verify` is run against it as the docs show
  before it is committed. `vuln_scanner/__main__.py:185-195` records that the repo has no
  digest-pin automation yet; that gap applies here too and is not closed by this plan.
- The Concourse pipeline is `topology="preview-gated"` (step 7), so a bump deploys to QA on
  merge and to Production after a reviewed preview. Stripe's dashboard shows the running
  version under **Inspect in Workbench**, which is the post-deploy check.

The pull-through cache resolves `dockerhub/stripe/orchestration-adapters@sha256:…` to the same
manifest Docker Hub serves, so the digest pin holds through the cache.

## P5: secrets and the rendered config file

Four values are secret: the Stripe restricted key, the Stripe event-destination signing secret,
the PayPal client ID and the PayPal client secret. Two are not and come from stack config:
the public base URL and `livemode`. The SOPS files are

```
src/bridge/secrets/paypal_adapter/secrets.qa.yaml          # Stripe sandbox + PayPal sandbox
src/bridge/secrets/paypal_adapter/secrets.production.yaml  # live Stripe + live PayPal
```

with keys `stripe_secret_key`, `stripe_signing_key`, `paypal_client_id`, `paypal_client_secret`.
`.sops.yaml` already keys on the `qa.yaml` / `production.yaml` suffix, so no new creation rule.

The stack follows learn_ai (`applications/learn_ai/__main__.py:502-670`): a `vault.Mount` at
`secret-paypal-adapter`, a `vault.generic.Secret` at `secret-paypal-adapter/secrets`, a Vault
policy granting read on that mount, a Kubernetes auth backend role bound to the namespace's
service account, `OLVaultK8SResources` for the `VaultAuth`, and one `OLVaultK8SStaticSecret`
whose `templates` entry renders the config file:

```json
{
  "adapter": {"base_url": "https://paypal-adapter-qa.ol.mit.edu", "livemode": false},
  "stripe": {"secret_key": "{{ get .Secrets \"stripe_secret_key\" }}"},
  "logging": {"level": "debug", "json": true},
  "adapter_configs": {
    "paypal": {
      "client_id": "{{ get .Secrets \"paypal_client_id\" }}",
      "client_secret": "{{ get .Secrets \"paypal_client_secret\" }}",
      "stripe_signing_key": "{{ get .Secrets \"stripe_signing_key\" }}",
      "enable_webhooks": true
    }
  }
}
```

`base_url` and `livemode` are interpolated by Pulumi from stack config before the template is
handed to VSO. The resulting Secret is mounted read-only at `/run/secrets` and the
`VaultStaticSecret` lists the webapp Deployment in `restart_targets`, so rotating a key in SOPS
→ `pulumi up` → Vault → VSO → rolling restart, with no manual step. `refresh_after` stays at
the component default of one hour.

The vendor template sets `"json": false`. Ours sets `true` so Loki gets structured lines; if
the adapter's JSON turns out not to parse in the existing pipeline, flip it back. Level stays
`debug` as the vendor template has it, because Stripe asks for these logs when debugging and
the volume is a few lines per payment; revisit for Production once traffic is known.

The vendor stack's alternative, `AWS_CONFIG_SECRET_ARN` with Secrets Manager and an IAM role,
is not used. It would need IRSA and a second secret store for one file that Vault already
holds.

## P6 and P7: ingress

The applications gateway already fronts hostnames under `ol.mit.edu`
(`infrastructure/aws/eks/Pulumi.applications.QA.yaml`, `eks:allowed_dns_zones` and
`eks:apisix_domains`). Adding the two adapter hostnames to `eks:apisix_domains` makes
external-dns publish them on the gateway's NLB, which is a deploy of the EKS stack before the
app stack. The app stack follows tika (`applications/tika/__main__.py`) for the certificate
(`OLCertManagerCert`, `create_apisixtls_resource=True`), the upstream
(`OLApisixUpstream`, `backend_resolve_granularity="endpoint"` so a pod in its preStop sleep
leaves the pool) and the routes (`OLApisixRoute` with `OLApisixSharedPlugins`,
`cors_allow_origins=[]` because no browser page fetches these endpoints with XHR).

| Route | Caller | Plugins | Why |
|---|---|---|---|
| `/paypal/payments` | Stripe, POST | `ip-restriction` with Stripe's webhook IPs | Stripe says it calls from the addresses in `https://stripe.com/files/ips/ips_webhooks.json`. The adapter also verifies the Stripe signature, so this is defence in depth, not the only check. |
| `/paypal/webhook` | PayPal, POST | none | PayPal calls it; PayPal does not publish a stable source range. The adapter verifies PayPal's signature. |
| `/paypal/approved`, `/paypal/canceled` | the customer's browser, GET | none | The customer's browser lands here. Query strings must pass through; APISIX forwards them unchanged. |

`OLApisixRouteConfig` matches on host and path only; it has no `methods` field, so the routes
accept any method and the adapter answers 404 or 405 for the wrong one. Restricting methods at
the gateway is a small addition to the component (`match.methods` in the ApisixRoute v2 schema)
with a unit test, and is worth doing in step 5 but is not required for the adapter to work.

`ip-restriction` works here because the NLB preserves the client address
(`apisix_official.py:132`, `preserve_client_ip.enabled=true`) and the `real-ip` plugin only
believes `X-Forwarded-For` from Fastly, so a request from elsewhere cannot spoof a Stripe
address. The IP list is a constant in the stack with the source URL in a comment, and
comparing it against the published list is part of each adapter upgrade. If the
list goes stale, payments fail closed, so the first alert on `/paypal/payments` 403s (step 8)
is what catches it.

No route covers `/monitor/health` or `/monitor/metrics`. APISIX returns 404 for an unmatched
path on a known host, which is the "restrict public access" the docs ask for without a second
plugin.

Egress needs nothing: the applications cluster has no NetworkPolicy or egress proxy, and the
hosts the adapter needs (`api.stripe.com`, `api-m.sandbox.paypal.com` or `api.paypal.com`,
`adapter-observability.stripe.com`, all 443) are reachable through the NAT gateways. Recorded
here so a future egress policy knows to allow them.

## P8 and P9: sizing, availability and observability

Vendor defaults are 256 CPU units and 512 MiB per task, 3-10 tasks, scaling on CPU and memory.
Start at `resource_requests={"cpu": "250m", "memory": "512Mi"}`,
`resource_limits={"memory": "512Mi"}`, `application_min_replicas=3`,
`application_max_replicas=6`, `app_pdb_maximum_unavailable=1`, anti-affinity on (the
default). The component's CPU HPA stays on; its memory VPA stays on too, so the limit follows
what the process actually uses. Adjust after the first weeks of QA metrics.

Probes: readiness and liveness `GET /monitor/health` on 8080, readiness every 5 s with a
failure threshold of 3 so an unhealthy pod leaves the gateway in about 15 s; liveness every
10 s with a threshold of 6 so a slow PayPal or Stripe call does not restart a pod. A startup
probe with 30 attempts covers the first Vault fetch.

Observability: the PodMonitor scrapes port 8080 at `/monitor/metrics` every 30 s. The webapp
pods carry `AlertTier.page` in Production, so the existing Deployment-availability and
crash-loop rules page without a new rule. One new rule is worth adding once the metric names
are known: a 5xx or 403 rate on the `/paypal/payments` route from the gateway's metrics, since
that is the Stripe-facing failure and the one that stales the IP list would cause. The
synthetic-monitoring checks (`metric_rules/synthetic_monitoring.py`) can also probe
`GET https://paypal-adapter.ol.mit.edu/paypal/canceled` for a non-5xx answer to prove the
whole path from DNS to pod; that is optional and depends on what the adapter returns for a
bare GET, which the spike will show.

## Implementation steps

Each step is one reviewable change. Steps 1-7 can be merged and previewed before any Stripe
credential exists; step 8 is the first `pulumi up` that needs them.

1. **Spike** (no merge needed): in a scratch stack directory, render `OLApplicationK8s` with
   the adapter image, the three Django switches off, `application_port=8080`,
   `probe_configs`, and a dummy Secret volume, and run `pulumi preview` against `QA`. Confirm
   the Deployment has one container, no init containers, no nginx, the right probes and the
   volume mount. If the component cannot render that, P3 falls back to a raw Deployment,
   Service, HPA and PDB in the stack and the rest of the plan is unchanged.

2. **Registration**: `lib/pulumi_projects.py` (`PAYPAL_ADAPTER = "ol-application-paypal-adapter"`
   and its `applications.paypal_adapter` mapping), `lib/ol_types.py` (`Application.paypal_adapter`,
   `Services.paypal_adapter`), `bridge/lib/versions.py` (tag and digest constants with the
   Renovate marker), `src/ol_concourse/pipelines/versions_map.py` and `secrets_map.py`
   entries. `tests/ol_concourse/test_secrets_map.py` enforces the latter.

3. **EKS stack config**: add `paypal-adapter` to `eks:namespaces` and the two hostnames to
   `eks:apisix_domains` in `infrastructure/aws/eks/Pulumi.applications.QA.yaml` and
   `Pulumi.applications.Production.yaml`. Deploy the EKS stacks; `check_cluster_namespace` in
   the app stack fails until this is live.

4. **Secrets**: create `src/bridge/secrets/paypal_adapter/secrets.{qa,production}.yaml` with
   placeholder values and the four keys, encrypted with `sops`. Real values replace them in
   step 8. Placeholders let the stack and the VSO template be exercised end to end.

5. **The stack**: `applications/paypal_adapter/` with `Pulumi.yaml`, `Pulumi.QA.yaml`,
   `Pulumi.Production.yaml` (`paypal_adapter:k8s_domain`, `paypal_adapter:livemode`,
   `vault:address`, `vault_server:env_namespace`, `aws:region`), and `__main__.py` covering
   namespace check, labels, Vault mount/secret/policy/role, `OLVaultK8SResources`,
   `OLVaultK8SStaticSecret` with the template, `OLApplicationK8s`, the PodMonitor, the
   certificate, the upstream, the shared plugins and the four routes. Keep the stack config
   small: everything that does not differ between QA and Production is a constant in
   `__main__.py`.

6. **Vault policy**: `applications/paypal_adapter/paypal_adapter_policy.hcl` granting read on
   `secret-paypal-adapter` and `secret-paypal-adapter/*`, following
   `learn_ai/learn_ai_policy.hcl`. No dynamic secrets, so no lease paths.

7. **Pipeline**: a `SimplePulumiParams` entry `"paypal-adapter"` in
   `src/ol_concourse/pipelines/infrastructure/simple_pulumi/pipeline.py` with
   `category="applications"`, `topology="preview-gated"`, `stages=["QA", "Production"]`,
   `auto_deploy_stages=["QA"]`, and the matching name in `simple_pulumi/meta.py`. QA deploys on
   merge; Production waits for a reviewed preview, which is the right gate for a payment-path
   service.

8. **First deploy, QA**: once the Stripe-side owner has created the sandbox custom payment
   method against `https://paypal-adapter-qa.ol.mit.edu/paypal/payments`, the restricted key
   and the signing secret, and accounting has supplied PayPal sandbox credentials, replace the
   placeholders in `secrets.qa.yaml`, merge, and let the pipeline deploy. Then run the
   verification below. Production repeats this with live credentials after mitxonline RC has
   completed real sandbox payments through it.

## Verification

After each deploy:

- `kubectl -n paypal-adapter get pods` shows three Ready pods on three different nodes.
- `kubectl -n paypal-adapter port-forward deploy/<webapp> 8080` then
  `curl localhost:8080/monitor/health` returns 200 and `curl localhost:8080/monitor/metrics`
  returns Prometheus text.
- From outside: `curl -i https://<host>/monitor/health` returns 404 from the gateway;
  `curl -i -X POST https://<host>/paypal/payments` from a non-Stripe address returns 403;
  `curl -i https://<host>/paypal/canceled` returns whatever the adapter returns for a bare GET,
  but not a 5xx and not a gateway error.
- Stripe Dashboard → Custom Payment Methods → **Inspect in Workbench** shows the adapter
  version `2.4.2` and a healthy status.
- Edit a placeholder in the SOPS file, deploy, and watch the pods roll: proves the VSO
  restart-target path before a real rotation needs it.
- A test checkout from mitxonline RC with a PayPal sandbox buyer completes, and the PayPal
  developer dashboard shows a `VAULT.PAYMENT-TOKEN.CREATED` event delivered to
  `/paypal/webhook` with HTTP 202 (docs `02-accept-a-payment.md`, "Verify that PayPal saves
  payment methods").

Before submitting: `uv run ruff format`, `uv run ruff check`, `uv run mypy src/`,
`pulumi preview` from the project directory, `uv run pytest tests/ol_concourse/` for the
secrets and versions maps, `uv run prek run --all-files`.

## Dependencies outside this repository

| What | Owner | Blocks |
|---|---|---|
| Hostnames `paypal-adapter-qa.ol.mit.edu` and `paypal-adapter.ol.mit.edu`, sent to the Stripe-side contact (Olympia in VPF, via Bryan Adkison per the issue) | us, now | Stripe-side setup |
| Custom payment method per Stripe account (sandbox, live) pointing at `https://<host>/paypal/payments`; restricted key with Write on Payment Records, Payment Methods, Setup Intents; event-destination signing secret | Stripe-side contact | step 8 |
| PayPal business account sandbox and live client ID and secret | accounting | step 8 |
| PayPal account settings: "save payment methods" eligibility completed before go-live (subscription renewals fail silently otherwise), and currency conversion set to "Accept all payments without converting" | accounting with PayPal | Production go-live |
| mitxonline: Stripe API version `2025-03-31.basil` with the `checkout_merchant_instructed_orchestration_preview=v1` beta header, and `custom_payment_method_types[0]=<cpmt id>` on Checkout Session creation, presumably in `mitol-django`'s payment gateway | mitxonline team | end-to-end test |

## Risks

- **It is in the payment path.** Downtime means failed PayPal checkouts and renewals and Stripe
  may disable PayPal at checkout. Three replicas, anti-affinity, a PDB and `AlertTier.page` are
  the mitigation; the Production pipeline being preview-gated keeps a bad bump from reaching it
  unreviewed.
- **The IP allowlist fails closed.** A stale list blocks Stripe. The 403-rate alert and the
  check at each upgrade are the mitigation. If that turns out too fragile, drop the plugin and
  rely on the adapter's signature check; the docs call the IP restriction a recommendation.
- **Private preview.** Stripe can change the adapter contract between minor versions and ends
  the preview at its discretion. The docs are mirrored in the setup repo for that reason, and
  the terms require timely updates, so the Renovate marker on the tag is not optional.
- **Config is read once.** Any secret change without a restart is silently ignored. The
  `restart_targets` on the `VaultStaticSecret` and the component's config-hash annotation
  cover the two paths a change can arrive by.
- **No CI stack.** Nothing exercises the stack code on every merge except `pulumi preview` in
  the QA job. Acceptable for a deployment with no application code of our own; revisit if the
  stack grows logic.

## Open questions

1. Hostnames: `paypal-adapter[-qa].ol.mit.edu` follows tika's `<app>-qa.ol.mit.edu` and the
   `api-pay.ol.mit.edu` precedent. The URL can appear in a browser's network log during the
   PayPal redirect, not in the address bar. Fine, or something less descriptive?
2. Keep `ip-restriction` on `/paypal/payments` (fails closed, defence in depth) or rely on
   the adapter's signature check alone (fails open to a signature error, never blocks Stripe)?
   The plan says keep it.
3. QA log level `debug` follows the vendor template. Production the same, or `info`?
