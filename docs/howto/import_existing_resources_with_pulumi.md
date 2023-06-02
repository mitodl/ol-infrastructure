# Importing Resources That Already Exist Into Your Pulumi Stack

## Summary

There are several ways to skin this particular cat but Tobias has shown me one
that works really well so that's what we'll outline here.

There's another approach using the [pulumi import CLI command](https://www.pulumi.com/docs/cli/commands/pulumi_import/)
but in my experience that brings in a bunch of extra attributes we don't want.
(There may be ways to tune this, I just don't know them.)

## Whole Cloth

Before you start importing, code your resources the same way you always would.

For example, if you need an S3 bucket, use all the usual Pulumi code -
s3.Bucket etc.

You want to code such that in a disaster recovery scenario, if we were staring from
scratch, the resources would get build 100% correctly and the applications they
support would pass all monitoring checks and smoke tests and function normally.

## Bring On The Special (Import) Sauce

If you know you want to keep existing resources while having Pulumi create the
rest, you should tell it to import these resources by passing in a
ResourceOptions object at resource creation time. Here's an example of our S3
bucket:

```python
bootcamps_storage_bucket_name = f"ol-bootcamps-app-{stack_info.env_suffix}"
bootcamps_storage_bucket = s3.Bucket(
    f"ol-bootcamps-app-{stack_info.env_suffix}",
    ### ****THIS LINE IS THE IMPORT CLAUSE****
    opts=ResourceOptions(import_=bootcamps_storage_bucket_name,ignore_changes=[]),
    bucket=bootcamps_storage_bucket_name,
    # ...
```

Note that obviously the object you're creating will differ if it's not an S3 bucket,
for example a Vault mount point.

## Tuning

After having added the above code, go ahead and run pulumi up on your stack. At this
stage DO NOT SAY YES to finalizing these changes if there are any diagnostic warnigns
displayed.

Here's an example of something like what we'd expect:

```
# cpatti @ rocinante in ~/src/mit/ol-infrastructure/src/ol_infrastructure/applications/bootcamps on git:cpatti_pulumi_bootcamp x ol-infrastructure-yqmQEgvq-py3.11 [14:31:28]
$ pulumi up  -s applications.bootcamps_ecommerce.Production
Previewing update (applications.bootcamps_ecommerce.Production):
     Type                                                      Name                                                                                           Plan       Info
 +   pulumi:pulumi:Stack                                       ol-infrastructure-bootcamps-ecommerce-application-applications.bootcamps_ecommerce.Production  create
 +   ├─ ol:infrastructure:aws:database:OLAmazonDB              bootcamps-db-applications-production                                                           create
 +   │  ├─ aws:rds:ParameterGroup                              bootcamps-db-applications-production-postgres-parameter-group                                  create
 +   │  ├─ aws:rds:Instance                                    bootcamps-db-applications-production-postgres-instance                                         create
 +   │  └─ aws:rds:Instance                                    bootcamps-db-applications-production-postgres-replica                                          create
 +   ├─ ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS  bootcamps-db-applications-production-CPUUtilization-OLCloudWatchAlarmSimpleRDSConfig           create
 +   │  └─ aws:cloudwatch:MetricAlarm                          bootcamps-db-applications-production-CPUUtilization-simple-rds-alarm                           create
 +   ├─ ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS  bootcamps-db-applications-production-WriteLatency-OLCloudWatchAlarmSimpleRDSConfig             create
 +   │  └─ aws:cloudwatch:MetricAlarm                          bootcamps-db-applications-production-WriteLatency-simple-rds-alarm                             create
 +   ├─ ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS  bootcamps-db-applications-production-FreeStorageSpace-OLCloudWatchAlarmSimpleRDSConfig         create
 +   │  └─ aws:cloudwatch:MetricAlarm                          bootcamps-db-applications-production-FreeStorageSpace-simple-rds-alarm                         create
 +   ├─ ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS  bootcamps-db-applications-production-EBSIOBlance-OLCloudWatchAlarmSimpleRDSConfig              create
 +   │  └─ aws:cloudwatch:MetricAlarm                          bootcamps-db-applications-production-EBSIOBalance%-simple-rds-alarm                            create
 +   ├─ ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS  bootcamps-db-applications-production-DiskQueueDepth-OLCloudWatchAlarmSimpleRDSConfig           create
 +   │  └─ aws:cloudwatch:MetricAlarm                          bootcamps-db-applications-production-DiskQueueDepth-simple-rds-alarm                           create
 +   ├─ ol:infrastructure.aws.cloudwatch.OLCloudWatchAlarmRDS  bootcamps-db-applications-production-ReadLatency-OLCloudWatchAlarmSimpleRDSConfig              create
 +   │  └─ aws:cloudwatch:MetricAlarm                          bootcamps-db-applications-production-ReadLatency-simple-rds-alarm                              create
 +   ├─ ol:services:Vault:DatabaseBackend:postgresql           bootcamps                                                                                      create
 +   │  └─ vault:index:Mount                                   bootcamps-mount-point                                                                          create
 +   │     └─ vault:database:SecretBackendConnection           bootcamps-database-connection                                                                  create
 +   │        ├─ vault:database:SecretBackendRole              bootcamps-database-role-approle                                                                create
 +   │        ├─ vault:database:SecretBackendRole              bootcamps-database-role-admin                                                                  create
 +   │        ├─ vault:database:SecretBackendRole              bootcamps-database-role-readonly                                                               create
 +   │        └─ vault:database:SecretBackendRole              bootcamps-database-role-app                                                                    create
 +   ├─ pulumi:providers:vault                                 vault-provider                                                                                 create
 +   ├─ aws:iam:Policy                                         bootcamps-production-policy                                                                    create
 =   ├─ aws:s3:Bucket                                          ol-bootcamps-app-production                                                                    import     [diff: -tagsAll~tags]; 1 warning
 =   ├─ vault:index:Mount                                      bootcamps-vault-secrets-storage                                                                import
 +   ├─ aws:ec2:SecurityGroup                                  bootcamps-db-access-production                                                                 create
 +   └─ vault:aws:SecretBackendRole                            bootcamps-app-production                                                                       create


Diagnostics:
  aws:s3:Bucket (ol-bootcamps-app-production):
    warning: inputs to import do not match the existing resource; importing this resource will fail

Outputs:
    bootcamps_app: {
        rds_host: output<string>
    }

```

The two key bits of output to focus on here are the diagnostic warning towards the end:

```
Diagnostics:
  aws:s3:Bucket (ol-bootcamps-app-production):
    warning: inputs to import do not match the existing resource; importing this resource will fail

```

This tells us that Pulumi has detected a critical difference between the
resource's state in the real world and Pulumi's model of what's there and
what needs to change to arrive at the desired state.

The next most important bit is nestled amongst Pulumi telling us what changes it plans to make:

`=   ├─ aws:s3:Bucket                                          ol-bootcamps-app-production                                                                    import     [diff: -tagsAll~tags]; 1 warning`

This tells us that an attribute, in this case the tags that we're specifying the
S3 bucket should have in our Pulumi source disagrees with what's actually
sitting out there on EC2.

In order to fix this discrepancy, we go back to our ResourceOptions line we added,
adding the tags attribute into it to tell Pulumi to leave the current tags alone
and not complain that they differ:

`    opts=ResourceOptions(import_=bootcamps_storage_bucket_name,ignore_changes=["policy","tags"]),`

You'll also note that "policy" is in that attributes list. That's needed because
Pulumi signalled a mismatch on a previous run.

After these additions, your pulumi up should succeed and all the resources should be
created with no further warnings.

## Cleaning Up

After you've successfully built your environment with Pulumi and imported the
existing resources, you'll want to remove all those ResourceOptions lines
from your Pulumi model source as the import should only be done once.
