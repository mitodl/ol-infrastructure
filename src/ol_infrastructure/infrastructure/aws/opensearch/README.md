If you're building a public cluster you will need to run the `bootstrap_security_config.py` script to initialize the basic auth users that the apps will need. Use the same poetry environment you used to run pulumi, so something like:

```
poetry run python3 ./bootstrap_security_config.py -s <same stack name as the pulumi call>
```

It is dirty, but it gets the job done. The script is idempotent and you can run it mutliple times if need-be. It is useful for resetting the passwords of the basic auth users if that is needed.

It expects to find the passwords in the sops file for environment. So, same place you defined the `master_user_password` in order to build the environment.
