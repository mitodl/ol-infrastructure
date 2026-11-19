path "*" {
  capabilities = ["read", "list"]
}

path "secret-data/*" {
  capabilities = ["read", "list"]
}


path "sys/leases/renew" {
  capabilities = ["update"]
}

# Only the Hindsight forget job (applications/hindsight/forget_policy.hcl) may
# read these. The admin client can irreversibly forget any learner's memory.
path "secret-operations/sso/ol-memory-admin" {
  capabilities = ["deny"]
}

path "secret-operations/sso/ol-memory-forget-lookup" {
  capabilities = ["deny"]
}

# Operators read this to run OVS's assign_group_users. Its manage-users role
# can edit any user in the ol-mit realm.
path "secret-operations/sso/ovs-group-admin" {
  capabilities = ["deny"]
}
