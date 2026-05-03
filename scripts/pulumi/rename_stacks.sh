#!/usr/bin/env bash
# =============================================================================
# Pulumi Project-Scoped Stacks — Phase 4 Stack Rename Commands
#
# See docs/project-scoped-stacks-migration.md for full context and instructions.
#
# Prerequisites:
#   1. `pulumi state upgrade` has been run (Phase 3) — see docs for command
#   2. Phase 2a code PR is merged (missing constants added to pulumi_projects.py)
#   3. pulumi CLI >= 3.61.0
#   4. `pulumi login s3://mitol-pulumi-state` has been run
#
# Usage: Run per-section, not all at once. After each project's section,
#        merge the corresponding code PR (Pulumi.yaml + config file renames
#        + LEGACY_PROJECT_PREFIXES removal). Then run `pulumi preview` to
#        verify zero resource diff before proceeding to the next section.
#
# Stack rename syntax (post-upgrade):
#   pulumi stack rename --stack OLD_NAME NEW_NAME
#   (from the project directory; for project-name-change projects use full
#   org/project/stack form: organization/NEW_PROJECT_NAME/NEW_STACK_NAME)
#
# For projects where Pulumi.yaml name does NOT change, omit the org/project prefix.
# For projects where Pulumi.yaml name CHANGES, use the full form.
# =============================================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# ------------------------------------------------------------------------------
# GROUP A: PROJECT NAME UNCHANGED (18 projects, 96 stacks)
# Only stack names change. Rename from dotted to short form.
# For these, no org/project prefix needed — Pulumi.yaml name is already correct.
# ------------------------------------------------------------------------------

# echo "=== infrastructure/aws/dns (1 stack -> default) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/dns"
# pulumi stack rename --stack infrastructure.aws.dns default

# echo "=== infrastructure/aws/ecr (1 stack -> default) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/ecr"
# pulumi stack rename --stack infrastructure.aws.ecr default

# echo "=== infrastructure/aws/eks (12 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/eks"
# pulumi stack rename --stack infrastructure.aws.eks.applications.CI applications.CI
# pulumi stack rename --stack infrastructure.aws.eks.applications.QA applications.QA
# pulumi stack rename --stack infrastructure.aws.eks.applications.Production applications.Production
# pulumi stack rename --stack infrastructure.aws.eks.data.CI data.CI
# pulumi stack rename --stack infrastructure.aws.eks.data.QA data.QA
# pulumi stack rename --stack infrastructure.aws.eks.data.Production data.Production
# pulumi stack rename --stack infrastructure.aws.eks.operations.CI operations.CI
# pulumi stack rename --stack infrastructure.aws.eks.operations.QA operations.QA
# pulumi stack rename --stack infrastructure.aws.eks.operations.Production operations.Production
# pulumi stack rename --stack infrastructure.aws.eks.residential.CI residential.CI
# pulumi stack rename --stack infrastructure.aws.eks.residential.QA residential.QA
# pulumi stack rename --stack infrastructure.aws.eks.residential.Production residential.Production

# echo "=== infrastructure/aws/iam (1 stack -> default) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/iam"
# pulumi stack rename --stack infrastructure.aws.iam default

# echo "=== infrastructure/aws/kms (4 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/kms"
# pulumi stack rename --stack infrastructure.aws.kms.CI CI
# pulumi stack rename --stack infrastructure.aws.kms.QA QA
# pulumi stack rename --stack infrastructure.aws.kms.Production Production
# pulumi stack rename --stack infrastructure.aws.kms.Dev Dev

# echo "=== infrastructure/aws/network (4 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/network"
# pulumi stack rename --stack infrastructure.aws.network.CI CI
# pulumi stack rename --stack infrastructure.aws.network.QA QA
# pulumi stack rename --stack infrastructure.aws.network.Production Production
# pulumi stack rename --stack infrastructure.aws.network.Dev Dev

