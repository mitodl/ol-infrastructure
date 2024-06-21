

# xPro Outage - October 30, 2023

## Timeline

Undetermined date within the past 6 months:

- AWS RDS config updated to default new credentials to use SCRAM rather than md5

Monday, October 30th 2023
- 3:45PM - Configuration update deployed xPro Production Heroku stack via Salt.
- 3:51PM - [Pingdom alerts DevOps on-call](https://mitodl.app.opsgenie.com/alert/detail/909993c0-72e7-4433-9e9e-30a2196e8f87-1698695490940/details).
- 3:51PM - Alert Acknowledged by DevOps on-call (Mike Davidson).
- 3:52PM - Verified that site was non-responsive.
- 3:52PM - Begin detailed investigation.
- ~3:53PM - Confirm log messages in Heroku regarding failed database logins:
```
LOG C-0x55cb586d2540: db1/v-aws-mitxpro-OC7crfPS2rL3jSryzvHE-1698696728@127.0.0.1:55038 login attempt: db=db1 user=v-aws-mitxpro-OC7crfPS2rL3jSryzvHE-1698696728 tls=no
ERROR S-0x55cb586d5580: db1/v-aws-mitxpro-OC7crfPS2rL3jSryzvHE-1698696728@3.209.36.28:5432 cannot do SCRAM authentication: wrong password type
LOG C-0x55cb586d2e00: db1/v-aws-mitxpro-OC7crfPS2rL3jSryzvHE-1698696728@127.0.0.1:52474 closing because: server login failed: wrong password type (age=14s)
```
- ~3:55PM Determined this matches the fingerprint of a previously encountered issue seen when setting up the new MITOpen environments.
- 3:56PM Found [previous PR](https://github.com/mitodl/ol-infrastructure/pull/1703) that addressed this for MITOpen:
- 3:56PM Started looking for code location for make the above modification.
- 3:58PM Tobias finds [a possible alternative resolution](https://github.com/pgbouncer/pgbouncer/issues/787#issuecomment-1374848819).
- 4:00PM Unable to locate code for this environment. Determined it is not managed by pulumi.
- 4:03PM Started a zoom call. Attendees: Mike, Tobias, Sar
- 4:05PM Decided making the database parameter group configuration will possibly take longer than is desirable. Opt for trying the alternative resolution.
- ~4:07PM Role definition in vault is modified and verified to still generate credentials via the Vault UI.
- 4:09PM Cached credentials are cleared and Heroku configuration is re-applied via Salt.
- ~4:10PM Verified the issue is not resolved.
- 4:12PM Cleared cached credentials again and Heroku configuration is re-applied via Salt.
- 4:14PM Verified the issue is not resolved and that the credentials applied via Salt are truely different.
- 4:15PM Decided alternative solution did not work. Begin implementing database parameter group fix known to work.
- ~4:20PM A new database parameter group is created and applied to the running database.
- ~4:23PM The database is listed as Available in the RDS web console.
- 4:24PM Cleared cached credentials again and Heroku configuration is re-applied via Salt.
- 4:25PM Verified the site is now available again and login/other database interactions work.
- 4:25PM Pingdom automatically closes the OpsGenie alert.

## Root Cause:

At some point in the last 6 months, the default behavior of the underlying RDS instance for xPro production was transitioned to using SCRAM password authentication rather than MD5 password authentication. Further information about this is documented [here](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Roles.html). The [buildpack](https://github.com/heroku/heroku-buildpack-pgbouncer/issues/155) we use in Heroku for pgbouncer does not support SCRAM.

When managing configuration items for applications deployed in Heroku, we invoke Vault via SaltStack to generate database credentials. These credentials are then cached on the Salt Master. Vault tracks the credentials it generates via internal objects known as 'leases'. When Salt checked + applied the configuration at 3:45PM, it saw that the lease was due to expire soon and was not eligible for renewal. This triggered Vault generating new credentials, however these new credentials were generated with the new default SCRAM configuration rather than the MD5 configuration supported by pgbouncer.

When attempting to establish a pool of database connections, pgbouncer was rejected by the RDS instance for sending the wrong password type. It sent an MD5 password hash while the database expected SCRAM for the newly generated credential. Being unable to establish a connection to the database is a fatal error for the application and it was not able to finish starting up and serving requests.

## Action Items

- [Migrate management of XPro resources into Pulumi](https://github.com/mitodl/ol-infrastructure/issues/1886)
  - One of the key factors delaying the resolution of the outage was the fact that none, or nearly none, of the configuration for this stack was managed as code. In particular the RDS instance and its associated resources. Additionally, the RDS instance was using an instance of the default parameter group provided by AWS, which cannot be modified. As such, we were required to duplicate this default parameter group definitions, make the required modifications, and then associate the newly created group with the instance. This is a more time consuming task than modifying a parameter configuration on an already associated parameter group. All of this would have been mitigated if the resources were managed with pulumi, where we do not utilize AWS sourced default parameter groups.
