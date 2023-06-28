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

| ODL App / Component / Process | Sentry | HealthChecks.io | Grafana Metrics | Grafana Logs |
| ------------------------------| ------ | --------------- | --------------- | ------------ |
| OCW Studio (webapp)           | Yes    | Yes             | Yes             | Yes          |
| OCW Site (static content)     | N/A    | N/A             | No              | No           |
| OCW Site Backup (process)     | N/A    | Yes             | N/A             | No           |
| MITx (webapp)                 | Yes    | No              | No              | Yes          |
| MITx (openEdx)                | Yes    | No              | No              | Yes          |
| MITx (infrastructure)         | N/A    | N/A             | Yes             | Yes          |
| xPro (webapp)                 | Yes    | No              | No              | Yes          |
| xPro (openEdx)                | Yes    | No              | No              | Yes          |
| xPro (infrastructure)         | N/A    | N/A             | Yes             | Yes          |
| Residential (openEdx)         | Yes    | No              | No              | Yes          |
| Residential (infrastructure)  | N/A    | N/A             | Yes             | Yes          |
| MIT Open (webapp)             | Yes    | No              | No              | Yes          |
| MIT Open Discussions (webapp) | Yes    | No              | No              | Yes          |
| MIT Open Reddit (webapp)      | Yes    | No              | No              | Yes          |
| odl-video (webapp)            | Yes    | No              | No              | Yes          |
| Bootcamps (webapp)            | Yes    | No              | No              | Yes          |
| MicroMasters (webapp)         | Yes    | No              | No              | Yes          |

# Synthetic Monitoring

| ODL App / Component / Process | URL | Pingdom | Grafana |
| ------------------------------| ----| ------- | --------|
| Bootcamp production | bootcamp.odl.mit.edu | yes |  no |
| MITx CAS | cas.mitx.mit.edu | yes |  no |
| MITx Online Production Application | nitxonline.mit.edu | yes |  no |
| MITx Online Production edX | courses.mitxonline.mit.edu | yes |  no |
| MITx Online QA edX Application | courses-qa.mitxonline.mit.edu | yes |  no |
| MITx Online RC Application | rc.mitxonline.mit.edu | yes |  no |
| MITx QA CMS | studio-mitx-qa.mitx.mit.edu | yes |  no |
| MITx QA LMS | mitx-qa.mitx.mit.edu | yes |  no |
| MITx current QA preview | preview-mitx-qa.mitx.mit.edu | yes |  no |
| MITx production CMS | studio.mitx.mit.edu | yes |  no |
| MITx production CMS draft | studio-staging.mitx.mit.edu | yes |  no |
| MITx production LMS | lms.mitx.mit.edu | yes |  no |
| MITx production LMS draft | staging.mitx.mit.edu | yes |  no |
| MITx production preview | preview.mitx.mit.edu | yes |  no |
| MITx production preview draft | preview.mitx.mit.edu | yes |  no |
| Micromasters CI | micromasters-ci.odl.mit.edu | yes |  no |
| Micromasters RC | micromasters-rc.odl.mit.edu | yes |  no |
| Micromasters production | micromasters.mit.edu | yes |  no |
| OCW Production (Fastly) | ocw.mit.edu | yes |  no |
| OCW production CMS 1 | ocwcms.mit.edu | yes |  no |
| OCW production CMS 2 | ocw-production-cms-2.odl.mit.edu | yes |  no |
| OCW production origin server | ocw-origin.odl.mit.edu | yes |  no |
| ODL Video RC | video-rc.odl.mit.edu | yes |  no |
| ODL Video production | video.odl.mit.edu | yes |  no |
| Open Discussions production | open.mit.edu | yes |  no |
| xPro CMS RC | studio-rc.xpro.mit.edu/heartbeat | yes |  no |
| xPro CMS production | studio.xpro.mit.edu/heartbeat | yes |  no |
| xPro LMS RC | courses-rc.xpro.mit.edu/heartbeat | yes |  no |
| xPro LMS production | courses.xpro.mit.edu/heartbeat | yes |  no |
| xPro RC | xpro-rc.odl.mit.edu | yes |  no |
| xPro preview RC | preview-rc.xpro.mit.edu/heartbeat | yes |  no |
| xPro preview production | preview.xpro.mit.edu/heartbeat | yes |  no |
| xPro production | xpro.mit.edu | yes |  no |