# echo "=== infrastructure/aws/opensearch (30 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/opensearch"
# pulumi stack rename --stack infrastructure.aws.opensearch.apps.CI apps.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.apps.QA apps.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.apps.Production apps.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.celery_monitoring.CI celery_monitoring.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.celery_monitoring.QA celery_monitoring.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.celery_monitoring.Production celery_monitoring.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.mitlearn.CI mitlearn.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.mitlearn.QA mitlearn.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.mitlearn.Production mitlearn.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.mitopen.QA mitopen.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.mitopen.Production mitopen.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.mitx.CI mitx.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.mitx.QA mitx.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.mitx.Production mitx.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.mitx-staging.CI mitx-staging.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.mitx-staging.QA mitx-staging.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.mitx-staging.Production mitx-staging.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.mitxonline.CI mitxonline.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.mitxonline.QA mitxonline.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.mitxonline.Production mitxonline.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.open.CI open.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.open.QA open.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.open.Production open.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.open_metadata.CI open_metadata.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.open_metadata.QA open_metadata.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.open_metadata.Production open_metadata.Production
# pulumi stack rename --stack infrastructure.aws.opensearch.xpro.CI xpro.CI
# pulumi stack rename --stack infrastructure.aws.opensearch.xpro.QA xpro.QA
# pulumi stack rename --stack infrastructure.aws.opensearch.xpro.Production xpro.Production

# echo "=== infrastructure/aws/policies (1 stack -> default) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/policies"
# pulumi stack rename --stack infrastructure.aws.policies default

# echo "=== infrastructure/aws/private_ca (1 stack -> default) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/private_ca"
# pulumi stack rename --stack infrastructure.aws.private_ca default

# echo "=== infrastructure/aws/s3_sites (2 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/s3_sites"
# pulumi stack rename --stack infrastructure.aws.s3_sites.QA QA
# pulumi stack rename --stack infrastructure.aws.s3_sites.Production Production

echo "=== infrastructure/aws/sftp_servers (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/sftp_servers"
# pulumi stack rename --stack infrastructure.aws.sftp_servers.CI CI
pulumi stack rename --stack infrastructure.aws.sftp_servers.QA QA
pulumi stack rename --stack infrastructure.aws.sftp_servers.Production Production

echo "=== infrastructure/consul (5 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/consul"
pulumi stack rename --stack infrastructure.consul.operations.CI operations.CI
pulumi stack rename --stack infrastructure.consul.operations.QA operations.QA
pulumi stack rename --stack infrastructure.consul.operations.Production operations.Production
pulumi stack rename --stack infrastructure.consul.data.QA data.QA
pulumi stack rename --stack infrastructure.consul.data.Production data.Production

echo "=== infrastructure/grafana_cloud (1 stack) ==="
cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/grafana_cloud"
pulumi stack rename --stack infrastructure.grafana_cloud.Production Production

echo "=== infrastructure/mongodb_atlas (12 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/mongodb_atlas"
pulumi stack rename --stack infrastructure.mongodb_atlas.mitx.CI mitx.CI
pulumi stack rename --stack infrastructure.mongodb_atlas.mitx.QA mitx.QA
pulumi stack rename --stack infrastructure.mongodb_atlas.mitx.Production mitx.Production
pulumi stack rename --stack infrastructure.mongodb_atlas.mitx-staging.CI mitx-staging.CI
pulumi stack rename --stack infrastructure.mongodb_atlas.mitx-staging.QA mitx-staging.QA
pulumi stack rename --stack infrastructure.mongodb_atlas.mitx-staging.Production mitx-staging.Production
pulumi stack rename --stack infrastructure.mongodb_atlas.mitxonline.CI mitxonline.CI
pulumi stack rename --stack infrastructure.mongodb_atlas.mitxonline.QA mitxonline.QA
pulumi stack rename --stack infrastructure.mongodb_atlas.mitxonline.Production mitxonline.Production
pulumi stack rename --stack infrastructure.mongodb_atlas.xpro.CI xpro.CI
pulumi stack rename --stack infrastructure.mongodb_atlas.xpro.QA xpro.QA
pulumi stack rename --stack infrastructure.mongodb_atlas.xpro.Production xpro.Production

echo "=== infrastructure/monitoring (1 stack -> default) ==="
cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/monitoring"
pulumi stack rename --stack infrastructure.monitoring default

echo "=== infrastructure/qdrant_cloud (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/qdrant_cloud"
pulumi stack rename --stack infrastructure.qdrant_cloud.mitlearn.CI mitlearn.CI
pulumi stack rename --stack infrastructure.qdrant_cloud.mitlearn.QA mitlearn.QA
pulumi stack rename --stack infrastructure.qdrant_cloud.mitlearn.Production mitlearn.Production

