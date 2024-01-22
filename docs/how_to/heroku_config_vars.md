
## Managing Heroku Config Vars with Pulumi

### heroku.app.ConfigAssociation

The resource/mechanism we are using to manage config vars in Heroku is called a 'ConfigAssociation' which is documented (here)[https://www.pulumi.com/registry/packages/heroku/api-docs/app/configassociation/]. A `ConfigAssociation` takes in an application ID and two sets of variable maps: `sensitive_vars` and `vars`. The only difference being that `sensitive_vars` will not be output during `up` operations.

### Four Flavors of Vars

While we don't yet have a component resource or abstraction available for setting up Config Vars in a simpler fashion, we do have a basic blueprint available with the MITOpen application.

#### Unchanging Values

These are not really variables because they represent Key:Value mappings that are unchanging between environments. That is, Production and QA will have the same value set for the same environment. These values are specified directly in the python code under `heroku_vars`

#### Simple Environment Specific Vars

These are simple 1-to-1 mappings from a value stored in the Pulumi configuration under `heroku_app:vars:`. This map contains the variable names, in their final forms using all-caps, and the static values that are applicable to the environment.

#### Interpolated Environment Specific Vars

These are key:value mappings that are used in more complicated manners than a simple 1-to-1 mapping as with the simple settings. These values are stored in the Pulumi configuration under `heroku_app:interpolated_vars:` in lower-case, signifying that they do not directly become Config Vars in Heroku. These more involved interpolations take place during the construction of the `heroku_interpolated_vars` dictionary.

#### Secrets

Many Config Vars that we use represent values that can be considered secret or otherwise sensitive and should not be publicly disclosed. Nothing from these vars is derived from values stored in the Pulumi configuration, rather they are obtained either from SOPS config or directly from vault at stack application time. Secrets are complicated to work with and it is best to reach out to DevOps for assistence in getting your new secret configuration var setup.
