# Summary

This document will detail the best practice we use to develop and deploy changes
to our Concourse pipeline web and worker servers.

## Developing

As with most projects here at MIT OL's Devops team, you'll want to start by
checking out the
[ol-infrastructure](https://github.com/mitodl/ol-infrastructure/) project to
your local workspace.

As usual, be sure to run `poetry install` so all the right dependencies will be
cached and ready to run your newly changed [pyinfra](https://pyinfra.com/) code.

Now change directory to the src/bilder/images/concourse folder. Here you'll see
a number of files. The most important for our purposes is
[deploy.py](https://github.com/mitodl/ol-infrastructure/blob/main/src/bilder/images/concourse/deploy.py).

Very likely, any chances you might want to make will be in this file.

## Local Testing

Apparently I'm alone on this bus, but I personally enjoy testing changes on my
machine before I commit them to Git. The testing doesn't have to be deep,
something like syntax and being end to end runnable are good enough for me.

In any case, in order to locally build the docker container, run the following
command:

`pyinfra @docker/debian:bookworm deploy.py`

**NOTA BENE**: I am not entirely sure about the Debian Bookworm base container
I'm using here, it worked well enough for my very shallow testing purposes.

**TODO**: Figure out what we actually use for a base container and cite that in
this doc.

If you have a syntax error or an error in your pyinfra code that prevents the
container from building, you will see that on your screen and can debug it
accordingly.  Otherwise, you'll see a message about the container building
successfully.

## Build The Image

Now that we've very shallowly 'smoke tested' our code, let's run the actual
official script to build the AMI with packer and stage it for later deployment.

From the same directory, run the following command:

`pr packer build -on-error=ask -var node_type=web -var app_name=concourse -only amazon-ebs.third-party src/bilder/images`

You will see a LOT of output and the process could take up to 20 minutes.

## Initial Testing in CI

For most of the rest of the journey to production, you'll be using a pipeline
rather than doing it by hand with Pulumi, but for your first time, you'll
probably want to do it by hand and then minutely monitor the resultant deployed
image in CI to ensure everything went smoothly.

**TODO: Add curl smoke test for web node**

## Deployment to QA and Beyond

For the remainder of your change's journey through QA and ultimately to
production, you'll be using the [Concourse
pipeline](https://cicd.odl.mit.edu/teams/infrastructure/pipelines/packer-pulumi-concourse).

**TODO: Add further testing details**
