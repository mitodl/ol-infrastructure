### Basic Idea

Once a day we will copy the content from the real ocw s3 buckets to a backup bucket. In the event that something goes wrong with a release/mass publish we can update the fastly configs to point to the backup bucket and all will be magically well again.

### Determining when to Break-Glass

This type of rollback / failover should only happen if there is a large scale issue, for example a mass publish that damaged more than 10 courses. Otherwise, it would be faster and safer to revert individual courses.

This descision should be made by at least one one representative from product and one representative from engineering.

### Runbook for DR of ocw-content

- Confirm the last execution of the relevant `backup-ocw-content` job in concourse.
- IMPORTANT: Pause that pipeline so that no further executions will take place (which would sync the bad content from the real bucket over the backup bucket...). To  feel double safe, you can run a destroy-pipeline `fly` command on it as well.
- Go to fastly.
  - Navigate to the relevant service.
  - Clone the active configuration.
  - In your new clone, under `origins` and `hosts`, update references to `ocw-content-<draft|live>-<env>` to `ocw-content-backup-<draft|live>-<env>`.
  - Additionally, be sure to check `override-host`, `SNI Host`, and `Certificate Host` sections for any values that need to be updated in the same manner.
  - Activate the new configuration.
- Verify by navigating to the appropriate OCW instance and verify that courses and resources are all working as expected. Try many different courses and resources.

### Runbook for un-DR of ocw-content

- Go to fastly
  - Navigate to the relevant service.
  - Clone the active configuration.
  - In your new clone, under `origins` and `hosts`, update references to `ocw-content-backup-<draft|live>-<env>` to `ocw-content-<draft|live>-<env>`.
  - Additionally, be sure to check `override-host`, `SNI Host`, and `Certificate Host` sections for any values that need to be updated in the same manner.
- Unpause the backup pipeline or restore it with a `set-pipeline` command.
- Verify by navigating to the appropriate OCW instance and verify that courses and resources are all working as expected. Try many different courses and resources.

### set-pipeline

Example of how to set the pipeline for the CI environment.

```
fly -t ci-ocw sp -p backup-ocw-content -c backup-ocw-content.yml -l vars-backup-ci.yml
```
