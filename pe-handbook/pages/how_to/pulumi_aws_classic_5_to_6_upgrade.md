So, this is roughly what I believe is happening. So you guys can know too.

1. I updated / tested a bunch of stacks, mostly CI or sometimes QA, yesterday with the `pulumi-aws 6.5.0` upgrade. That is a two part thing. Python package but also provider.
2. Part of that upgrade, pulumi changed some of the state/stack structures and they are no longer backwards compatible, in particular those around RDS instances.
3. So I, by hand, upgraded a bunch of stacks to the new provider, they migrate nicely everything is good.
4. Concourse comes around and something triggers it and it applies the stacks again but with the old provider. It works okay the first time, but when the second time comes around it decides it has lost track of the RDS instance and it tries to recreate it.
5. If it DOES try to recreate it, thankfully it fails, because the RDS instance is still there, just pulumi has lost it, and a duplicate error gets thrown and the apply/up fails.
6. But, now the stack is broken and requires manual intervention to revive it.
    * So what I do is I go into the history/checkpoint area in S3 and find the most recent checkpoint that references the 6.5.0 provider (the one I upgraded to). I pull that down.
    * Checkpoints can’t be imported directly they need to be fixed. Fix it with this: <https://gist.github.com/clstokes/977b7bd00b37e0a564f707f0ebe36e08>
    * `pr pulumi stack import -s <stackname> --file <fixed stack checkpoint file>`
    * `poetry install` using an environment pre-6.5.0 upgrade if necessary (I keep multiple ol-inf envs for exactly this kind of thing so I just switch between my 5.4 and 6.5 envs).
    * `pulumi plugin rm resource aws 6.5.0` uninstall the PROVIDER
    * `pr pulumi up --refresh -s <stackname>` x2 — Shouldn’t be trying to create a database anymore.
7. Many of the stacks were fine because they either:
    * didn’t have RDS resources
    * didn’t get up’d by concourse inbetween.
8. BUT! BUT! When the PR making this upgrade was merged to `main` , it trigged nearly everything. Very sad. :disappointed:
    * But the code has been merged so that is fine right? Wrong.
    * The resources that concourse uses to do the needful are not upgraded yet. Specifically these:
        * <https://hub.docker.com/r/mitodl/concourse-pulumi-resource-provisioner>
        * <https://hub.docker.com/r/mitodl/concourse-pulumi-resource>
        * <https://hub.docker.com/r/mitodl/ol-infrastructure>
    * These actually have a complicated silent depedency between them, but basically `mitodl/ol-infrastructure` is a base layer for the other two.
    * These builds also kicked off (maybe? I did run them by hand too…) when the PR was merged but they didn’t publish to dockerhub before concourse started trying to update CI/QA stacks automatically.
        * EVEN IF they had published to dockerhub before, it wouldn’t matter because servers-be-cachin’.
        * The concourse workers were holding on to their old versions and efficiently re-using them, ignorant of the new versions available out on dockerhub.
        * Solve this by doing and instance refresh on concourse workers. New servers do new `docker pull` on the resources needed.
9. Addendum: Made a neat script to pause concourse pipelines en-mass. Should have run this before #8 :disappointed:
```bash
for pl in $(fly -t pr-inf ps --json | jq -r '.[].name'); do
  fly -t pr-inf pp -p $pl
done
```
