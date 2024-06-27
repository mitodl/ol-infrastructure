# How To Test And Deploy MIT OL Pulumi/Packer Projects

The way we build images here at MIT OL is complicated and involves a number of
components with various moving parts, so it can be difficult to understand where
to start, what to change, and moreover how to safely test your changes without
breaking production. This document will address these issues.


# The Pipeline

Each MIT OL project that uses this technique has an accompanying
[Concourse](https://concourse.io) pipeline that builds the Packer image, then
deploys that image to the appropriate stage's AWS EC2 launch profile where new
instances will be launched by instance refresh to deploy the new code /
configuration into the wild.

One such project is [Tika](https://tika.apache.org/). It's a data transformation
service used in our data platform. [Here is its
pipeline](https://cicd.odl.mit.edu/teams/infrastructure/pipelines/packer-pulumi-tika/).

## Packer Stage

The first couple stages are reasonable self explanatory:

- Validate the packer template for any syntax errors and the like
- Actually run packer build on the template, invoking pyinfra and packer,
producing an AMI.

You can find the pyinfra sources in ol-infrastructure
src/bilder/images/<projectr>. Here is the pyinfra folder for
[Tika](https://github.com/mitodl/ol-infrastructure/tree/main/src/bilder/images/tika).

### Testing

If you want to test the pyinfra portion locally, you can use the following
invocation:

```
# cpatti @ rocinante in ~/src/mit/ol-infrastructure/src/bilder/images/tika on git:main o [17:11:31] C:1
$ pr pyinfra @docker/debian:latest deploy.py
```

You should see output similar to the following:

_TODO: Is there a base image we can use that won't whine about curl/wget and say
no hosts remaining?_

```
--> Loading config...

--> Loading inventory...

--> Connecting to hosts...
    [@docker/debian:latest] Connected

--> Preparing Operations...
    Loading: deploy.py
    [@docker/debian:latest] Ready: deploy.py

--> Proposed changes:
    Groups: @docker
    [@docker/debian:latest]   Operations: 32   Change: 32   No change: 0


--> Beginning operation run...
--> Starting operation: Install Hashicorp Products | Ensure unzip is installed
    [@docker/debian:latest] Success

--> Starting operation: Install Hashicorp Products | Create system user for vault
    [@docker/debian:latest] Success

--> Starting operation: Install Hashicorp Products | Download vault archive
    [@docker/debian:latest] sh: 1: curl: not found
    [@docker/debian:latest] sh: 1: wget: not found
    [@docker/debian:latest] Error: executed 0/3 commands
    [@docker/debian:latest] docker build complete, image ID: cc0b00b971bd
--> pyinfra error: No hosts remaining!

```

When you're satisfied that your pyinfra build will at least build correctly, you
can trigger a local packer image build that the CI deployment stage can consume
to get your changes into CI. Note that this can take a while, so be prepared for
it to chug for 15-20 minutes. Great time to go get a beverage :)

You can kick this off with an invocation like the following:

```
poetry run packer build  src/bilder/images/tika/tika.pkr.hcl
```

Note that obviously your path will change if the project you're building is
different.

Note also that you may require a slightly different invocation for different
projects. In Tika's case we require a custom Packer template as specified above,
but in the case of other projects which use the default Packer template, you
might use an invocation like the one we use to build Concourse's image:

```
poetry run packer build -on-error=ask -var node_type=web -var app_name=concourse -only amazon-ebs.third-party src/bilder/images
```

Note the node_type and app_name variable declarations above. For Concourse, we
can build either a web image or a worker image, so it's important we include
node_type in our invocation.

## Deploy Stage

The remaining boxes in the pipeline are deployment stages. One per environment
stage e.g. CI, QA and Production.

Each of these deployment stages basically runs the equivalent of a `pulumi up`
on the Pulumi stack associated with the application in question. Here's the one
for
[Tika](https://github.com/mitodl/ol-infrastructure/tree/main/src/ol_infrastructure/applications/tika).

Let's take a look at the output from a build of this stage:

```
INFO:root:@ updating....

8< snip for brevity 8<

INFO:root:    aws:ec2:LaunchTemplate tika-server-tika-ci-launch-template  [diff: ~blockDeviceMappings]

INFO:root:    aws:autoscaling:Group tika-server-tika-ci-auto-scale-group  [diff: ~instanceRefresh,tags,vpcZoneIdentifiers]

INFO:root:@ updating....

INFO:root:    pulumi:pulumi:Stack ol-infrastructure-tika-server-applications.tika.CI

INFO:root:    ol:infrastructure:aws:auto_scale_group:OLAutoScaleGroup tika-server-tika-ci

INFO:root:

INFO:root:Resources:

INFO:root:    17 unchanged
```

From this rather verbose blurb we can see that Pulumi updated the ASG and all
associated resources like the Launch template. This is the mechanism which seeds
the updated image into our EC2 environment where instance refresh safely cycles
the old instances out and the ones with our updated code in.
