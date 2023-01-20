# Managing Datasources Automatically

## Why

We use dynamic credentials for many of our RDS instances. This means that they are issued from maria/postgres/whatever for a fixed period time via the Vault instance for that environment. When the credential expire, they need to be reissued by this process. Initially we were configuring datasources in redash via the UI with a workflow something like this:

1. Create a datasource in Redash UI.
2. Create a database user+password from the vault UI.
3. Copy the user+password into the datasource config in the redash UI.
4. Wait for a query or dashboard to break to know that a credential has expired.
5. Go to step 2.

This is tedious, error prone, and depends on user-centric monitoring.

## How
We can automate this, because most things can be automated. There are some key files needed.

### Important Files

1. `src/bilder/images/redash/files/datasources.yaml.tmpl`
  a. This is a `consul-template` template that represents a list of datasource definitions as required by Redash.
  b. The file is divided into two sections, one for QA and one for Production. Presumably if you had a dsource for both, you could define it outside of the if-else blocks.
  c. Each datasource definition follows the exact same structure that is output by the `manage.py ds list` command for Redash. Three parts, `name`, `type`, and `options`. The contents of `options` varies with the `type`.
2. `src/bilder/images/redash/files/update_datasources.sh`
  a. This is a script that `consul-template` will execute any time there is a change to the rendered `datasources.yaml` file. This takes the rendered yaml file and executes `manage.py` commands to update the definitions in Redash.
3. `src/ol-infrastructure/applications/redash/__main__.py`
  a. This is the pulumi stack code for a redash environment. Specifically, there is a conditional check `if redash_config.get_bool("manage_datasources"):` which contains a block of code to execute if datasource management is enabled for an environment. Additionally, that configuration flag must be set to True in the pulumi stack configuration for the environment you're working with...
  b. Within the environment appropriate block of nested in this conditional check, you will need to ensure that any `consul-template` keys you have referenced are created and any additional vault secrets are created as needed.
4. `src/ol-infrastructure/applications/redash/redash_server_policy.hcl`
  a. This is the vault policy definition for a redash server. You will need to have entries to match any secrets you reference in your template defined here as well otherwise the vault reads will fail.

### Walkthrough

Typically it is going to be easier to manage a datasource that has already be defined once in the UI and that is what we will focus on here.

1. The easeist thing is to execute a manage.py command to list the existing datasources. Something like `manage.py ds list`.
2. Using that information, create an entry in `datasources.yaml.tmpl` for your datasource.
  a. The `name` NEEDS TO MATCH EXACTLY. That means uppercase/lowercase/white spaces/special characters. Everything.
  b. You cannot change the the `type` on an existing datasource, so that needs to match exactly as well.
  c. `options` is where you will have the bulk of your contents.
  d. Generally you can take the exact json that `manage.py ds list` gave you, convert it to yaml, and use it for `options' as is. It goes without saying that the field names, again, must match exactly.
  e. For the username and password, you want to add template calls to the appropriate vault mount for this datasource.
  f. You can't get a database host name from vault when requesting credentials, so you will want to populate this with a template call to a consul key. Try to follow the preexisting path pattern.
3. Next, you need to edit the datasource management block in `ol-infrastructure/applications/redash/__main__.py` to include any `StackReferences` you may need for obtaining hostnames which you then put into consul at the path from above.
4.And finally, you need to update `redash_server_policy.hcl` to allow the redash server to access any vault new vault mounts you have added to the template.
