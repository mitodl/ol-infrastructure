# Initalize a new stack with a DB dump from an existing OVS application

## Steps to recreate

1. Take a backup of the old environment.
```
pg_dump -d odlvideo -h <old RDS endpoint> -p 5432 -U <'admin' user from vault auth endpoint> --create -Fc -f /tmp/dump.db
```
Then `scp` it somewhere you can access the new RDS instance, if applicable.

2. Prepare new RDS instance, maybe only partially needed.
```
psql -h <new RDS endpoint>  -p 5432 -d postgres -U oldevops
# if the database has already been created by the app startup, we need to drop it
# Kill all the active sessions to DB 'odlvideo'
select pg_terminate_backend(pid) from pg_stat_activity where datname='odlvideo';
# Drop the database
ALTER DATABASE odlvideo owner TO oldevops;
drop database "odlvideo";
# Make a new one
create database "odlvideo";
```

3. Now we need to restore the old data and schema to the new rds instance. The password is in the Pulumi config for the environment.
```
pg_restore -h <new RDS enpoint> -U oldevops  -C -d odlvideo < /tmp/dump.db
```
