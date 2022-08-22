# Initalize a new stack with a DB dump from an existing Redash application

- [ ] Deploy the latest AMI and other pulumi changes to Production. As of right now this is still configured as `redash.odl.mit.edu`.
```
poetry run pulumi up --refresh -C src/ol_infrastructure/applications/redash -s applications.redash.Production
```
- [ ] Prepare the production database by dropping everything currently there. Password found in pulumi config. Should be able to run this from the old redash instance. Peering and network config should be in place.
```
poetry run pulumi config --show-secrets -C src/ol_infrastructure/applications/redash -s applications.redash.Production
```
```
psql -h <new RDS endpoint>  -p 5432 -d postgres -U oldevops
# if the database has already been created by the app startup, we need to drop it
# Kill all the active sessions to DB 'redash'
select pg_terminate_backend(pid) from pg_stat_activity where datname='redash';
# Drop the database
drop database "redash";
# Make a new one
create database "redash";
```
- [ ] Take a backup of the current redash environment. Password should be in vault from, admin role under `postgres-operations-redash`. Should be able to run this from the old redash instance. Peering and network config should be in place.
```
# The schema
pg_dump -h <old RDS endpoint> -p 5432 -d redash -U odldevops -Z1 -Fc -s > schema.redash.dmp
# The data
pg_dump -h <old RBS endpoint> -p 5432 -d redash -U odldevops -Z1 -Fc -a --file=data.redash.dump
```
- [ ] Restore the back up to the new redash production environment. Same password from when you prepped the db earlier. Should be able to run this from the old redash instance. Peering and network config should be in place.
```
pg_restore -h <new RDS endpoint> -d redash -U oldevops -C -Fc schema.redash.dmp
pg_restore -h <new RDS endpoint> -d redash -U oldevops -a  -Fc data.redash.dump
```
- [ ] Run the database upgrade (because the old instance was at v8 and the new instance is at v10...). Run this from a web or worker node from the new stack. It will look something like this:
```
root@ip-10-3-0-182:/etc/docker/compose# docker-compose run --rm server manage db upgrade
Creating compose_server_run ... done
[2022-07-26 17:04:54,652][PID:1][INFO][alembic.runtime.migration] Context impl PostgresqlImpl.
[2022-07-26 17:04:54,653][PID:1][INFO][alembic.runtime.migration] Will assume transactional DDL.
[2022-07-26 17:04:54,671][PID:1][INFO][alembic.runtime.migration] Running upgrade e5c7a4e2df4d -> d7d747033183, encrypt alert destinations
[2022-07-26 17:04:54,692][PID:1][INFO][alembic.runtime.migration] Running upgrade e5c7a4e2df4d -> 0ec979123ba4, empty message
[2022-07-26 17:04:54,696][PID:1][INFO][alembic.runtime.migration] Running upgrade 0ec979123ba4, d7d747033183 -> 89bc7873a3e0, fix_multiple_heads
[2022-07-26 17:04:54,698][PID:1][INFO][alembic.runtime.migration] Running upgrade 89bc7873a3e0 -> fd4fc850d7ea, Convert user details to jsonb and move user profile image url into details column
```
- [ ] Verify the new site at redash.odl.mit.edu. SSO will NOT work, use a password login.
- [ ] Remove Route-53 entry for `bi.odl.mit.edu`.
- [ ] Redeploy the stack after changing the url in the pulumi config to `bi.odl.mit.edu`.
- [ ] Verify taht SSO works after updating the URL
- [ ] Turn off password login in SSO works.
