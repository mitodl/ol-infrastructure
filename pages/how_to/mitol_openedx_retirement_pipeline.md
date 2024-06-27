# Setting Up The Retirement Pipeline For A New MIT OL OpenEdX Application

## Getting Set Up

First, follow the instructions in the [Setting Up User Retirement In The LMS](https://github.com/openedx/edx-documentation/blob/master/en_us/install_operations/source/configuration/user_retire/service_setup.rst) document.

You'll need to ssh into an edx-worker for the appropriate environment you're working in.

Run `sudo su - edxapp -s /bin/bash` and then `source edxapp_env` to get to where you need to be to run the Django manage application.

You can follow the doc verbatim with the exception that:
* The manage.py executable is located in the edx-platform folder, so you'd start your invocations with `python edx-platform/manage.py`
* Ignore the `--settings <your settings>` option as we already have our settings defined in the LMS configuration.

Be sure that the retirement tates, user and application creation invocations exit without throwing any fatal errors. ALL these scripts unfortunately
print a number of warnings, which can be ignored.

Here's what the output of each section should look approxiately like:

## Retirement States Creation
```
States have been synchronized. Differences:
   Added: {'COMPLETE', 'FORUMS_COMPLETE', 'ENROLLMENTS_COMPLETE', 'RETIRING_LMS_MISC', 'RETIRING_LMS', 'NOTES_COMPLETE', 'RETIRING_ENROLLMENTS', 'PENDING', 'RETIRING_NOTES', 'PROCTORING_COMPLETE', 'RETIRING_FORUMS', 'ABORTED', 'ERRORED', 'LMS_MISC_COMPLETE', 'RETIRING_PROCTORING', 'LMS_COMPLETE'}
   Removed: set()
   Remaining: set()
States updated successfully. Current states:
PENDING (step 1)
RETIRING_FORUMS (step 11)
FORUMS_COMPLETE (step 21)
RETIRING_ENROLLMENTS (step 31)
ENROLLMENTS_COMPLETE (step 41)
RETIRING_NOTES (step 51)
NOTES_COMPLETE (step 61)
RETIRING_PROCTORING (step 71)
PROCTORING_COMPLETE (step 81)
RETIRING_LMS_MISC (step 91)
LMS_MISC_COMPLETE (step 101)
RETIRING_LMS (step 111)
LMS_COMPLETE (step 121)
ERRORED (step 131)
ABORTED (step 141)
COMPLETE (step 151)
edxapp@ip-10-22-2-49:~$
```

## Retirement User Creation

```
Created new user: "retirement_service_worker"
Setting is_staff for user "retirement_service_worker" to "True"
Setting is_superuser for user "retirement_service_worker" to "True"
Adding user "retirement_service_worker" to groups []
Removing user "retirement_service_worker" from groups []
2023-04-28 21:10:00,691 INFO 69935 [common.djangoapps.student.models.user] [user None] [ip None] user.py:782 - Created new profile for user: retirement_service_worker
```

## Retirement DOT Application Creation
```
None] [ip None] create_dot_application.py:82 - Created retirement application with id: 10, client_id: XXXXXXXXXXXXXXXXXXXXXXXX, and client_secret: XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

You'll need to save this client_id and secret somewhere because you'll need to use it in the next step!

## We Keep Secrets In The Vault, Duh :)

Next, we'll need to make these secrets accessible to the pipeline so they can be consumed by the various retirement worker scripts.

In order to do that, we'll use a utility called [sops](https://github.com/mozilla/sops) to encrypt the secrets so we can safely store them in Github and conveyed to Vault.

Unfortunately setting yourself up to use sops is a bit of a process in and of itself and it outside the scope of this document.

Once you've got sops squared away, use it on the appropriate operational configuration file. Here's the one for [QA](https://github.com/mitodl/ol-infrastructure/blob/main/src/bridge/secrets/concourse/operations.qa.yaml).


So you'd invoke: `sops operations.qa.yml`. At that point, sops will decrypt the file and open your editor of choice with its contents.

Now, we want to add the necessary secrets we copied in the previous step, as well as the LMS host for this environment. You can find the
LMS hosts for the various environments listed in the [App Links](https://github.mit.edu/odl-engineering/project-status/wiki/App-Links) Wiki page.

If you're unsure, ask an old hand for help. Disambiguating the various environments can be tricky, and better safe than sorry!

Here's an example of what I added for mitxonline qa:

```
  mitxonline/tubular_oauth_client:
    id: CLIENTIDUNENCRYPTEDSECRETSAREFUNYESTHEYARE
    secret: EVENMORESLIGHTLYLONGERGIBBERISHTHATISYOURUNENCRYPTEDSECRET
    host: https://courses-qa.mitxonline.mit.edu
```

Once you've made your additions, save the file and quit your editor. sops will now do its magic and re-encrypt those values.

**BE SURE NOT TO COMMIT UNENDCRYPTED SECRETS TO GITHUB**.

Now create a pull request and get these changes merged. If this is for production, you'll also need to kick off [this pipeline](https://cicd.odl.mit.edu/teams/infrastructure/pipelines/packer-pulumi-concourse) manually.

At this point, your secrets should be in vault and available to the pipeline we're about to build.

## START THE RUBE GOLDBERG DEVICE! Building The Pipeline Itself

In order to get this done, you'll need to have the [Concourse](https://concourse-ci.org/) `fly` CLI command installed and an appropriate target for your
Concourse server and team defined.

I like to keep my target names reasonably short, so for the target I created for mitxonline QA, I ran:

`fly login --target mo-qa --team-name=mitxonline --concourse-url https://cicd-qa.odl.mit.edu`

Once we have a suitable target defined, we can use it to actually create our pipeline for real:

* From a checked out mitodl/ol-infrastructure repository, change directory to the `src/ol_concourse/pipelines/open_edx/tubular` folder.
* Run `poetry run python tubular.py` - This should print a reasonable looking blob of JSON to the screen with a fly command to run at the bottom.
* Go ahead and run that command! For example, given the mo-qa target I defined above, I ran: `fly -t mo-qa sp -p misc-cloud-tubular -c definition.json`.
  * At this point fly should show you the pipeline definition and will ask you if you want to actually make the change. Answer yes.
* Assuming there were no errors, your pipeline definition should be in place and we're ready to get the ball rolling!
* You can either use the fly unpause-pipeline or else, in the Concourse web UI, click the little Play icon at the very top right of the screen. <!-- TODO add screenshot -->
* The build is set to run once a week, so once you unpause the pipeline, you may noeed to click the + icon in order to actually kick off a build.

That's it! Obviously keep an eye out on the pipeline for any failures.
