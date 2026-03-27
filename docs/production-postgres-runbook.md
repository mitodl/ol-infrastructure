# Production PostgreSQL Runbook

This runbook covers operational procedures for managing production PostgreSQL
databases (RDS instances). It includes queries and procedures for diagnosing
and recovering from incidents involving long-running queries, disk exhaustion,
and performance degradation.

> **Context:** This runbook was created as a direct action item from the
> [2026-03-24 MIT Learn Outage Post Mortem](https://pe.ol.mit.edu/runbooks_post_mortems/20260324_mitlearn_outage/).
> The incident was caused by a query pattern change that caused PostgreSQL to
> fill up storage with temporary files, resulting in a 3-hour outage.

---

## Table of Contents

1. [Connecting to the Database](#connecting-to-the-database)
2. [Detecting Long-Running Queries](#detecting-long-running-queries)
3. [Selectively Terminating Queries](#selectively-terminating-queries)
4. [Bulk Terminating All Long-Running Queries](#bulk-terminating-all-long-running-queries)
5. [Monitoring Disk and I/O Health](#monitoring-disk-and-io-health)
6. [Identifying Temp File Usage](#identifying-temp-file-usage)
7. [Recovering from Disk Exhaustion](#recovering-from-disk-exhaustion)
8. [Useful CloudWatch Alarms](#useful-cloudwatch-alarms)

---

## Connecting to the Database

Production database connection details are stored in Vault. Use dynamic
credentials (short-lived, auto-rotated) rather than long-lived admin passwords.

```bash
# Get Vault token first (if not already authenticated)
vault login -method=aws

# Get dynamic credentials for the MIT Learn production DB
vault read database/creds/postgres-mitlearn-admin
```

Connection string format (use environment variables to avoid credentials in shell history):
```bash
# Preferred: use separate psql parameters to avoid credentials in history
export PGPASSWORD="<password>"
psql -h ol-mitlearn-db-production.cbnm7ajau6mi.us-east-1.rds.amazonaws.com \
     -p 5432 -U <username> -d mitopen
unset PGPASSWORD
```

Alternatively, use a `.pgpass` file:
```
ol-mitlearn-db-production.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432:mitopen:<username>:<password>
```
Set file permissions: `chmod 600 ~/.pgpass`

---

## Detecting Long-Running Queries

Use this query to list all currently active long-running queries. Review before
deciding to terminate any.

```sql
SELECT
    pid,
    now() - query_start AS duration,
    state,
    wait_event_type,
    wait_event,
    left(query, 200) AS query_preview
FROM pg_stat_activity
WHERE state = 'active'
  AND now() - query_start > interval '5 minutes'
ORDER BY duration DESC;
```

For more detail including backend type and application name:

```sql
SELECT
    pid,
    usename,
    application_name,
    client_addr,
    now() - query_start AS duration,
    state,
    wait_event_type,
    wait_event,
    query
FROM pg_stat_activity
WHERE state = 'active'
  AND now() - query_start > interval '5 minutes'
ORDER BY duration DESC;
```

To check for queries that are waiting (blocked by locks):

```sql
SELECT
    pid,
    now() - query_start AS duration,
    state,
    wait_event_type,
    wait_event,
    left(query, 200) AS query_preview
FROM pg_stat_activity
WHERE wait_event IS NOT NULL
  AND state != 'idle'
ORDER BY duration DESC;
```

---

## Selectively Terminating Queries

After reviewing the list of long-running queries, terminate specific ones by PID.
`pg_cancel_backend` sends a graceful interrupt (like Ctrl+C); `pg_terminate_backend`
forcefully ends the connection.

Try cancellation first:
```sql
-- Graceful cancel (preferred)
SELECT pg_cancel_backend(<pid>);
```

Force termination if cancel doesn't work within 30 seconds:
```sql
-- Force terminate
SELECT pg_terminate_backend(<pid>);
```

To confirm the query was terminated:
```sql
SELECT pid, state, query_start FROM pg_stat_activity WHERE pid = <pid>;
-- Should return no rows if terminated successfully
```

---

## Bulk Terminating All Long-Running Queries

> **⚠️ WARNING:** Only use bulk termination during an active incident when
> long-running queries are causing storage exhaustion or I/O thrash. This will
> abruptly end all matching sessions.

Terminate all active queries running longer than 10 minutes (excluding the
current session):

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'active'
  AND now() - query_start > interval '10 minutes'
  AND pid <> pg_backend_pid();
```

Dry-run first (shows which PIDs would be terminated without actually doing it):
```sql
SELECT pid, now() - query_start AS duration, left(query, 150) AS query_preview
FROM pg_stat_activity
WHERE state = 'active'
  AND now() - query_start > interval '10 minutes'
  AND pid <> pg_backend_pid()
ORDER BY duration DESC;
```

---

## Monitoring Disk and I/O Health

### Check temporary file usage (current sessions)

```sql
SELECT
    pid,
    usename,
    now() - query_start AS duration,
    temp_files,
    temp_bytes,
    left(query, 150) AS query_preview
FROM pg_stat_activity
WHERE state = 'active'
ORDER BY temp_bytes DESC NULLS LAST
LIMIT 20;
```

### Check cumulative temp file usage by query plan

```sql
SELECT
    query,
    calls,
    total_exec_time / 1000 AS total_exec_seconds,
    temp_blks_written,
    temp_blks_read
FROM pg_stat_statements
WHERE temp_blks_written > 0
ORDER BY temp_blks_written DESC
LIMIT 20;
```

> Note: `pg_stat_statements` must be enabled. Check with:
> `SELECT * FROM pg_extension WHERE extname = 'pg_stat_statements';`

### Check database size and table sizes

```sql
-- Overall database size
SELECT pg_size_pretty(pg_database_size(current_database()));

-- Largest tables
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname || '.' || tablename)) AS total_size,
    pg_size_pretty(pg_relation_size(schemaname || '.' || tablename)) AS table_size,
    pg_size_pretty(pg_indexes_size(schemaname || '.' || tablename)) AS index_size
FROM pg_tables
ORDER BY pg_total_relation_size(schemaname || '.' || tablename) DESC
LIMIT 20;
```

---

## Identifying Temp File Usage

Temp files are created when PostgreSQL cannot fit sort/hash operations in
`work_mem`. Large temp file usage is a key signal of potentially problematic
queries.

Enable temp file logging (requires RDS parameter group change):
```
log_temp_files = 1024  # log temp files larger than 1 MB
```

Check current temp file parameter settings:
```sql
SHOW log_temp_files;
SHOW work_mem;
SHOW temp_file_limit;
```

Use `EXPLAIN ANALYZE` to review query plans:
```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) <YOUR_QUERY_HERE>;
```

Look for:
- `Sort Method: external merge` → using temp files for sorting
- `Hash Batches: N > 1` → using temp files for hash joins
- High `Temp Blks Written` values

---

## Recovering from Disk Exhaustion

If the database has run out of disk space:

1. **Check CloudWatch Metrics** - Review `FreeStorageSpace` and `DiskQueueDepth`
   in the AWS Console for the RDS instance.

2. **Identify and kill long-running queries** - Use the bulk termination query
   above to free up I/O and allow the database to process its backlog.

3. **Monitor recovery** - After terminating queries, watch the `DiskQueueDepth`
   metric drop and `FreeStorageSpace` recover as temp files are cleaned up.

4. **Contact AWS Support if needed** - If the database is completely unresponsive,
   open a support case. AWS may be able to help complete a pending backup to free
   storage. Reference the [2026-03-24 MIT Learn Outage AWS Support Case](https://support.console.aws.amazon.com/support/home)
   for precedent.

5. **Roll back the offending application change** - If a recent deploy caused
   the issue, coordinate with the application team to roll back. Review
   [Rollback Steps](https://pe.ol.mit.edu) for documented procedures.

---

## Useful CloudWatch Alarms

The following alarms are configured for production RDS instances in this
infrastructure (via `OLAmazonDB` with the production monitoring profile):

| Alarm | Threshold | Level | Purpose |
|-------|-----------|-------|---------|
| `FreeStorageSpace` | < 15 GB | warning | Early warning for disk space |
| `FreeStorageSpace_critical` | < 5 GB | critical | Urgent disk space alert |
| `DiskQueueDepth` | > 64 (sustained 30 min) | warning | I/O saturation signal |
| `EBSIOBalance` | < 75% | warning | EBS burst bucket depletion |
| `CPUUtilization` | > 90% (30 min) | warning | Extended high CPU |
| `WriteLatency` | > 100ms (30 min) | warning | Slow write operations |
| `ReadLatency` | > 20ms (10 min) | warning | Slow read operations |

To view these alarms in AWS Console:
```
CloudWatch → Alarms → All Alarms → filter by "ol-mitlearn-db-production"
```
