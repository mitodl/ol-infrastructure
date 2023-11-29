# Moving a Heroku Managed Postgres DB to Pulumi Managed AWS RDS

## Preparation

You will need to gather some information about the Heroku managed application and its database before you start. You'll need the
[Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli) with auehenticated access to the application in question to continue.

You'll also need the postgresql tools, specifically the `pg_dump` and `psql` tools.

Record the following information somewhere persistent that you'll be able to refer back to through this process. I like using my [notes](https://joplinapp.org/).

- The Heroku application's name. Our convention is generally <application>-<environment> so for the CI environment of the micromasters
application, you'd use `micromasters-ci`.
- The applications's `DATABASE_URL`. You can obtain this with the following invocation: `heroku config:get DATABASE_URL -a <your app>`
- The currently attached Heroku database name as well as the alias they have assigned to it by default. You can get this with:
`heroku addons -a <application> | grep -i postgres` for example:
```
╰ ➤ heroku addons -a micromasters-ci | grep -i postgres
heroku-postgresql (postgresql-rigid-71273)  mini   $5/month   created
 └─ as HEROKU_POSTGRES_YELLOW
```
so in this case we see that postgresql-rigid-71273 is attached as HEROKU_POSTGRES_YELLOW
- A set of URLs to browse when the transition is done to ensure everything is working properly. You should also browse them before the transition to note how
everything looks.

## Building the Infrastructure with Pulumi

<!-- TODO: My branch might vanish. Change this to a CR when one exists. -->
Describing in detail how to code the necessary resources to build the AWS RDS Postgres instance and associated S3 bucket, IAM rules, VPC peerings
etc. is beyond the scope of this ducument. You can see what I did in my [Github branch](https://github.com/mitodl/ol-infrastructure/tree/cpatti_micromasters_pulumi).

Once the infrastructure is properly built, you'll need to record the new AWS RDS Postgres instance's endpoint. You can do this from within
the directory for the application you're working on. For my current project, that's `ol-infrastructure/src/ol_infrastructure/applications/micromasters`
with this: 'poetry run pulumi stack export -s applications.micromasters.CI | grep -i endpoint' but obviously sub your app in for micromasters.

You'll also need to retrieve the database password from Vault using Pulumi. You can do that with the following invocation:
`poetry run pulumi config get  "micromasters:db_password"`

## Construct A New DATABASE_URL

I suggest doing this in a text file you can source easily since you'll be working with this database a bit for this project. I keep such things in an 'envsnips' folder
in my home directory.

The file should look something like:
```
export DATABASE_URL=postgresql://oldevops:<password you pulled from Pulumi config>@micromasters-ci-app-db.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/micromasters
```

Make sure the URL has the following components:

- `postgresql://` is the protocol identifier followed by a :.
- 'oldevops' is the database user, then another :.
- the database password we pulled from Pulumi above, followed by an @ sign.
- The endpoint hostname we retrieved from Pulumi earlier, followed by a :.
- The port number. We usually use 5432. Then a /.
- The database name.

If your URL is missing any of these it will not work. Once you've finished write out your file and source it in your shell.

Now, test that you can connect using the URL you just built with:

`psql $DATABASE_URL`

If you get an access denied message, make sure you got the correct password for the app and environment (e.g. CI, QA or production) and check the
other components.

We'll assume $DATABASE_URL is set to to the new RDS database we've created for the rest of the runbook.

## Put the Heroku App Into Maintenance Mode

In order to ensure database consistency during the transition we need to put the
Heroku application into maintenance mode. 

```
heroku maintenance:on -a <app>
```

**CAUTION** customers will see a notice about ongoing maintenance until you
take the app out of maintenance mode, so be mindful of the wall clock time!


## Dump Heroku Managed DB

Use something like the following invocation to dump the contents of the current application database.

`pg_dump -x -O $(heroku config:get DATABASE_URL -a micromasters-rc) > micromasters_qa_db_dump.sql`

Obviously, substitute your app for micromasters and your environment for rc/qa.

(Aside: We use rc and qa interchangably here).

Examine the dump in your editor (read-only to be safe) and ensure that all the necessary components are present: Schema, data, foreign keys, and the like.

## Restore Dump Into AWS RDS DB

Using the DATABASE_URL we just created and tested, we can now restore the data we dumped in the prior step into the new DB:

`psql $DATABASE_URL < micromasters_qa_db_dump.sql`

You will see a lot of output representing each statement as it's processed by the DB. You shouldn't see any errors here.

## Take Heroku application out of maintenance mode

```
heroku maintenance:off -a <app>
```

At this point, the application will resume normal operation and be available to
customers.


## Coordinate Transition

In the process of changing the database out from under a running application, there will be some small period of down time, so it's important to coordinate with
all the appropriate stakeholders and leadership before you do.

## Perform The Final Transition

At the time, it's important that you perform the following steps quickly in succession, because once you detach the current DB, the application will be down.
Keep this as brief as possible.

You may wish to cue up the commands you want to run in a text file somewhere you can eaily review them, and then cut and paste them into your shell when the
time comes.

### Create An Additional DB Attachment

You'll need to create an additional attachment for the current DB:
`heroku addons:attach postgresql-amorphous-36035 --as HEROKU_POSTGRES_DB`

Substitute your db instance you gathered above. HEROKU_POSTGRES_DB is just an alias we can use if we should need to roll back.

### Detach The Current Database

This is where you'll need to use the Heroku managed database instance above, along with the Heroku application name we collected. Substitute accordingly
into the following invocation:

`heroku addons:detach postgresql-amorphous-36035 -a micromasters-rc`

### Change the DATABASE_URL to the New RDS Instance

Ensuring that your DATABASE_URL environment variable is properly set to your new RDS from the above steps, use it to set DATABASE_URL in the heroku app:

`heroku config:set -a micromasters-rc DATABASE_URL=$DATABASE_URL`

Now immediately print out the value you just set to ensure that all looks good:
`heroku config:get -a micromasters-rc DATABASE_URL`

## Test Your Work

You should carefully test the application you just transitioned to ensure everything works using the set of URLs you gathered at the beginning.
- Do the pages have all the elements they should?
- Are images loading?

## How To Roll Back

If something goes wrong and you need to roll back, don't panic!

All you need to do is promote the old Heroku managed DB back into use:

`heroku pg:promote --app micromasters-ci postgresql-rigid-71273`

Obviously substitute your db and application for the ones above.

Re-run your tests as defined above to make sure everything's working right post-rollback.

## S3 Buckets

Our applications use S3 buckets for CMS asset storage and backup among other things.

You will need to either continue using the existing buckets by using `pulumi import` or creating new onnes. You should create
new ones if the old ones don't conform to naming conventions. You'll also need to ensure that IAM permissions are properly
set in your Pulumi code.

To sync the bucket contents, use the [AWS CLI](https://docs.aws.amazon.com/cli/latest/reference/s3/sync.html) `aws s3 sync` command.

For example, the command we used to sync the old micromasters S3 bucket to the new one which conforms to our desired naming
conventions is:
```
aws s3 sync s3://odl-micromasters-production s3://ol-micromasters-app-production
```

## Saltstack & Cloudfront

Currently, we are using Saltstack to configure some aspects of our Heroku applications, including which S3 bucket to use and which
CloudFront distribution we're fronting the app with.

**TODO**: This section needs to be way less of a hand wave and include actual detail as to how to operate the Saltstack side or
whatever we replace it with.