echo "=== infrastructure/vault (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/vault"
pulumi stack rename --stack infrastructure.vault.operations.CI operations.CI
pulumi stack rename --stack infrastructure.vault.operations.QA operations.QA
pulumi stack rename --stack infrastructure.vault.operations.Production operations.Production

echo "=== substructure/aws/eks (12 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/substructure/aws/eks"
pulumi stack rename --stack substructure.aws.eks.applications.CI applications.CI
pulumi stack rename --stack substructure.aws.eks.applications.QA applications.QA
pulumi stack rename --stack substructure.aws.eks.applications.Production applications.Production
pulumi stack rename --stack substructure.aws.eks.data.CI data.CI
pulumi stack rename --stack substructure.aws.eks.data.QA data.QA
pulumi stack rename --stack substructure.aws.eks.data.Production data.Production
pulumi stack rename --stack substructure.aws.eks.operations.CI operations.CI
pulumi stack rename --stack substructure.aws.eks.operations.QA operations.QA
pulumi stack rename --stack substructure.aws.eks.operations.Production operations.Production
pulumi stack rename --stack substructure.aws.eks.residential.CI residential.CI
pulumi stack rename --stack substructure.aws.eks.residential.QA residential.QA
pulumi stack rename --stack substructure.aws.eks.residential.Production residential.Production

# ------------------------------------------------------------------------------
# GROUP B: PROJECT NAME CHANGES (52 projects, 178 stacks)
# Use full org/project/stack form: organization/NEW_PROJECT_NAME/NEW_STACK_NAME
# Pulumi.yaml must be updated in the code PR AFTER these commands run.
# ------------------------------------------------------------------------------

echo "=== applications/airbyte (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/airbyte"
pulumi stack rename --stack applications.airbyte.CI organization/ol-application-airbyte/CI
pulumi stack rename --stack applications.airbyte.QA organization/ol-application-airbyte/QA
pulumi stack rename --stack applications.airbyte.Production organization/ol-application-airbyte/Production

echo "=== applications/b2b_partners_storage (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/b2b_partners_storage"
pulumi stack rename --stack applications.b2b_partners_storage.CI organization/ol-application-b2b-partners-storage/CI
pulumi stack rename --stack applications.b2b_partners_storage.QA organization/ol-application-b2b-partners-storage/QA
pulumi stack rename --stack applications.b2b_partners_storage.Production organization/ol-application-b2b-partners-storage/Production

echo "=== applications/bootcamps (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/bootcamps"
pulumi stack rename --stack applications.bootcamps.CI organization/ol-application-bootcamps/CI
pulumi stack rename --stack applications.bootcamps.QA organization/ol-application-bootcamps/QA
pulumi stack rename --stack applications.bootcamps.Production organization/ol-application-bootcamps/Production

echo "=== applications/celery_monitoring (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/celery_monitoring"
pulumi stack rename --stack applications.celery_monitoring.CI organization/ol-application-celery-monitoring/CI
pulumi stack rename --stack applications.celery_monitoring.QA organization/ol-application-celery-monitoring/QA
pulumi stack rename --stack applications.celery_monitoring.Production organization/ol-application-celery-monitoring/Production

echo "=== applications/clickhouse (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/clickhouse"
pulumi stack rename --stack applications.clickhouse.CI organization/ol-application-clickhouse/CI
pulumi stack rename --stack applications.clickhouse.QA organization/ol-application-clickhouse/QA
pulumi stack rename --stack applications.clickhouse.Production organization/ol-application-clickhouse/Production

echo "=== applications/codejail (12 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/codejail"
pulumi stack rename --stack applications.codejail.mitx.CI organization/ol-application-codejail/mitx.CI
pulumi stack rename --stack applications.codejail.mitx.QA organization/ol-application-codejail/mitx.QA
pulumi stack rename --stack applications.codejail.mitx.Production organization/ol-application-codejail/mitx.Production
pulumi stack rename --stack applications.codejail.mitx-staging.CI organization/ol-application-codejail/mitx-staging.CI
pulumi stack rename --stack applications.codejail.mitx-staging.QA organization/ol-application-codejail/mitx-staging.QA
pulumi stack rename --stack applications.codejail.mitx-staging.Production organization/ol-application-codejail/mitx-staging.Production
pulumi stack rename --stack applications.codejail.mitxonline.CI organization/ol-application-codejail/mitxonline.CI
pulumi stack rename --stack applications.codejail.mitxonline.QA organization/ol-application-codejail/mitxonline.QA
pulumi stack rename --stack applications.codejail.mitxonline.Production organization/ol-application-codejail/mitxonline.Production
pulumi stack rename --stack applications.codejail.xpro.CI organization/ol-application-codejail/xpro.CI
pulumi stack rename --stack applications.codejail.xpro.QA organization/ol-application-codejail/xpro.QA
pulumi stack rename --stack applications.codejail.xpro.Production organization/ol-application-codejail/xpro.Production

