# The Problem

Here at MIT OL we use [Concourse CI](https://concourse-ci.org/). It's an
incredibly powerful package for managing complex continuous integration
workflows, and we leverage its power in all kinds of interesting ways.

What that means for you, the developer however is that our pipelines can be
difficult to get one's head around and understand. As an example,
[this](https://cicd.odl.mit.edu/teams/infrastructure/pipelines/docker-packer-pulumi-edxapp-global)
is our edx platform meta-pipeline.

# The Ask

Most of the time, there are exactly two questions software developers wants
answered when it comes to deploying their software:

1. How can I tell when my code has been deployed to $X?
2. How can I trigger my code to be deployed to $X?

# The Solution

Thankfully, our director Tobias came up with an excellent and novel solution.
What's one of the most common non code mechanism developers use to govern their work?

Github Issues!

## A Traffic Light For Deploys

Now that you've indulged me with a full page of setup. Let's get down to brass
tacks and answer those two most common questions devs have about their deploys:

1. How can I tell when my code has been deployed to $X?

We're currently keeping the Github Issues that govern our pipelines on MIT's
internal Github, because the public one has throttles that were shooting us in
the foot :)

So, take a look at the issues for the
[concourse-workflow](https://github.mit.edu/ol-platform-eng/concourse-workflow/issues) repo.

Right now, that page looks something like this (simplified view):

```
[bot] Pulumi ol-infrastructure-vault-encryption_mounts substructure.vault.encryption_mounts.operations.Production deployed. DevOps finalized-deployment pipeline-workflow product:infrastructure
#1427 opened yesterday by tmacey
[bot] Pulumi ol-infrastructure-forum-server applications.forum.xpro.Production deployed. DevOps finalized-deployment pipeline-workflow product:infrastructure
#1426 opened yesterday by tmacey
[bot] Pulumi ol-infrastructure-dagster-server applications.dagster.QA deployed. DevOps pipeline-workflow product:infrastructure promotion-to-production
#1425 opened yesterday by tmacey
```

Let's say I'm someone on the data platform team and I'm wondering whether or not
my changes have been deployed in the Dagster project. Aha! I look down the list
and see that the Dagster project has been deployed to QA, but not to production.

Let's click on [that
issue](https://github.mit.edu/ol-platform-eng/concourse-workflow/issues/1425)
and take a look.

At the time of this writing, this issue is Open, which means concourse Github
issues is waiting for us to tell it that we're ready to deploy these changes to
production. We can see everything this deployment would contain because for each
Concourse build that's happened since this issue was created, a comment was
added along with the build log.

If we're ready to move these changes to production, we just Close this issue.
That's all there is to it!

If you want to watch your change's progress, just click one of those build log
links which will bring you to the pipeline where that's happening. You should
see a new build in progress. Just click that tab to see the details.

And that's all there is to know! It's a simple system for busy people.

If you have any questions, don't hesitate to reach out to @sre on slack or send
us E-mail at oldevops@mit.edu

Thanks for taking the time to read this doc! Obviously feel free to suggest any
improvements or let me know if anything's unclear.
