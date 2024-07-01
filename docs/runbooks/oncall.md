* [Style Guide](#style-guide)
* [SaltStack](#saltstack)
* [XQueueWatcher](#xqueuewatcher)
* [OVS](#ovs)
* [Bootcamp Ecommerce](#bootcamp-ecommerce)
* [OpenEdX Residential MITx](#openedx-residential-mitx)
* [XPro](#xpro)
* [MITXOnline](#mitxonline)
* [Reddit](#reddit)


# Introduction

This document is meant to be one stop shopping for your MIT OL Devops oncall needs.

Please update this doc as you handle incidents whenever you're oncall.

## Style Guide

There should be a table of contents at the top of the document with links to
each product heading. Your editor likely has a plugin to make this automatic.

Each product gets its own top level heading.

Entries that are keyed to a specific alert should have the relevant text in a
second level heading under the product. Boil the alert down to the most relevant
searchable text and omit specifics that will vary. For instance:

```
"[Prometheus]: [FIRING:1] DiskUsageWarning mitx-production (xqwatcher filesystem /dev/root ext4 ip-10-7-0-78 integrations/linux_hos"
```
would boil down to `DiskUsageWarning xqwatcher` because the rest will change and
make finding the right entry more difficult.

Each entry should have at least two sections, Diagnosis and Mitigation. Use
_bold face_ for the section title.
This will allow the oncall to get only as much Diagnosis in as required to
identify the issue and focus on putting out the fire.

# Products

## SaltStack


### MemoryUsageWarning operations-<ENVIRONMENT>

_Diagnosis_

You get an alert like: `[Prometheus]: [FIRING:1] MemoryUsageWarning operations-qa (memory ip-10-1-3-33 integrations/linux_host warning)`.


You'll need an account and ssh key set up on the saltstack master hosts. This should happen when you join the team.

Now, ssh into the salt master appropriate to the environment you received the alert for. The IP address is cited in the alert. So, for the above:

(Substitute your username and the appropriate environment if not qa, e.g. production)
```
ssh -l cpatti salt-qa.odl.mit.edu
```

Next, check free memory:

```
mdavidson@ip-10-1-3-33:~$ free -h
              total        used        free      shared  buff/cache   available
Mem:           7.5G        7.2G        120M         79M        237M         66M
Swap:            0B          0B          0B
```

In this case, the machine only has 120M free which isn't great.

_Mitigation_

We probably need to restart the Salt master service. Use the systemctl command for that:


```
root@ip-10-1-3-33:~#  systemctl restart salt-master
```

Now, wait a minute and then check free memory again. There should be significantly more available:

```
root@ip-10-1-3-33:~# free -h
              total        used        free      shared  buff/cache   available
Mem:           7.5G        1.9G        5.3G         80M        280M        5.3G
Swap:            0B          0B          0B
```

If what you see is something like the above, you're good to go. Problem solved (for now!)

## XQueueWatcher

### DiskUsageWarning xqwatcher

_Diagnosis_

This happens every few months if the xqueue watcher nodes hang around for that
long.

_Mitigation_

```console
From salt-pr master:

sudo ssh -i /etc/salt/keys/aws/salt-production.pem ubuntu@10.7.0.78
sudo su -

root@ip-10-7-0-78:~# df -h
Filesystem      Size  Used Avail Use% Mounted on
/dev/root        20G   16G  3.9G  81% /           <<<<<<<<<<<<<<<<<<<<<<<<<< offending filesystem
devtmpfs        1.9G     0  1.9G   0% /dev
tmpfs           1.9G  560K  1.9G   1% /dev/shm
tmpfs           389M  836K  389M   1% /run
tmpfs           5.0M     0  5.0M   0% /run/lock
tmpfs           1.9G     0  1.9G   0% /sys/fs/cgroup
/dev/loop1       56M   56M     0 100% /snap/core18/2751
/dev/loop2       25M   25M     0 100% /snap/amazon-ssm-agent/6312
/dev/loop0       25M   25M     0 100% /snap/amazon-ssm-agent/6563
/dev/loop3       54M   54M     0 100% /snap/snapd/19361
/dev/loop4       64M   64M     0 100% /snap/core20/1950
/dev/loop6       56M   56M     0 100% /snap/core18/2785
/dev/loop5       54M   54M     0 100% /snap/snapd/19457
/dev/loop7       92M   92M     0 100% /snap/lxd/24061
/dev/loop8       92M   92M     0 100% /snap/lxd/23991
/dev/loop10      64M   64M     0 100% /snap/core20/1974
tmpfs           389M     0  389M   0% /run/user/1000

root@ip-10-7-0-78:~# cd /edx/var           <<<<<<<<<<<<<<<<<<< intuition / memory

root@ip-10-7-0-78:/edx/var# du -h | sort -hr | head
8.8G	.
8.7G	./log
8.2G	./log/xqwatcher         <<<<<<<<<<<< Offender
546M	./log/supervisor
8.0K	./supervisor
4.0K	./xqwatcher
4.0K	./log/aws
4.0K	./aws
root@ip-10-7-0-78:/edx/var# cd log/xqwatcher/
root@ip-10-7-0-78:/edx/var/log/xqwatcher# ls -tlrha
total 8.2G
drwxr-xr-x 2 www-data xqwatcher 4.0K Mar 11 08:35 .
drwxr-xr-x 5 syslog   syslog    4.0K Jul 14 00:00 ..
-rw-r--r-- 1 www-data www-data  8.2G Jul 14 14:12 xqwatcher.log             <<<<<<<<< big file

root@ip-10-7-0-78:/edx/var/log/xqwatcher# rm xqwatcher.log

root@ip-10-7-0-78:/edx/var/log/xqwatcher# systemctl restart supervisor.service
Job for supervisor.service failed because the control process exited with error code.
See "systemctl status supervisor.service" and "journalctl -xe" for details.
root@ip-10-7-0-78:/edx/var/log/xqwatcher# systemctl restart supervisor.service       <<<<<<<<<<<<  Restart it twice because ???

root@ip-10-7-0-78:/edx/var/log/xqwatcher# systemctl status supervisor.service
● supervisor.service - supervisord - Supervisor process control system
     Loaded: loaded (/etc/systemd/system/supervisor.service; enabled; vendor preset: enabled)
     Active: active (running) since Fri 2023-07-14 14:12:51 UTC; 4min 48s ago
       Docs: http://supervisord.org
    Process: 1114385 ExecStart=/edx/app/supervisor/venvs/supervisor/bin/supervisord --configuration /edx/app/supervisor/supervisord.conf (code=exited, status=0/SUCCESS)
   Main PID: 1114387 (supervisord)
      Tasks: 12 (limit: 4656)
     Memory: 485.8M
     CGroup: /system.slice/supervisor.service
             ├─1114387 /edx/app/supervisor/venvs/supervisor/bin/python /edx/app/supervisor/venvs/supervisor/bin/supervisord --configuration /edx/app/supervisor/supervisord.conf
             └─1114388 /edx/app/xqwatcher/venvs/xqwatcher/bin/python -m xqueue_watcher -d /edx/app/xqwatcher

root@ip-10-7-0-78:/edx/var/log/xqwatcher# ls -lthra
total 644K
drwxr-xr-x 5 syslog   syslog    4.0K Jul 14 00:00 ..
drwxr-xr-x 2 www-data xqwatcher 4.0K Jul 14 14:12 .
-rw-r--r-- 1 www-data www-data  636K Jul 14 14:17 xqwatcher.log                <<<<<<<< New file being written to
root@ip-10-7-0-78:/edx/var/log/xqwatcher# df -h .
Filesystem      Size  Used Avail Use% Mounted on
/dev/root        20G  7.4G   12G  38%                  <<<<<<<<<<< acceptable utilization
```

## OVS

### [Prometheus]: [FIRING:1] InvalidAccessKeyProduction apps-production (odl-video-service critical)

_Diagnosis_

This happens sometimes when the applications's instance S3 credentials become out of date.

_Mitigation_

Use the AWS EC2 web console and navigate to the EC2 -> Auto Scaling Group pane. Search on:
`odl-video-service-production`

Once you have the right ASG, click on the "Instance Refresh" tab and then click
the "Start Instance Refresh" button.

_Be sure to un-check the "Enable Skip Matching" box_, or your instance refresh
will most likely not do anything at all.

### Request by deeveloper to add videos

_Diagnosis_

N/A - developer request

_Mitigation_

Use the AWS EC2 web console and find instances of type
`odl-video-service-production` - detailed instructions for accessing the
instance can be found
[here](https://github.com/mitodl/ol-infrastructure/blob/main/docs/how_to/access_openedx_djange_manage.md).

The only difference in this case is that the user is `admin` rather than
`ubuntu`. Stop when you get a shell prompt and rejoin this document.

First, run:

`sudo docker compose ps` to see a list of running processes. In our case, we're
looking for `app`. This isn't strictly necessary here as we know what we're
looking for, but good to look before you leap anyway.

You should see something like:

```console
admin@ip-10-13-3-50:/etc/docker/compose$ sudo docker compose ps
NAME                IMAGE                                 COMMAND                  SERVICE             CREATED             STATUS              PORTS
compose-app-1       mitodl/ovs-app:v0.69.0-5-gf76af37     "/bin/bash -c ' slee…"   app                 3 weeks ago         Up 3 weeks          0.0.0.0:8087->8087/tcp, :::8087->8087/tcp, 8089/tcp
compose-celery-1    mitodl/ovs-app:v0.69.0-5-gf76af37     "/bin/bash -c ' slee…"   celery              3 weeks ago         Up 3 weeks          8089/tcp
compose-nginx-1     pennlabs/shibboleth-sp-nginx:latest   "/usr/bin/supervisor…"   nginx               3 weeks ago         Up 3 weeks          0.0.0.0:80->80/tcp, :::80->80/tcp, 0.0.0.0:443->443/tcp, :::443->443/tcp
```

Now run:

`sudo docker compose exec -it app /bin/bash` which should get you a new, less
colorful shell prompt.

At this point you can run the manage.py command the developer gave you in slack.
In my case, this is what I ran and the output I got:

```console
mitodl@486c7fbba98b:/src$ python ./manage.py add_hls_video_to_edx --edx-course-id course-v1:xPRO+DECA_Boeing+SPOC_R0
Attempting to post video(s) to edX...
Video successfully added to edX – VideoFile: CCADE_V11JW_Hybrid_Data_Formats_v1.mp4 (105434), edX url: https://courses.xpro.mit.edu/api/val/v0/videos/
```

You're all set!

## Bootcamp Ecommerce

### [Prometheus]: [FIRING:1] AlternateInvalidAccessKeyProduction production (bootcamp-ecommerce critical)

_Diagnosis_

N/A

_Mitigation_

You need to refresh the credentials the salt-proxy is using for Heroku to manage this app.

- ssh to the salt production server: `ssh salt-production.odl.mit.edu`
- Run the salt proxy command to refresh creds: `salt proxy-bootcamps-production state.sls heroku.update_heroku_config`. You should see output similar to the following:

```
cpatti@ip-10-0-2-195:~$ sudo salt proxy-bootcamps-production state.sls heroku.update_heroku_config
proxy-bootcamps-production:
----------
          ID: update_heroku_bootcamp-ecommerce_config
    Function: heroku.update_app_config_vars
        Name: bootcamp-ecommerce
      Result: True
     Comment:
     Started: 14:43:58.916128
    Duration: 448.928 ms
     Changes:
              ----------
              new:
                  ----------

** 8< snip 8< secret squirrel content elided **

Summary for proxy-bootcamps-production
------------
Succeeded: 1 (changed=1)
Failed:    0
------------
Total states run:     1
Total run time: 448.928 ms
cpatti@ip-10-0-2-195:~$
```

## OpenEdX Residential MITx

### Task handler raised error: "OperationalError(1045, "Access denied for user 'v-edxa-fmT0KbL5X'@'10.7.0.237' (using password: YES)

_Diagnosis_

If the oncall receives this page, instances credentials to access Vault and the
secrets it contains have lapsed.

_Mitigation_

Fixing this issue currently requires an instance refresh, as the newly launched
instances will have all the necessary credentials.

From the EC2 console, on the left hand side, click "Auto Scaling Groups", then
type 'edxapp-web-mitx-<ENVIRONMENT>' e.g. 'edxapp-web-mitx-production'. This
should yield 1 result with something like 'edxapp-web-autoscaling-group-XXXX' in
the 'Name' column. Click that.

Now click the "Instance Refresh" tab.

Click "Start instance refresh".

_Be sure to un-check the "Enable Skip Matching" box_, or your instance refresh
will most likely not do anything at all.

Monitor the instance refresh to ensure it completes successfully. If you have
been receiving multiple similar pages, they should stop coming in. If they
continue, please escalate this incident as this problem is user visible and thus
high impact to customers.

## XPro

### ApiException hubspot_xpro.tasks.sync_contact_with_hubspot

_Diagnosis_

This error is thrown when the Hubspot API key has expired.

You'll see an error similar to this one in
[Sentry](https://mit-office-of-digital-learning.sentry.io/issues/3925327041/?environment=production&project=1413655&query=is%3Aunresolved+issue.priority%3A[high%2C+medium]+hubspot&referrer=issue-stream&statsPeriod=14d&stream_index=0).

_Mitigation_

The fix for this is to generate a new API key in Hubspot and then get that key
into Vault, triggering the appropriate pipeline deployment afterwards.

First, generate a new API key in Hubspot. You can do this by logging into
Hubspot,

You can do this using the username/password and TOTP token found in
[Vault](https://vault-production.odl.mit.edu/ui/vault/secrets/platform-secrets/kv/hubspot/details?version=1.

Once you're logged in, click "Open" next to "MIT XPro" in the Accounts list.

Then, click on the gear icon in the upper right corner of the page and select
"Integrations" -> "Private Apps" in the sidebar on the left.

You should then see the XPRo private app and beneath that a link for "View
Access Token". Click that, then click on the "Manage Token" link.

On this screen, you should see a "Rotate" button, click that to generate a new
API key.

Now that you've generated your new API token, you'll need to get that token into
Vault using SOPS. You can find the right secrets file for this in Github
[here](https://github.com/mitodl/ol-infrastructure/blob/main/src/bridge/secrets/xpro/secrets.production.yaml).

The process for deploying secrets deserves its own document, so after adding the
new API token to the SOPS decrypted secrets file you just generated, commit it
to Github, ensure it runs through the appropriate pipelines and ends up in
Vault.

You can find the ultimate home of the XPro Hubspot API key in Vault
[here](https://vault-production.odl.mit.edu/ui/vault/secrets/secret-mitxpro/show/hubspot-api-private-token).

Once the new API token is in the correct spot, you'll need to ensure that new
token gets deployed to production in Heroku by tracking its progress in
[this](https://cicd.odl.mit.edu/teams/infrastructure/pipelines/pulumi-xpro)
pipeline.

You will likely need to close Concourse Github workflow issues to make this
happen. See [its users
guide](https://github.com/mitodl/ol-infrastructure/blob/main/docs/how_to/concourse_github_issues_user_guide.md)
for details.

Once that's complete, you should have mitigated this issue. Keep checking that
Sentry page to ensure that the Last Seen value reflects something appropriately
long ago and you can resolve this ticket.

If you are asked to run a sync to Hubspot:
- Inform the requester, preferably on the #product-xpro Slack that the process
will take quite a long time. If this is time critical they may ask you to run
only parts of the sync. You can find documentation on the command you'll run
[here](https://github.com/mitodl/mitxpro/blob/master/hubspot_xpro/management/commands/sync_db_to_hubspot.py).

Since XPro runs on Heroku, you'll need to get a Heroku console shell to run the
management command. You can get to that shell by logging into heroku with the
Heroku CLI and running:

```
heroku run /bin/bash -a xpro-production
```

It takes a while but you will eventually get your shell prompt.

From there, run the following commands. To sync all variants:

```
./manage.py sync_db_to_hubspot create
```

If you're asked to run only one, for example deals, you can consult the
documentation linked above and see that you should add the `--deals` flag to the
invocation.

Be sure to inform the requester of what you see for output and add it to the
ticket for this issue if there is one.

If you see the command fail with an exception, note the HTTP response code. In
particular a 401 means that the API key is likely out of date. A 409 signals a
conflict (e.g. dupe email) that will likely be handled by conflict resolution
code and thus can probably be ignored.

## MITXOnline

### Cybersource credentials potentially out of date

_Diagnosis_

Often we will get a report like [this](https://github.com/mitodl/hq/issues/4052)
indicating that one of our Cybersource credentials is out of date.

_Mitigation_

Since we have no access to the Cybersource web UI, we must send E-mail
to: <sbmit@mit.edu> to validate the status of the current credential
or request a new one.


### Grading Celery Task Failed (STUB entry. Needs love)

_Diagnosis_

Usually we'll get reports from our users telling us that grading tasks have
failed.

At that point we should surf to [celery
monitoring](https://celery-monitoring.odl.mit.edu/) and login with your
Keycloak Platform Engineering realm credentials.

Then, get the course ID for the failed grading tasks and search for it in
Celery Monitoring by entering the course key in the kwargs input, surrounded
by {*' and '*}, for example {*'course-v1:MITxT+14.310x+1T2024'*}.


_Mitigation_

You may well be asked to run the `compute_graded` management command on the LMS
for mitxonline. (TODO: Needs details. How do we get there? etc.)

## Reddit

### [Prometheus]: [FIRING:1] DiskUsageWarning production-apps (reddit filesystem /dev/nvme0n1p1 ext4 ip-10-13-1-59 integrations/linux_
### [Pingdom] Open Discussions production home page has an alert

_Diagnosis_

We often get low disk errors on our reddit nodes, but in this case the low disk
alert was paired with a pingdom alert on open-discussions.  This may mean that
pgbouncer is in trouble on reddit, likely because its credentials are out of
date.

You can get a view into what's happening by logging into the node cited in the
disk usage ticket and typing:

```
salt reddit-production* state.sls reddit.config,pgbouncer
```

_Mitigation_

Once you've determined that pgbouncer is indeed sad, you can try a restart /
credential refresh with the following command:

```
salt reddit-production* state.sls reddit.config
```
