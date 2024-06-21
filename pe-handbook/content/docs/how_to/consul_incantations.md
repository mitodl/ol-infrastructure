
# Misbehaving consul cluster incident from 20231004-20231905

# Overview

At some point `operations-production` consul cluster fell over and stopped being useful. The root cause was not immediately know but was almost definitely related to an upgrade to `1.16.2` from `???` (no good record indication what the cluster was on before all this went down...)

Key takeaway from the logs was this cryptic message about being unable to restore snapshot:

```
{"@level":"error","@message":"failed to restore snapshot","@module":"agent.server.raft","@timestamp":"2023-10-04T14:17:40.200037Z","error":"failed to restore snapshot 1156-86945697-1696429059987: failed inserting acl token: missing value for index 'accessor'"}
```

This is something that happens anytime a consul server restarts, it gets a copy of the raft from the other servers currently running and restores it. But, it is failing to do that and crashing.

# Initial Response

Tobias was able to revive the cluster by downgrading it to 1.14.10 and it was then able to restore the snapshot it received from other nodes / on the filesystem (unclear how broken the cluster was at this time).

# Research

Looking up that message returned [one very-not-promising result](https://discuss.hashicorp.com/t/consul-accessorid-is-empty-in-one-of-remote-clusters/53191) from the hashicorp forums.

# Resolution

Ultimitely spent a lot of time reading and pursuing dead ends but what _I believe_ ulimitely resolved the issue was the following:

1. Step through each consul server in the cluster and ensure:
  a. it is on version 1.14.10
  b. It has this acl stanza in `00-default.json`: `"acl": {"enabled":  true, "default_policy": "allow", "enable_token_persistence": false}`
  c. Restart the servers one at a time to ensure the quorum never drops below 3 (or 2 if you're in non-prod).
2. Now you should be able to issue `acl` commands using the consul cli.
  a. They won't work though because you don't have a token and in this particular case the cluster says it is not elgible for bootstrapping.
  b. At some point in history this very special cluster had ACLs enabled and then disabled? Possibly?
3. We need to reset/recover the ACL master token. Follow the procedure [here](https://github.com/hashicorp/consul/issues/5331#issuecomment-462772868).
4. The output of that command should be "Bootstrap Token (Global Management)" an `AccessorID` and `SecretID`. Export the secret ID in your terminal as `CONSUL_HTTP_TOKEN`.
5. Then you can do `consul acl token list` and one of them should be labeled "Master Token".
  a. export the `SecretID` from the Master Token as `CONSUL_HTTP_TOKEN` or just save it off somewhere.
6. Repeat step 1, loop through all the servers and remove the `acl` stanza, restarting one at a time to ensure quorum.
7. Once all nodes are running again and ACL is disabled, start upgrading the nodes. one at a time, to 1.16.2.
```
wget https://releases.hashicorp.com/consul/1.16.2/consul_1.16.2_linux_amd64.zip
unzip consul_1.16.2_linux_amd64.zip
mv consul consul_1_16_2
systemctl stop consul
cp consul_1_16_2 /usr/local/bin/consul
cd /var/lib
cp -r consul consul_bak
cd consul
rm -rf serf/ raft/ server_metadata.json checkpoint-signature
systemctl start consul
```
8. Verify quorum: `consul operator raft list-peers`
9. Verify version: `consul members` and `consul version`
