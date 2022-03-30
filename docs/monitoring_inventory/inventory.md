# Tools

- Sentry
  - Purpose: Detailed application monitoring and error logging.
- Healthchecks.io
  - Purpose: Absence of alerting detection. For determining when other alerting tools may have failed or become unresponsive.
- GrafanaCloud
  - Purpose: Collecting and storing metric and log data from applications and infrastructure.
  - Subcomponents:
    - Grafana: Visualization and alerting on metric and log data.
    - Cortex: Backend for storing metric data.
    - Loki: Backend for storing log data
- Pingdom
  - Purpose: External synthetic and HTTP monitoring.

# Usage Matrix

| ODL App / Component / Process | Sentry | HealthChecks.io | Grafana Metrics | Grafana Logs | Pingdom |
| ------------------------------| ------ | --------------- | --------------- | ------------ | ------- |
| OCW Studio (webapp)           | Yes    | Yes             | Yes             | Yes          | No      |
| OCW Site (static content)     | N/A    | N/A             | No              | No           | Yes     |
| OCW Site Backup (process)     | N/A    | Yes             | N/A             | No           | N/A     |
| MITx (webapp)                 | Yes    | No              | No              | Yes          | Yes     |
| MITx (openEdx)                | Yes    | No              | No              | Yes          | Yes     |
| MITx (infrastructure)         | N/A    | N/A             | Yes             | Yes          | N/A     |
| xPro (webapp)                 | Yes    | No              | No              | Yes          | Yes     |
| xPro (openEdx)                | Yes    | No              | No              | Yes          | Yes     |
| xPro (infrastructure)         | N/A    | N/A             | Yes             | Yes          | N/A     |
| Residential (openEdx)         | Yes    | No              | No              | Yes          | Yes     |
| Residential (infrastructure)  | N/A    | N/A             | Yes             | Yes          | Yes     |
| MIT Open (webapp)             | Yes    | No              | No              | Yes          | No      |
| MIT Open Discussions (webapp) | Yes    | No              | No              | Yes          | Yes     |
| MIT Open Reddit (webapp)      | Yes    | No              | No              | Yes          | No      |
| odl-video (webapp)            | Yes    | No              | No              | Yes          | Yes     |
| Bootcamps (webapp)            | Yes    | No              | No              | Yes          | Yes     |
| MicroMasters (webapp)         | Yes    | No              | No              | Yes          | No      |