echo "=== applications/concourse (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/concourse"
pulumi stack rename --stack applications.concourse.CI organization/ol-application-concourse/CI
pulumi stack rename --stack applications.concourse.QA organization/ol-application-concourse/QA
pulumi stack rename --stack applications.concourse.Production organization/ol-application-concourse/Production

echo "=== applications/dagster (4 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/dagster"
pulumi stack rename --stack applications.dagster.CI organization/ol-application-dagster/CI
pulumi stack rename --stack applications.dagster.QA organization/ol-application-dagster/QA
pulumi stack rename --stack applications.dagster.Production organization/ol-application-dagster/Production
# pulumi stack rename --stack applications.dagster.Dev organization/ol-application-dagster/Dev

echo "=== applications/digital_credentials (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/digital_credentials"
pulumi stack rename --stack applications.digital_credentials.CI organization/ol-application-digital-credentials/CI
pulumi stack rename --stack applications.digital_credentials.QA organization/ol-application-digital-credentials/QA
pulumi stack rename --stack applications.digital_credentials.Production organization/ol-application-digital-credentials/Production

# echo "=== applications/ecs_test (1 stack) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/ecs_test"
# # current stack name is 'dev' (already short); project name changes only
# pulumi stack rename --stack dev organization/ol-application-ecs-test/dev

echo "=== applications/edx_notes (12 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/edx_notes"
pulumi stack rename --stack applications.edxnotes.mitx.CI organization/ol-application-edx-notes/mitx.CI
pulumi stack rename --stack applications.edxnotes.mitx.QA organization/ol-application-edx-notes/mitx.QA
pulumi stack rename --stack applications.edxnotes.mitx.Production organization/ol-application-edx-notes/mitx.Production
pulumi stack rename --stack applications.edxnotes.mitx-staging.CI organization/ol-application-edx-notes/mitx-staging.CI
pulumi stack rename --stack applications.edxnotes.mitx-staging.QA organization/ol-application-edx-notes/mitx-staging.QA
pulumi stack rename --stack applications.edxnotes.mitx-staging.Production organization/ol-application-edx-notes/mitx-staging.Production
pulumi stack rename --stack applications.edxnotes.mitxonline.CI organization/ol-application-edx-notes/mitxonline.CI
pulumi stack rename --stack applications.edxnotes.mitxonline.QA organization/ol-application-edx-notes/mitxonline.QA
pulumi stack rename --stack applications.edxnotes.mitxonline.Production organization/ol-application-edx-notes/mitxonline.Production
pulumi stack rename --stack applications.edxnotes.xpro.CI organization/ol-application-edx-notes/xpro.CI
pulumi stack rename --stack applications.edxnotes.xpro.QA organization/ol-application-edx-notes/xpro.QA
pulumi stack rename --stack applications.edxnotes.xpro.Production organization/ol-application-edx-notes/xpro.Production

echo "=== applications/edxapp (12 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/edxapp"
pulumi stack rename --stack applications.edxapp.mitx.CI organization/ol-application-edxapp/mitx.CI
pulumi stack rename --stack applications.edxapp.mitx.QA organization/ol-application-edxapp/mitx.QA
pulumi stack rename --stack applications.edxapp.mitx.Production organization/ol-application-edxapp/mitx.Production
pulumi stack rename --stack applications.edxapp.mitx-staging.CI organization/ol-application-edxapp/mitx-staging.CI
pulumi stack rename --stack applications.edxapp.mitx-staging.QA organization/ol-application-edxapp/mitx-staging.QA
pulumi stack rename --stack applications.edxapp.mitx-staging.Production organization/ol-application-edxapp/mitx-staging.Production
pulumi stack rename --stack applications.edxapp.mitxonline.CI organization/ol-application-edxapp/mitxonline.CI
pulumi stack rename --stack applications.edxapp.mitxonline.QA organization/ol-application-edxapp/mitxonline.QA
pulumi stack rename --stack applications.edxapp.mitxonline.Production organization/ol-application-edxapp/mitxonline.Production
pulumi stack rename --stack applications.edxapp.xpro.CI organization/ol-application-edxapp/xpro.CI
pulumi stack rename --stack applications.edxapp.xpro.QA organization/ol-application-edxapp/xpro.QA
pulumi stack rename --stack applications.edxapp.xpro.Production organization/ol-application-edxapp/xpro.Production

