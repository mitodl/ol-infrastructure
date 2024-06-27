## tika servers

There is nothing particularly special about [tika](https://tika.apache.org/) servers and their environments.

- They require a `x_access_token` specified in the sops configurations for each environment.
  - There are application consumers of the tika service in QA + Production, so you should set this to the right thing found in vault.
- In the image definition, they are coded to use a 2GB JVM heap, so t3.medium is probably the smallest you want to go for instance size. This could probably be revisited ... I suspect this is overkill for our use case but I don't have the evidence to support that assertion.

There isn't much else worth remarking on for Tika
