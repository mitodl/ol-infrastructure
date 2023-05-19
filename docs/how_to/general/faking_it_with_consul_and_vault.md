# Faking it with Consul and Vault

## Why?

Sometimes you want to test an AMI build process without all all the supporting stuff that allows an EC2 instance to talk to vault and consul like a good little box. All that supporting stuff typically happens are instance start time and rides in on `user_data` which you don't have if you fired up an EC2 by hand just to see what your AMI looks like so far.

So, for development purposes here is an abbreviated guide of faking it until you make it.


## Consul

1. From a like-minded instance, find the proper IAM instance profile ARN. In my case I'm making a new type of EC2 box for edxapp / mite-staging / ci, so I went to one of the existing boxes for that environment, copied the Instance Profile ARN and associated it to my new, one-off, manually created EC2 box. This is essential.
2. Second, from the same like-minded instance, nab `/etc/consul.d/99-autojoin.json`.
```
{"retry_join": ["provider=aws tag_key=consul_env tag_value=mitx-staging-ci"], "datacenter": "mitx-staging-ci"}
```
3. Copy that config to your one-off box and restart consul and confirm you are now joined to the cluster with `consul catalog nodes` or whatever you fancy.

## Vault

This requires using vault >= 1.13.x which introduced the `token_file` auto_auth method.

1. On your one-off EC2 box, backup the existing `/etc/vault/vault.json` file and change the auto_auth block to be like the following:
```
  "auto_auth": {
    "method": {
      "type": "token_file",
      "config": {
        "token_file_path": "/etc/vault/vault_token"
      }
    },
    "sink": [
      {
        "type": "file",
        "derive_key": false,
        "config": [
          {
            "path": "/etc/vault/vault_agent_token"
          }
        ]
      }
    ]
  },
```

2. In the vault UI, top right side little man icon, click 'Copy Token' and put that into `/etc/vault/vault_token`
```
echo -n "<token>" > /etc/vault/vault_token
```
3. Restart vault and verify that it is happy and healthy.

## Disclaimer

You basically just gave this node a root token to vault ... so, you know ... don't do this anywhere besides CI and don't leave it hanging around. This is just for debugging.