echo "=== applications/fastly_redirector (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/fastly_redirector"
pulumi stack rename --stack applications.fastly_redirector.CI organization/ol-application-fastly-redirector/CI
pulumi stack rename --stack applications.fastly_redirector.QA organization/ol-application-fastly-redirector/QA
pulumi stack rename --stack applications.fastly_redirector.Production organization/ol-application-fastly-redirector/Production

echo "=== applications/jupyterhub (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/jupyterhub"
pulumi stack rename --stack applications.jupyterhub.CI organization/ol-application-jupyterhub/CI
pulumi stack rename --stack applications.jupyterhub.QA organization/ol-application-jupyterhub/QA
pulumi stack rename --stack applications.jupyterhub.Production organization/ol-application-jupyterhub/Production

echo "=== applications/keycloak (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/keycloak"
pulumi stack rename --stack applications.keycloak.CI organization/ol-application-keycloak/CI
pulumi stack rename --stack applications.keycloak.QA organization/ol-application-keycloak/QA
pulumi stack rename --stack applications.keycloak.Production organization/ol-application-keycloak/Production

echo "=== applications/kubewatch (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/kubewatch"
pulumi stack rename --stack applications.kubewatch.applications.CI organization/ol-application-kubewatch/applications.CI
pulumi stack rename --stack applications.kubewatch.applications.QA organization/ol-application-kubewatch/applications.QA
pulumi stack rename --stack applications.kubewatch.applications.Production organization/ol-application-kubewatch/applications.Production

echo "=== applications/kubewatch_webhook_handler (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/kubewatch_webhook_handler"
pulumi stack rename --stack applications.kubewatch_webhook_handler.applications.CI organization/ol-application-kubewatch-webhook/applications.CI
pulumi stack rename --stack applications.kubewatch_webhook_handler.applications.QA organization/ol-application-kubewatch-webhook/applications.QA
pulumi stack rename --stack applications.kubewatch_webhook_handler.applications.Production organization/ol-application-kubewatch-webhook/applications.Production

echo "=== applications/learn_ai (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/learn_ai"
pulumi stack rename --stack applications.learn_ai.CI organization/ol-application-learn-ai/CI
pulumi stack rename --stack applications.learn_ai.QA organization/ol-application-learn-ai/QA
pulumi stack rename --stack applications.learn_ai.Production organization/ol-application-learn-ai/Production

echo "=== applications/mailgun (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/mailgun"
pulumi stack rename --stack applications.mailgun.CI organization/ol-application-mailgun/CI
pulumi stack rename --stack applications.mailgun.QA organization/ol-application-mailgun/QA
pulumi stack rename --stack applications.mailgun.Production organization/ol-application-mailgun/Production

echo "=== applications/micromasters (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/micromasters"
pulumi stack rename --stack applications.micromasters.CI organization/ol-application-micromasters/CI
pulumi stack rename --stack applications.micromasters.QA organization/ol-application-micromasters/QA
pulumi stack rename --stack applications.micromasters.Production organization/ol-application-micromasters/Production

echo "=== applications/mit_learn (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/mit_learn"
pulumi stack rename --stack applications.mit_learn.CI organization/ol-application-mit-learn/CI
pulumi stack rename --stack applications.mit_learn.QA organization/ol-application-mit-learn/QA
pulumi stack rename --stack applications.mit_learn.Production organization/ol-application-mit-learn/Production

