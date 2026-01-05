If you're building a public cluster you will need to run the `bootstrap_security_config.py` script to initialize the basic auth users that the apps will need. Use the same poetry environment you used to run pulumi, so something like:

```
poetry run python3 ./bootstrap_security_config.py -s <same stack name as the pulumi call>
```

It is dirty, but it gets the job done. The script is idempotent and you can run it mutliple times if need-be. It is useful for resetting the passwords of the basic auth users if that is needed.

It expects to find the passwords in the sops file for environment. So, same place you defined the `master_user_password` in order to build the environment.

If you need an older cluster ( < ES 7.X ), use the `bootstrap_security_config_6.8.py` script instead. Some of the API interfaces are different.

### ML Stuff for Anastasia

On 2025-11-18 Anastasia requested ML capabilities be added to the cluster. This requires some additional configuration. We turned on one setting by hand in the cluster:

```
PUT _cluster/settings
{
  "persistent": {
    "plugins.ml_commons.only_run_on_ml_node": false
  }
}
```
She said that this was not needed for production clusters, only in RC. Documented here incase that isn't true.

### ML Stuff for Anastasia v2

Important links:
- https://docs.opensearch.org/latest/tutorials/vector-search/semantic-search/semantic-search-openai/
- https://docs.aws.amazon.com/opensearch-service/latest/developerguide/ml-external-connector.html

To enable the ML features for Anastasia + hybrid search we have to do the steps outlined in the above links. For `mitlearn` clusters, this is largely automated except for one click-ops step that must take place which is step 3.2 from the first link: https://docs.opensearch.org/latest/tutorials/vector-search/semantic-search/semantic-search-openai/#step-32-map-a-backend-role

For this you will use the arn output in by the stack under the key `connector_management_role_arn`. Once that is done and the stack is up, you can run the `configure_openai_connector.py` script to finish the setup.
