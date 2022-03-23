### Basic Idea

Once a day we will copy the content from the real ocw s3 buckets to a backup bucket. In the event that something goes wrong with a release/mass publish we can update the fastly configs to point to the backup bucket and all will be magically well again.

### Runbook for DR of ocw-content

1. Confirm the last execution of the relevant `backup-ocw-content` job in concourse.
2. IMPORTANT: Pause that pipeline so that no further executions will take place (which would sync the bad content from the real bucket over the backup bucket...). To  feel double safe, you can run a destroy-pipeline `fly` command on it as well.
3. Go to fastly and << something >>
4. Verify  << something >>

### Runbook for un-DR of ocw-content

1. Go to fastly and << something >>
2. Unpause the backup pipeline or restore it with a `set-pipeline` command.
3. Verify << something >>


### set-pipeline

Example of how to set the pipeline for the CI environment.

```
fly -t ci-ocw sp -p backup-ocw-content -c backup-ocw-content.yml -l vars-backup-ci.yml
```