echo "=== applications/mit_learn_nextjs (3 stacks) ==="
cd "$REPO_ROOT/src/ol_infrastructure/applications/mit_learn_nextjs"
pulumi stack rename --stack applications.mit_learn_nextjs.CI organization/ol-application-mit-learn-nextjs/CI
pulumi stack rename --stack applications.mit_learn_nextjs.QA organization/ol-application-mit-learn-nextjs/QA
pulumi stack rename --stack applications.mit_learn_nextjs.Production organization/ol-application-mit-learn-nextjs/Production

# echo "=== applications/mitx (1 stack) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/mitx"
# pulumi stack rename --stack applications.mitx.QA organization/ol-application-mitx/QA

# echo "=== applications/mitxonline (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/mitxonline"
# pulumi stack rename --stack applications.mitxonline.CI organization/ol-application-mitxonline/CI
# pulumi stack rename --stack applications.mitxonline.QA organization/ol-application-mitxonline/QA
# pulumi stack rename --stack applications.mitxonline.Production organization/ol-application-mitxonline/Production

# echo "=== applications/ocw_site (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/ocw_site"
# pulumi stack rename --stack applications.ocw_site.CI organization/ol-application-ocw-site/CI
# pulumi stack rename --stack applications.ocw_site.QA organization/ol-application-ocw-site/QA
# pulumi stack rename --stack applications.ocw_site.Production organization/ol-application-ocw-site/Production

# echo "=== applications/ocw_studio (4 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/ocw_studio"
# pulumi stack rename --stack applications.ocw_studio.CI organization/ol-application-ocw-studio/CI
# pulumi stack rename --stack applications.ocw_studio.QA organization/ol-application-ocw-studio/QA
# pulumi stack rename --stack applications.ocw_studio.Production organization/ol-application-ocw-studio/Production
# # pulumi stack rename --stack applications.ocw_studio.Dev organization/ol-application-ocw-studio/Dev

# echo "=== applications/odl_video_service (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/odl_video_service"
# pulumi stack rename --stack applications.odl_video_service.CI organization/ol-application-odl-video-service/CI
# pulumi stack rename --stack applications.odl_video_service.QA organization/ol-application-odl-video-service/QA
# pulumi stack rename --stack applications.odl_video_service.Production organization/ol-application-odl-video-service/Production

# echo "=== applications/open_discussions (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/open_discussions"
# pulumi stack rename --stack applications.open_discussions.CI organization/ol-application-open-discussions/CI
# pulumi stack rename --stack applications.open_discussions.QA organization/ol-application-open-discussions/QA
# pulumi stack rename --stack applications.open_discussions.Production organization/ol-application-open-discussions/Production

# echo "=== applications/open_metadata (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/open_metadata"
# pulumi stack rename --stack applications.open_metadata.CI organization/ol-application-open-metadata/CI
# pulumi stack rename --stack applications.open_metadata.QA organization/ol-application-open-metadata/QA
# pulumi stack rename --stack applications.open_metadata.Production organization/ol-application-open-metadata/Production

# echo "=== applications/redash (2 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/redash"
# pulumi stack rename --stack applications.redash.QA organization/ol-application-redash/QA
# pulumi stack rename --stack applications.redash.Production organization/ol-application-redash/Production

# echo "=== applications/starburst (1 stack) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/starburst"
# pulumi stack rename --stack applications.starburst.Production organization/ol-application-starburst/Production

# echo "=== applications/starrocks (2 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/starrocks"
# pulumi stack rename --stack applications.starrocks.lakehouse.QA organization/ol-application-starrocks/lakehouse.QA
# pulumi stack rename --stack applications.starrocks.lakehouse.Production organization/ol-application-starrocks/lakehouse.Production

# echo "=== applications/superset (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/superset"
# pulumi stack rename --stack applications.superset.CI organization/ol-application-superset/CI
# pulumi stack rename --stack applications.superset.QA organization/ol-application-superset/QA
# pulumi stack rename --stack applications.superset.Production organization/ol-application-superset/Production

# echo "=== applications/tika (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/tika"
# pulumi stack rename --stack applications.tika.CI organization/ol-application-tika/CI
# pulumi stack rename --stack applications.tika.QA organization/ol-application-tika/QA
# pulumi stack rename --stack applications.tika.Production organization/ol-application-tika/Production

# echo "=== applications/xpro (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/xpro"
# pulumi stack rename --stack applications.xpro.CI organization/ol-application-xpro/CI
# pulumi stack rename --stack applications.xpro.QA organization/ol-application-xpro/QA
# pulumi stack rename --stack applications.xpro.Production organization/ol-application-xpro/Production

