# Initalize a new stack with a DB dump from an existing Redash application

## Background

bi.odl.mit.edu : old redash application on v8.
redash-qa.odl.mit.edu : new QA environment, initialized with data from bi.odl.mit.edu.
redash-production.odl.mit.edu : new Production environment , initalized with data from bi.odl.mit.edu.

## Steps to recreate

1. Take a backup of the old environment.
```
# The schema
pg_dump -h <old RDS endpoint> -p 5432 -d redash -U odldevops -Z9 -Fc -s > schema.redash.dmp
# The data
pg_dump -h <old RBS endpoint> -p 5432 -d redash -U odldevops -Z9 -Fc -a --file=data.redash.dump
```

Possible problems:
- Not enough memory.
  - Either `work_mem` is too low, or the instance just is not big enough. Replace the instance with a larger one in that case.
- Connectivity.
  - You need to run the dump from a place where you can talk to both the old RDS instance AND the new RDS instance. This may mean creating new peering connections and route table entries.

2. Prepare new RDS instance
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
3. Nice, you're doing amazing. Now we need to restore the old data and schema to the new rds instance.
```
pg_restore -h <new RDS endpoint> -d redash -U oldevops -C -Fc schema.redash.dmp
pg_restore -h <new RDS endpoint> -d redash -U oldevops -a  -Fc data.redash.dump
```
4. Finally, we need to run the database upgrade (because the old instance was at v8 and the new instance is at v10...) Run this from a web or worker node from the new stack. It will look something like this:
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
