This is a unified process for building MIT-ODL custom resource types, publishing them to docker-hub, and also staging them to a s3 bucket so they can be bundled into AMI bakes of concourse workers. In order to include a resource type in this process you will need to ensure a few things.

1. The resource type must have its own publicly accessible git repository.
2. Within that git repository, the resource type must utilize a `tag` file containing a semantic version number for the git resource. This tag file must be within the root of the repository.
3. There must be a Dockerfile in the root of the repository and it must build cleanly without any extra steps beyond `docker build .`.

To add a new resource type to this process, create a new `vars_{resource-name}.yaml` file and populate the following three variables.

1. `git-repo` : Aforementioned publicly accessible git repository matching the above prerequisites.
2. `docker-repo` : the full docker image repository name, something like `mitodl/{resource-name}`
3. `resource-name` : The short resource name. This will be what you refer to when you write pipelines utilizing the resource, etc. Should probably match what you put in the filename.

To create a new pipeline for your new resource type:
```
fly -t pr sp build-concourse-resources-{resource-name} -c pipelines/resources/build-concourse-resources.yaml -l pipelines/resources/vars_{resource-name}.yaml
```
Right now these pipeline are in the `main` team but that may change in the future.