# echo "=== applications/xqueue (9 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/xqueue"
# pulumi stack rename --stack applications.xqueue.mitx.CI organization/ol-application-xqueue/mitx.CI
# pulumi stack rename --stack applications.xqueue.mitx.QA organization/ol-application-xqueue/mitx.QA
# pulumi stack rename --stack applications.xqueue.mitx.Production organization/ol-application-xqueue/mitx.Production
# pulumi stack rename --stack applications.xqueue.mitx-staging.CI organization/ol-application-xqueue/mitx-staging.CI
# pulumi stack rename --stack applications.xqueue.mitx-staging.QA organization/ol-application-xqueue/mitx-staging.QA
# pulumi stack rename --stack applications.xqueue.mitx-staging.Production organization/ol-application-xqueue/mitx-staging.Production
# pulumi stack rename --stack applications.xqueue.mitxonline.CI organization/ol-application-xqueue/mitxonline.CI
# pulumi stack rename --stack applications.xqueue.mitxonline.QA organization/ol-application-xqueue/mitxonline.QA
# pulumi stack rename --stack applications.xqueue.mitxonline.Production organization/ol-application-xqueue/mitxonline.Production

# echo "=== applications/xqwatcher (9 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/applications/xqwatcher"
# pulumi stack rename --stack applications.xqwatcher.mitx.CI organization/ol-application-xqwatcher/mitx.CI
# pulumi stack rename --stack applications.xqwatcher.mitx.QA organization/ol-application-xqwatcher/mitx.QA
# pulumi stack rename --stack applications.xqwatcher.mitx.Production organization/ol-application-xqwatcher/mitx.Production
# pulumi stack rename --stack applications.xqwatcher.mitx-staging.CI organization/ol-application-xqwatcher/mitx-staging.CI
# pulumi stack rename --stack applications.xqwatcher.mitx-staging.QA organization/ol-application-xqwatcher/mitx-staging.QA
# pulumi stack rename --stack applications.xqwatcher.mitx-staging.Production organization/ol-application-xqwatcher/mitx-staging.Production
# pulumi stack rename --stack applications.xqwatcher.mitxonline.CI organization/ol-application-xqwatcher/mitxonline.CI
# pulumi stack rename --stack applications.xqwatcher.mitxonline.QA organization/ol-application-xqwatcher/mitxonline.QA
# pulumi stack rename --stack applications.xqwatcher.mitxonline.Production organization/ol-application-xqwatcher/mitxonline.Production

# echo "=== infrastructure/aws/data_warehouse (2 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/aws/data_warehouse"
# pulumi stack rename --stack infrastructure.aws.data_warehouse.QA organization/ol-infrastructure-data-warehouse/QA
# pulumi stack rename --stack infrastructure.aws.data_warehouse.Production organization/ol-infrastructure-data-warehouse/Production

# echo "=== infrastructure/gcp/gemini (0 stacks — Pulumi.yaml name fix only) ==="
# No stacks to rename. Just update Pulumi.yaml name: to ol-infrastructure-gemini-api in the code PR.

# echo "=== infrastructure/vector_log_proxy (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/infrastructure/vector_log_proxy"
# pulumi stack rename --stack infrastructure.vector_log_proxy.operations.CI organization/ol-infrastructure-vector-log-proxy/operations.CI
# pulumi stack rename --stack infrastructure.vector_log_proxy.operations.QA organization/ol-infrastructure-vector-log-proxy/operations.QA
# pulumi stack rename --stack infrastructure.vector_log_proxy.operations.Production organization/ol-infrastructure-vector-log-proxy/operations.Production

# echo "=== substructure/consul (5 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/consul"
# pulumi stack rename --stack substructure.consul.operations.CI organization/ol-substructure-consul/operations.CI
# pulumi stack rename --stack substructure.consul.operations.QA organization/ol-substructure-consul/operations.QA
# pulumi stack rename --stack substructure.consul.operations.Production organization/ol-substructure-consul/operations.Production
# pulumi stack rename --stack substructure.consul.data.QA organization/ol-substructure-consul/data.QA
# pulumi stack rename --stack substructure.consul.data.Production organization/ol-substructure-consul/data.Production

