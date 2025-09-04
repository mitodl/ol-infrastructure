# Architecture
![Jupyterhub Architectural Diagram](jupyterhub_box_diagram.svg)

# Image Building
https://github.mit.edu/ol-notebooks-qa is an org containing a template repository and all course repositories. Each course repository contains a Dockerfile and requirements.txt file alongside any Jupyter notebooks and data.

Concourse runs a [parameterized job](https://cicd.odl.mit.edu/?search=team%3A%22infrastructure%22%20group%3A%22jupyter_notebook_docker_image_build%22) which pulls from the course repositories, constructs a docker image to bundle everything together and push to a [private ECR repository](https://us-east-1.console.aws.amazon.com/ecr/repositories/private/610119931565/ol-course-notebooks?region=us-east-1) we maintain. The dockerfiles use the [official Jupyter pytorch docker images](https://jupyter-docker-stacks.readthedocs.io/en/latest/using/selecting.html#jupyter-pytorch-notebook) as a base and install tensorflow as well. The code to provision the pipelines is [here](../../../ol_concourse/pipelines/container_images/jupyter_courses.py) and the Pulumi code to set up the ECR repository is [here](../../../ol_infrastructure/infrastructure/aws/ecr/__main__.py).

# Jupyterhub

Jupyterhub will use the [KubeSpawner library](https://github.com/jupyterhub/kubespawner) (with some very slight modifications to enable [image selection via query param](dynamicImageConfig.py)) to allow users to start up and interact with a set of images we maintain. For courses, this will involve Jupyterhub starting up a notebook server for the user by pulling the corresponding course image from ECR.

The backing database is a Postgres RDS instance.

Images are pre-pulled via [hook-pre-puller and continuous-pre-puller](https://z2jh.jupyter.org/en/stable/administrator/optimization.html#pulling-images-before-users-arrive) daemonsets. This is currently configured to pull all 4 existing images we build and maintain via specification of an extraImages block.

Jupyterhub domains are currently gated by an SSO login to the olapps Keycloak realm. Once authenticated with Keycloak, Jupyterhub is set up to use the TmpAuthenticator class. When accessing the /tmplogin endpoint provided by the authenticator, Jupyterhub will unconditionally authenticate users as a random UUID. This ensures that users can spin up ephemeral notebooks provided they are able to authenticate with MIT Learn.

Culling is performed via the [jupyterhub-idle-culler](https://github.com/jupyterhub/jupyterhub-idle-culler) configured via [Helm chart](https://z2jh.jupyter.org/en/latest/resources/reference.html#cull). It currently culls both running, inactive servers as well as users. Culling users is important as we will accumulate UUID-keyed users and sessions in the database over time.
