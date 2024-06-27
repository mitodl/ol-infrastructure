# So you broke vault?

## Symptoms

- The vault UI or CLI reports that the cluster is sealed and there is no amount of coaxing to get it out of that state.
- There may be other symptoms. Add to this list as needed.

```
root@ip-172-16-0-82:/etc/vault# VAULT_SKIP_VERIFY=true vault status
Key                      Value
---                      -----
Recovery Seal Type       awskms
Initialized              false                 <<<<< Ahh! Bad!
Sealed                   true                  <<<<< Sealed!
Total Recovery Shares    0
Threshold                0
Unseal Progress          0/0                   <<<<< This node knowns nothing about any other nodes
Unseal Nonce             n/a
Version                  1.14.1
Build Date               2023-07-21T10:15:14Z
Storage Type             raft
HA Enabled               true                  <<<<< And it is supposed too!
```


## What to do!?!

1. Go to the apporpriate S3 bucket and find the most recent backup. Production backs up every six hours. QA + CI backup once a day. Bucket names are: **ol-infra-ci-vault-backups**, **ol-infra-qa-vault-backups**, **ol-infra-production-vault-backups**.
2. Download the most recent backup locally to your machine and stage it to the vault node you're going to run the restore on. **You only run this procedure ONCE! You do not need to run this on every vault node.**
```
scp -i <path to your oldevops.pem ssh private key> <path to downloaded .snapshot file> admin@<IP address of the vault node you're restoring to>:/tmp
```
3. On the node that you've copied the `.snapshot` file to, verify that the vault status outputs as above.
4. Export a vault setting to make life less annoying `export VAULT_SKIP_VERIFY=true`
5. Initialize the vault cluster: ` vault operator init`
6. Output will look like this:
```
Recovery Key 1: bX5A***********************************ExhBC
Recovery Key 2: XHo3***********************************LZxmZ
Recovery Key 3: EEOE***********************************XWC8p
Recovery Key 4: FyTq***********************************EY0ij
Recovery Key 5: oXaW***********************************Wr74k

Initial Root Token: hvs.**********************wf

Success! Vault is initialized

Recovery key initialized with 5 key shares and a key threshold of 3. Please
securely distribute the key shares printed above.
```
7. The message says those recovery keys are important but they aren't in this case. You don't need to save them. What you do need is the initial root token. Export that into env var: `export VAULT_TOKEN=<token value including hvs. from prev command output>`
8. Do the restore: `vault operator raft snapshot restore <path to .snapshot file>`. It doesn't actually output anything.
9. Unset VAULT_TOKEN with `unset VAULT_TOKEN` (because that token was tied the shortlived cluster that existed before running the restore). Then do a vault status: `vault status` and it should look something like this:
```
Key                      Value
---                      -----
Recovery Seal Type       shamir
Initialized              true
Sealed                   false                                       <<<< Victory!
Total Recovery Shares    2
Threshold                2
Version                  1.14.1
Build Date               2023-07-21T10:15:14Z
Storage Type             raft
Cluster Name             vault-cluster-ab3e7a1b
Cluster ID               ef172e45-fake-uuid-here-aeb8da8a7179
HA Enabled               true
HA Cluster               https://256.256.256.256:8201
HA Mode                  standby
Active Node Address      https://active.vault.service.consul:8200
Raft Committed Index     815551
Raft Applied Index       815551
```
10. Loop through the other nodes in the cluster and verify they have a similar vault unseal status. If they don't, try `systemctl restart vault`. They *should* join backup and restore the raft on their own provided they were broken to begin with.
11. Verify that the vault UI works as excpected again and your secrets are there.
12. **Some, possibly serious, complications after doing this:** it is possible that there are credential secrets out in the wild being used that are no longer tracked via leases in vault. For instance if they were issued between when the backup took place and when the cluster stopped being viable. This could be the case for PKI secrets as well and of course if any static secrets in vaults were changed between the backup and beginning of the outage.
