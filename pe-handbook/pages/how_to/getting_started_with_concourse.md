# Getting Started Building Concourse Pipelines in Python

## Getting Set Up
- Use the [Tutorial](https://concourse-ci.org/getting-started.html) on the Concourse website under Docs->Getting Started to get set up.
  - Do NOT use any other tutorials you find linked elsewhere. Some are out of date and the failure modes can be incredibly hard to debug!
  - If you're using an Arm64 Mac (e.g. M1, M2, etc.) just substitute [this](https://github.com/robinhuiser/concourse-arm64) platform specific image for the one cited in the tutorial. So the first line of your docker-compose.yml should look something like this: `image: rdclda/concourse:7.7.1`
  - Nota Bene: You should probably use the latest version of this. At the time of this writing the latest has a bug but that will likely be fixed by the time anyone reads this.
  - You'll also need to install Docker Desktop for Mac.
  - Be sure that at the end of this process you have a Concourse instance running locally in your Docker install. If you
    didn't end up running `docker-compose up -d` or similar then you may have missed something. Watch for errors and check that your newly launched Concourse is healthy and that you can browse to the localhost URL cited in the tutorial which should get you the Concourse main page with the giant spinning turbine blade icon.


## Now We're Cooking with Gas! Learning How Concourse Works By Building Pipelines
- Work through the entire tutorial including building and actually creating pipelines from all the examples. This is critical so
you'll have an understanding of what all the component parts are and how they fit together as you code your Python in later steps.
- Actually make a point of working with some of the later examples involving Git repos. Commit some chnges to your test repo and
watch them flow through the pipeline. Pretty neat eh?
- You may notice that some of the later examples get pretty unwieldy and become difficult to get right. It can be tricky figuring
out what level of indentation is correct just by eyeballing it. You might find the [yamllint](https://github.com/adrienverge/yamllint)
utility helpful for this as it will tell you when there are syntax errors. Ignore its whining about improper indent levels and
focus on the errors :)

## It Gets Easier - Building Pipelines in Python

Thankfully, we have been spared the pain of coding pipelines in YAML by virtue of a [Python wrapper](https://github.com/mitodl/ol-infrastructure/tree/main/src/ol_concourse)
that Tobias Macey wrote.

Each YAML section is wrapped in a Python object. It's a 1 to 1 mapping because the Python models are auto-generated from the
schema defined by Concourse.

Tahe a look at the simplest hello-world tutorial example converted into Python [here](). I've put the explanatory comments inline
to make it easier to understand what's going on.

## Actually Building a Pipeline From Your Python

Our Concourse pipeline Python build scripts, like everything else we maintain, is managed by [Poetry](https://python-poetry.org/).

So, to actually invoke our hello world script and get ready to actually create the pipeline, we run:
`poetry run python3 ./hello.py`

This will emit the JSON our Python produces, along with an actual Concourse `fly` invocation at the end. This assumes you're
already properly logged into your Concourse instance. For our purposes use the same one you set up previously to run through the
tutorials.

The output should look something like this:
```json
{
  "jobs": [
    {
      "build_logs_to_retain": null,
      "max_in_flight": 1.0,
      "serial": null,
      "old_name": null,
      "on_success": null,
      "ensure": null,
      "on_error": null,
      "disable_manual_trigger": null,
      "serial_groups": null,
      "build_log_retention": null,
      "name": "deploy-hello-world",
      "plan": [
        {
          "config": {
            "image_resource": {
              "source": {
                "repository": "busybox",
                "tag": "latest"
              },
              "params": null,
              "version": null,
              "type": "registry-image"
            },
            "caches": null,
            "run": {
              "args": [
                "Hello, World!"
              ],
              "user": null,
              "path": "echo",
              "dir": null
            },
            "inputs": null,
            "platform": "linux",
            "params": null,
            "container_limits": null,
            "outputs": null,
            "rootfs_uri": null
          },
          "file": null,
          "params": null,
          "task": "hello-task",
          "privileged": null,
          "vars": null,
          "output_mapping": null,
          "image": null,
          "input_mapping": null,
          "container_limits": null
        }
      ],
      "interruptible": null,
      "public": null,
      "on_failure": null,
      "on_abort": null
    }
  ],
  "groups": null,
  "resource_types": null,
  "var_sources": null,
  "display": null,
  "resources": null
}
```
```bash
fly -t pr-inf sp -p misc-cloud-hello -c definition.json
```


Since we'll be using fly to create our pipeline in the tutorial Concourse instance, use -t tutorial rather than pr-inf and make
any other necessary substitutions according to your environment.

That's it! You should now have the basic Hello World pipeline created from your Python source and operating properly, printing
that famous phrase as a result.

*TODO* Add a more meaty example like the the-artifact example so we get to show inputs and outputs.