# echo "=== substructure/keycloak (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/keycloak"
# pulumi stack rename --stack substructure.keycloak.CI organization/ol-substructure-keycloak/CI
# pulumi stack rename --stack substructure.keycloak.QA organization/ol-substructure-keycloak/QA
# pulumi stack rename --stack substructure.keycloak.Production organization/ol-substructure-keycloak/Production

# echo "=== substructure/starrocks (2 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/starrocks"
# pulumi stack rename --stack substructure.starrocks.lakehouse.QA organization/ol-substructure-starrocks/lakehouse.QA
# pulumi stack rename --stack substructure.starrocks.lakehouse.Production organization/ol-substructure-starrocks/lakehouse.Production

# echo "=== substructure/tls_certificates (0 stacks — Pulumi.yaml name fix only) ==="
# # No stacks to rename. Update Pulumi.yaml name: to ol-substructure-tls-certificates in the code PR.

# echo "=== substructure/vault/approle (0 stacks — Pulumi.yaml name fix only) ==="
# # No stacks to rename. Update Pulumi.yaml name: to ol-substructure-vault-approles in the code PR.

# echo "=== substructure/vault/auth (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/vault/auth"
# pulumi stack rename --stack substructure.vault.auth.operations.CI organization/ol-substructure-vault-auth/operations.CI
# pulumi stack rename --stack substructure.vault.auth.operations.QA organization/ol-substructure-vault-auth/operations.QA
# pulumi stack rename --stack substructure.vault.auth.operations.Production organization/ol-substructure-vault-auth/operations.Production

# echo "=== substructure/vault/encryption_mounts (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/vault/encryption_mounts"
# pulumi stack rename --stack substructure.vault.encryption_mounts.operations.CI organization/ol-substructure-vault-encryption-mounts/operations.CI
# pulumi stack rename --stack substructure.vault.encryption_mounts.operations.QA organization/ol-substructure-vault-encryption-mounts/operations.QA
# pulumi stack rename --stack substructure.vault.encryption_mounts.operations.Production organization/ol-substructure-vault-encryption-mounts/operations.Production

# echo "=== substructure/vault/pki (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/vault/pki"
# pulumi stack rename --stack substructure.vault.pki.operations.CI organization/ol-substructure-vault-pki/operations.CI
# pulumi stack rename --stack substructure.vault.pki.operations.QA organization/ol-substructure-vault-pki/operations.QA
# pulumi stack rename --stack substructure.vault.pki.operations.Production organization/ol-substructure-vault-pki/operations.Production

# echo "=== substructure/vault/secrets (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/vault/secrets"
# pulumi stack rename --stack substructure.vault.secrets.operations.CI organization/ol-substructure-vault-secrets/operations.CI
# pulumi stack rename --stack substructure.vault.secrets.operations.QA organization/ol-substructure-vault-secrets/operations.QA
# pulumi stack rename --stack substructure.vault.secrets.operations.Production organization/ol-substructure-vault-secrets/operations.Production

# echo "=== substructure/vault/setup (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/vault/setup"
# pulumi stack rename --stack substructure.vault.setup.operations.CI organization/ol-substructure-vault-setup/operations.CI
# pulumi stack rename --stack substructure.vault.setup.operations.QA organization/ol-substructure-vault-setup/operations.QA
# pulumi stack rename --stack substructure.vault.setup.operations.Production organization/ol-substructure-vault-setup/operations.Production

# echo "=== substructure/vault/static_mounts (3 stacks) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/vault/static_mounts"
# pulumi stack rename --stack substructure.vault.static_mounts.operations.CI organization/ol-substructure-vault-static-mounts/operations.CI
# pulumi stack rename --stack substructure.vault.static_mounts.operations.QA organization/ol-substructure-vault-static-mounts/operations.QA
# pulumi stack rename --stack substructure.vault.static_mounts.operations.Production organization/ol-substructure-vault-static-mounts/operations.Production

# echo "=== substructure/xpro_partner_dns (1 stack -> default) ==="
# cd "$REPO_ROOT/src/ol_infrastructure/substructure/xpro_partner_dns"
# pulumi stack rename --stack substructure.xpro_partner_dns organization/ol-substructure-xpro-partner-dns/default

echo "=== ALL RENAMES COMPLETE ==="
echo "Now merge the code PRs: Pulumi.yaml renames + LEGACY_PROJECT_PREFIXES removal"
