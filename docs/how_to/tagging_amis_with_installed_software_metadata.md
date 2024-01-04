# Tagging AMIs with Installed Software Metadata

## Background

We are looking to start a new pattern where each AMI we produce is tagged with metadata about the software installed on it. This included 3rd party software such as Hashicorp products as well as information our applications. Recently we've been asked 'What version of X is running in production right now?" and it was a surprisingly difficult question to answer. The idea behind this new pattern is to change that, by providing the required information simply by inspecting the AMI behind any running EC2 instance.

# Implementation

We use [pyinfra](https://pyinfra.com/) to do most of the operations involved with creating a new AMI and this is no exception.

Firstly, during the new build we will create a file on the build instance at `/etc/ami_tags.json` which contains our tag keys and values.

```
from bilder.lib.ami_helpers import build_tags_document
tags_json = json.dumps(
    build_tags_document(
        source_tags={
            "consul_version": VERSIONS["consul"],
            "consul_template_version": VERSIONS["consul-template"],
            "vault_version": VERSIONS["vault"],
            "docker_repo": DOCKER_REPO_NAME,
            "docker_digest": DOCKER_IMAGE_DIGEST,
            "edxapp_repo": edx_platform.git_origin,
            "edxapp_branch": edx_platform.release_branch,
            "edxapp_sha": edx_platform_sha,
            "theme_repo": theme.git_origin,
            "theme_branch": theme.release_branch,
            "theme_sha": theme_sha,
        }
    )
)
files.put(
    name="Place the tags document at /etc/ami_tags.json",
    src=io.StringIO(tags_json),
    dest="/etc/ami_tags.json",
    mode="0644",
    user="root",
)
```
This file persists as part of the AMI and will exist on any instances spawned from the image.

Next, we need to add three steps to our packer `build` stanza.

First, we need to retrieve the file we just created remotely in the pyinfra code. We use the same SSH information that we utilized when we ran `pyinfra`. This needs to be a `provisioner` step because the build instance still needs to be running in order to copy a file from it.

```
provisioner "shell-local" {
  inline = ["scp -o StrictHostKeyChecking=no -i /tmp/packer-${build.ID}.pem ${build.User}@${build.Host}:/etc/ami_tags.json /tmp/ami_tags-${build.ID}.json"]
}
```
Second, we create a `post-processor` that generates a `packer manifest` for the build. This is just a json file local to the machine running the packer build (not the remote ec2 build instance as before). Because this is the first `post-processor` and follows the last `provisioner` step, the remote EC2 instance has been terminated and an AMI has been generated. The manifest will contain the AMI ID which is needed for the next step.
```
post-processor "manifest" {
  output = "/tmp/packer-build-manifest-${build.ID}.json"
}
```

Finally, we will take the AMI ID out of the `packer manifest` and combined with the `ami_tags.json` file we will make a `create-tags` call on the newly created AMI to add our metadata to it.

```
post-processor "shell-local" {
  inline = ["AMI_ID=$(jq -r '.builds[-1].artifact_id' /tmp/packer-build-manifest-${build.ID}.json | cut -d \":\" -f2)",
            "aws ec2 create-tags --resource $AMI_ID --cli-input-json \"$(cat /tmp/ami_tags-${build.ID}.json)\"",
            "aws --no-cli-pager ec2 describe-images --image-ids $AMI_ID"]
}
```
