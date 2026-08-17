# QA stack — no separate body dump needed

Captured 2026-08-16. Every namespace slated for deletion on the QA stack was
byte-identical to production's at capture time, so `production-mimir.json`,
`production-loki.json` and `production-alertmanager.json` are the restore source
for QA as well.

Verified field-by-field (`interval`, group `name`, and each rule's `alert`, `expr`,
`for`, `labels`):

- `grafanacloud-prom` / `eks` — 1 group, 6 rules, identical to production, including
  the inverted environment filters (`DeploymentUnavailableWarning` → `production`,
  `DeploymentUnavailableCritical` → `ci|qa`) and the capital-W
  `severity: "Warning"` on `DeploymentReplicasMissingWarning`.
- `grafanacloud-prom` / `linux-host` — 3 groups, 4 rules, identical, including the
  dead `CPUUsageWarning` with its unsubstituted `host="$instance"`.
- `grafanacloud-logs` / `5xx-errors`, `cert-manager`, `edxapp-logs`, `heroku-logs`,
  `vault` — 11 groups, 29 rules, identical.
- Legacy Cloud Alertmanager — same four receivers (`oblivion`,
  `slack-notifications-ocw-misc-warning`, `slack-notifications-ocw-misc-critical`,
  `rootly`) and the same route tree, `Deploy.*`/`Kube.*` silences included.

The consequence noted in the spec (§0.7a) holds: because each Grafana Cloud tenant
only holds its own clusters, QA's `.*-(ci|qa)` rules match real data — so QA's
inverted `DeploymentUnavailableCritical` fires at critical severity on QA
deployments — while QA's Loki rules, hardcoded to `cluster="applications-production"`
and `environment=~".*production"`, can never match and are evaluated every interval
for nothing.
