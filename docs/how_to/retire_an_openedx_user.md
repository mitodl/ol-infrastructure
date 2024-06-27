# How To Retire An MIT Online Learning OpenEdX User

## Pre-Requisites

- You'll need the email address the user registered under. e.g. cpatti@mit.edu
- You'll need the pre-requisites defined in [How To Access An MIT OL OpenEdX Django Admin manage.py](https://github.com/mitodl/ol-infrastructure/blob/main/docs/how_to/access_openedx_djange_manage.md).


## Look Up This User's username

You'll need to have Django admin and superuser access to the product you're looking to retire users for.

Anyone who has the requisite access already can help. pdpinch@ and the Devops team are good folks to ask.

Next, you'll need to login to the admin web UI for the application we'll be working with. You can find that
URL from the [Application Links Page](https://github.mit.edu/odl-engineering/project-status/wiki/App-Links).

Find the product in question (e.g. mitxonline) and choose the LMS url for the environment you want (e.g. production) as a base.
Then add /admin on to the end. Ensure there's only one slash before /admin or things will go awry.

In our case, since we are looking to retire users from mitxonline, we'll use https://courses.mitxonline.mit.edu/admin

Find the Users link on that page, click it. Now type the email address into the search box and click Search.

This should yield the user's username. Copy that off into a safe place as we'll need it for the next section.

## Get Yourself Connected

First, follow the step by step instructions defined in [How To Access An MIT OL OpenEdX Django Admin manage.py](https://github.com/mitodl/ol-infrastructure/blob/main/docs/how_to/access_openedx_djange_manage.md).

This will land you at a shell prompt of the OpenEdX worker for the product in question.

It should look something like:

`ubuntu@ip-10-22-3-162:~$`

## Prepare Your Environment

Next you'll need to prepare your UNIX shell's environment to be able to run the manage.py command.

Type: `sudo su - edxapp -s /bin/bash`

Then type `source edxapp_env`.

At this point you should be ready to run the user retirement command.

## Get To The Retiring Already!

### The Invocation

Here's the command you'll use to retire a user.

`python edx-platform/manage.py lms retire_user --user_email <user email> --username '<username>'`

The single quotes around username are important in case there are any spaces in there. Otherwise the shell will mis-parse the command and throw an error.

So for example if I wanted to retire myself from mitxonline production, I'd use:

`python edx-platform/manage.py lms retire_user --user_email cpatti@mit.edu --username 'ChrisPatti'`

You should see a bunch of very voluminous output. Most of it is honestly garbage for our purposes. We'll focus on the bits we care about at the end:

`2023-06-21 16:07:20,714 INFO 186970 [openedx.core.djangoapps.user_api.management.commands.retire_user] [user None] [ip None] retire_user.py:173 - User succesfully moved to the retirment pipeline`

The None here isn't anything to worry about. It's the system trying to prevent us from leaking PII (personally identifiable information) into the logs.

At this point if all went well, we're done with our edx worker shell prompt for now so we can log out. Always be super careful to not leave production shells open
unnecessarily. You'd be surprised how many systems have been brought down by someone not realizing they're in the wrong terminal :)

### Priming The Pump (Well, Pipeline In This Case)

Now that we've successfully staged our user for retirement, we need to tell the retirement pipeline to actually retire the user.

- Surf to the appropriate concourse URL for the entironment you're working with:
  - [CI](https://cicd-ci.odl.mit.edu)
  - [QA](https://cicd-qa.odl.mit.edu/)
  - [Production](https://cicd.odl.mit.edu)

  and search for 'tubular'. You'll want the tubular pipeline in the group associated with whichever product you're working with. In our case, it'd be
  the [misc-cloud-tubular pipeline in the mitxonline group](https://cicd.odl.mit.edu/teams/mitxonline/pipelines/misc-cloud-tubular).
- Click the green + icon with a circle around it in the upper right of your screen. This will trigger a run of this pipeline.
- If all goes well, you should see each stage go green one by one. You can click on any stage to see more detail around what that stage is doing.
  You can see an example of a successful pipeline run [here](https://cicd.odl.mit.edu/teams/mitxonline/pipelines/misc-cloud-tubular/jobs/deploy-tubular-world/builds/26)

At this point, if the pipeline is green, congratulations are in order! This user had been retired!
