# How To Update The Release Version Of An Open edX Deployment

## Open edX Releases
Open edX cuts new named releases [approximately every 6
months](https://openedx.atlassian.net/wiki/spaces/COMM/pages/3613392957/Open+edX+release+schedule). Each
of our deployments needs to be able to independently upgrade the respective version on
their own schedule. MITx Residential upgrades twice per year, roughly aligned with the
release timing of the upstream Open edX platform, whereas xPro has typically upgraded on
an annual basis in the December timeframe.

Our MITx Online deployment of Open edX is a special case in that it deploys directly
from the master branches of the different applications and has a weekly release
schedule.

## Open Learning's Open edX Deployments
At Open Learning we have four distinct installations of Open edX, each with their own
release cadence and configuration requirements. For each of those deployments we have
multiple environment stages (e.g. CI, QA, Production). Each of those deployment * stages
combinations needs to be able to have their versions managed independently, with newer
versions being progressively promoted to higher environments.

This is further complicated by the long testing cycles for new releases in a given
deployment. We need to be able to deploy a newer release to a lower environment stage,
while maintaining the ability to deploy patches of the current release in a given time
period to subsequent environments. For example, as we start testing the Palm release for
the MITx Residential deployment we need to get it installed in the CI
environment. Meanwhile, we may have a bug-fix or security patch that needs to be tested
in the QA environment and propagated to Production.

In addition to the cartesian product of (versions * deployments * env stages) we also
have to manage these values across the different components of the Open edX
ecosystem. For example, the core of the platform is called edx-platform, which in turn
relies on:
- a forum service
- the edx-notes API service
- a codejail REST API service
- and myriad MFEs (Micro-FrontEnds).

For any of these different components we may need to be able to override the repository
and/or branch that we are building/deploying from.

## Versioning of Build and Deploy
In order to support this sea of complexity we have a module of `bridge.settings.openedx`
that has a combination of data structures and helper functions to control what
applications and versions get deployed where and when. These values are then used in the
build stages of the different components, as well as driving the deployment pipelines in
Concourse that are responsible for orchestrating all of this.

## Managing Supported Releases
We only want to support the build and deployment of a minimal subset of Open edX
releases. This is controlled by the Enum `OpenEdxSupportedRelease` in the
`bridge.settings.openedx.types` module. When there is a new release created it needs to
be added to this Enum.  For example, to add support for the Palm release a new line is
added of the format `palm = ("palm", "open-release/palm.master")`

When we have upgraded all deployments past a given release we remove it from that Enum
so that we no longer need to worry about maintaining configuration/code/etc. for that
release.

## Performing An Upgrade
There are two data structures that control the applications and versions that get
included in a given deployment and which version to use in the respective environment
stage. The `OpenLearningOpenEdxDeployment` Enum is the top level that sets the release
name for the environment stage of a given deployment.

There are situations where we need to customize a component that is being deployed. In
those cases we typically create a fork of the upstream repository where we manage the
patches that we require. The `ReleaseMap` dictionary is used to manage any overrides of
repository and branch for a given component of the platform, as well as which components
are included in that deployment. The `OpenEdxApplicationVersion` structure will map to
the default repositories and branches for a given component, but supplies a
`branch_override` and `origin_override` parameter to manage these customizations.

For example, to upgrade our MITx Residential deployments to start testing the Palm
release we change the `CI` stages of the `mitx` and `mitx-staging` deployments to use
the `palm` value for the `OpenEdxSupportedRelease`

```diff
@@ -13,7 +13,7 @@ class OpenLearningOpenEdxDeployment(Enum):
     mitx = DeploymentEnvRelease(
         deployment_name="mitx",
         env_release_map=[
-            EnvRelease("CI", OpenEdxSupportedRelease["olive"]),
+            EnvRelease("CI", OpenEdxSupportedRelease["palm"]),
             EnvRelease("QA", OpenEdxSupportedRelease["olive"]),
             EnvRelease("Production", OpenEdxSupportedRelease["olive"]),
         ],
@@ -21,7 +21,7 @@ class OpenLearningOpenEdxDeployment(Enum):
     mitx_staging = DeploymentEnvRelease(
         deployment_name="mitx-staging",
         env_release_map=[
-            EnvRelease("CI", OpenEdxSupportedRelease["olive"]),
+            EnvRelease("CI", OpenEdxSupportedRelease["palm"]),
             EnvRelease("QA", OpenEdxSupportedRelease["olive"]),
             EnvRelease("Production", OpenEdxSupportedRelease["olive"]),
         ],
```

Because Palm is a new release for these deployments we also need to add a `palm` key to
the `ReleaseMap` dictionary that contains the applications that are associated with
those deployments and the appropriate `OpenEdxApplicationVersion` records for that
deployment.

```diff
@@ -61,6 +61,122 @@ ReleaseMap: dict[
     OpenEdxSupportedRelease,
     dict[OpenEdxDeploymentName, list[OpenEdxApplicationVersion]],
 ] = {
+    "palm": {
+        "mitx": [
+            OpenEdxApplicationVersion(
+                application="edx-platform",  # type: ignore
+                application_type="IDA",
+                release="palm",
+                branch_override="mitx/palm",
+                origin_override="https://github.com/mitodl/edx-platform",
+            ),
             ...
+        ],
+        "mitx-staging": [
+            OpenEdxApplicationVersion(
+                application="edx-platform",  # type: ignore
+                application_type="IDA",
+                release="palm",
+                branch_override="mitx/palm",
+                origin_override="https://github.com/mitodl/edx-platform",
+            ),
             ...
+        ],
+    },
     "olive": {
         "mitx": [
             OpenEdxApplicationVersion(
```

All of the [deployment
pipelines](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_concourse/pipelines/open_edx/)
for these application components are managed by a corresponding `meta` pipeline that
will automatically update the build and pipeline configuration based on the changed
version information as soon as it is merged into the `master` branch of
`ol-infrastructure`.

## Supporting New Applications
There are two categories of applications that comprise the overall Open edX
platform. These are "IDA"s (Independently Deployable Applications), and "MFE"s (Micro
Front-Ends). An IDA is a Django application that can be deployed as a backend service
and typically integrates with the edx-platform (LMS and CMS) via OAuth. An MFE is a
ReactJS application that is deployed as a standalone site which then interacts with LMS
and/or CMS via REST APIs.

In order to ensure that we have visibility into which of these components we are
deploying there is an Enum of
[`OpenEdxApplication`](https://github.com/mitodl/ol-infrastructure/blob/main/src/bridge/settings/openedx/types.py#L7)
and
[`OpenEdxMicroFrontend`](https://github.com/mitodl/ol-infrastructure/blob/main/src/bridge/settings/openedx/types.py#L25)
respectively which captures the details of which elements of the overall edX ecosystem
we are supporting in our deployments. These Enums are then used as an attribute of the
[`OpenEdxApplicationVersion`](https://github.com/mitodl/ol-infrastructure/blob/main/src/bridge/settings/openedx/types.py#L121)
model which captures the details of a specific instance of one of those
components. These are then used in the `ReleaseMap` dict to show which versions and
deployments include the given instance of that application and version.

To add support for a new IDA or MFE you need to add a new entry to the appropriate Enum,
and then include an instance of an `OpenEdxApplicationVersion` that points to that
application in the `ReleaseMap` at the appropriate location.

**Note - TMM 2023-04-14**

- The current configuration of our meta pipelines means that before the updates to the
  `bridge.settings.openedx.version_matrix` module can be picked up by the `meta`
  pipelines the `ol-infrastructure` docker image needs to be built and pushed to our
  registry so that it can be loaded. This means that once the [ol-infrastructure image
  pipeline](https://cicd.odl.mit.edu/teams/main/pipelines/ol-infrastructure-docker-container)
  completes it might be necessary to manually trigger the meta pipelines again.
- Once a new release is deployed to a given environment stage for the first time it may
  be necessary to manually ensure that all database migrations are run properly. It will
  be attempted automatically on deployment, but there are often conflicts between the
  existing database state and the migration logic that require intervention.

## Troubleshooting

### OpenEdX Redwood
With each new OpenEdX release, inevitably there will be problems. For instance,
in the Redwood release, we noticed that login for MITX CI was failing with a 500
response code.

So we ran the following Grafana query using the query builder:

```
{environment="mitx-ci", application="edxapp"} |= `` | json | line_format `{{.message}}
```

That yielded a problem with JWT signing keys as the exception cited a signing
failure.

So we had to log onto an EC2 instance appropriate to MITX CI and run the
following command from the lms container shell:

```
manage.py generate_jwt_signing_key
```

That produced public and private signing key JSON blobs, which we could then add
to SOPS and then through the pipeline into Vault where they'll be picked up as
Redwood deploys to the various apps and environments.
