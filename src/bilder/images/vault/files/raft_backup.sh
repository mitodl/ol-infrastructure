#!/bin/bash

export VAULT_SKIP_VERIFY="true"

raft_backup_dir='/tmp/backup/raft'
current_time=$(date "+%Y.%m.%d-%H.%M.%S")

VAULT_BIN="/usr/local/bin/vault"
AWS_BIN="/usr/bin/aws"

log_message() {
  logger -t 'raft_backup' "$1"
}

clean_up() {
  log_message "cleaning up."
  rm -rf "$raft_backup_dir/$current_time.snapshot"
}

if [ "$($VAULT_BIN status --format json | jq --raw-output 'select(.is_self==true) | .is_self')" = "true" ]; then
  if [ ! -d $raft_backup_dir ]; then
    mkdir -p $raft_backup_dir
  fi

  if [ -z "$BUCKET_NAME" ]; then
    log_message "ERROR. Bucket name not provided."
    exit 1
  fi
  if [ -z "$RAFT_BACKUP_USERNAME" ]; then
    log_message "ERROR. Raft Backup username not specifed."
    exit 1
  fi
  if [ -z "$RAFT_BACKUP_PASSWORD" ]; then
    log_message "ERROR. Raft Backup password not specifed."
    exit 1
  fi

  # Login to vault, get a token, adjust token lifetime.
  log_message "Requesting vault token."
  VAULT_TOKEN=$($VAULT_BIN login --method userpass --path pulumi -no-store -token-only username="$RAFT_BACKUP_USERNAME" password="$RAFT_BACKUP_PASSWORD")
  if [ -z "$VAULT_TOKEN" ]; then
    log_message "ERROR. Failed to obtain vault token."
    exit 1
  fi
  export VAULT_TOKEN
  log_message "Vault token received."

  log_message "Adjusting token lifetime to 5 minutes."
  if ! $VAULT_BIN token renew -increment 5m; then
    log_message "ERROR. Could not update token lifetime."
    clean_up
    exit 1
  fi
  log_message "Token lifetime adjusted."

  # Create the snapshot
  log_message "Making raft snapshot $raft_backup_dir/$current_time"
  if ! $VAULT_BIN operator raft snapshot save "$raft_backup_dir/$current_time.snapshot"; then
    log_message "ERROR. Could not take raft snapshot."
    clean_up
    exit 1
  fi
  log_message "raft snapshot $raft_backup_dir/$current_time.snapshot complete"

  # Copy to S3
  if ! $AWS_BIN s3 cp --quiet --no-progress  "$raft_backup_dir/$current_time.snapshot" "s3://$BUCKET_NAME/"; then
    log_message "ERROR. Could not upload snapshot to s3."
    clean_up
    exit 1
  fi
  log_message "snapshot $current_time.snapshot uploaded to s3 successfully."
  clean_up
  log_message "Raft snapshot successful and uploaded to s3. Exiting."
  exit 0
else
  log_message "Node not leader, not making raft snapshot."
  exit 0
fi
