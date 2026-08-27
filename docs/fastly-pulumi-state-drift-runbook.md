# Fastly / Pulumi state-drift repair runbook

What to do when `pulumi up` reports no changes but the live Fastly service is
missing configuration that the code clearly declares.

## The symptom

You merge a change that adds a VCL snippet (or any other named child object) to
a `fastly.ServiceVcl`. The deploy pipeline goes green. The feature is not live.
You run `pulumi up` again, by hand, and Pulumi tells you there is nothing to do
— while the Fastly UI plainly does not have your snippet.

Nothing is broken enough to alarm. Every subsequent deploy will also be green,
and the feature will stay missing indefinitely. This has happened twice:

| Incident | What was illegal | Blast radius |
| --- | --- | --- |
| ol-infrastructure#5513 | `/` in `Handle course/program redirects to MIT Learn` | mitxonline redirect absent from QA and Production for days |
| ol-infrastructure#5563 | `(` `)` in `... fetch (miss)` / `... (pass)` | mit-learn cache-key whitelist absent from every environment |

In the second case mit-learn CI build 610 reported SUCCESS while creating none
of the snippets. A green build is not evidence that the config is live.

## Why it happens

Fastly's snippet endpoint requires a name to start with a letter and contain
only alphanumeric, underscore, hyphen, period, and space characters. An illegal
name is rejected with a 400:

```
Name must start with a letter and contain only alphanumeric, underscore,
hyphen, period, and space characters.
```

That rejection arrives **mid-apply**, after Pulumi has already cloned a new
Fastly service version. The failed run nonetheless persists the full *desired*
`snippets` set into the resource's state outputs — including the snippet that
was never created.

From then on the state is lying, and the provider's diff logic believes it.
`SetDiff.Diff()` keys set elements on `name` and computes
`unmodified := oldSet.Intersection(newSet)`. `Process()` only ever iterates the
Deleted, Added and Modified buckets. An element that is byte-identical between
the lying state and the config lands in Unmodified, so it generates no API call
— forever. No amount of re-running `pulumi up` will heal it, because as far as
Pulumi is concerned there is nothing to heal.

This is why the repair below leads with a refresh. **Refresh is the only thing
that can break the tie**, because the provider's snippet `Read()` re-lists the
children from the live active version and overwrites the fiction in state.

## Prevention (already in place)

`vcl_snippet()` in `src/ol_infrastructure/lib/fastly.py` validates the name and
raises during `pulumi preview`, before any version is cloned. All snippet
definitions in the repo go through it, and
`tests/ol_infrastructure/lib/test_fastly.py` fails CI if a raw
`fastly.ServiceVclSnippetArgs` or an illegal literal name reappears.

The validator is deliberately scoped to **snippets only**. Fastly validates
conditions and request settings more loosely, and we have live ones that this
pattern would reject — xpro has a condition named
`path starts with /images cache condition`, and mit-learn a request setting
containing a comma. Do not widen the rule to those endpoints.

## Detecting it

There is currently no automated detection. Every Fastly-bearing stack
(mitxonline, micromasters, xpro, learn-ai, mit-learn, ocw-site,
fastly-redirector, edxapp) runs with `refresh_stack=False`, precisely *because*
it has Fastly resources — see the next section. Coverage is zero exactly where
the risk lives.

To check a service by hand, compare the names in state against the names live.
This needs no admin credential and cannot mutate anything:

```bash
# State side -- what Pulumi believes exists.
cd src/ol_infrastructure/applications/mit_learn
pulumi stack export --stack CI | python3 -c '
import json, sys
for r in json.load(sys.stdin)["deployment"]["resources"]:
    if r["type"] == "fastly:index/serviceVcl:ServiceVcl":
        o = r["outputs"]
        print("service", o["id"], "active version", o["activeVersion"])
        for s in sorted(o.get("snippets", []), key=lambda x: x["name"]):
            print(" ", s["name"])
'

# Live side -- what Fastly actually serves. Use the read-only token.
curl -s -H "Fastly-Key: $FASTLY_READ_KEY" \
  "https://api.fastly.com/service/<service-id>/version/<active-version>/snippet" \
  | python3 -c 'import json,sys; [print(" ", s["name"]) for s in sorted(json.load(sys.stdin), key=lambda x: x["name"])]'
```

Use `global_read_api_key` from `src/bridge/secrets/fastly.yaml`, not
`admin_api_key`. A name present in the first list and absent from the second is
this bug.

Do **not** try to detect this with `pulumi preview --refresh` and an
"any diff is drift" rule. It does not work in practice:

- The refresh is safe — verified on `ol-application-mit-learn/CI`, where the
  canonicalised state export was byte-identical before and after. But
- on that stack 38 resources reported refresh-diffs and only one was Fastly;
  the rest was routine Kubernetes churn (VaultStaticSecret, VPA, KEDA
  ScaledObject status, and so on), and
- the single Fastly diff was 189 lines of pure noise even with no real drift:
  `backends` and `loggingHttps` drop and re-add because they contain `[secret]`
  members and the whole collection flips to secret on refresh, and
  `requestSettings` gains a provider-default `maxStaleAge: 0`. `snippets` did
  not appear at all — because they matched.

The signal is inverted: the noise is always there and the thing you care about
is invisible inside it. Compare name sets, not diffs.

## Why refresh is off on these pipelines

`refresh_stack=False` is set on every Fastly-bearing pipeline because a refresh
calls the Fastly API, and during an admin API token rotation it calls it with
the stale token and fails the entire deploy job. Turning refresh back on is
tracked separately; until then, refresh is a deliberate manual step, which is
what makes this runbook necessary.

## The repair

Validated on both QA and Production during hq#12449 (mitxonline QA v563→v564,
Production v207→v208) and again on mit-learn (QA v1435→v1437, CI v1902→v1904).

Work one stack at a time, and target the Fastly resource explicitly so an
unrelated in-flight change cannot ride along.

**1. Get the URN of the Fastly service.**

```bash
cd src/ol_infrastructure/applications/<app>
pulumi stack --stack <STACK> --show-urns | grep serviceVcl
```

It looks like:

```
urn:pulumi:CI::ol-application-mit-learn::fastly:index/serviceVcl:ServiceVcl::fastly-mit_learn-ci
```

**2. Refresh just that resource.** This rewrites state from the live service and
is the step that actually breaks the deadlock.

```bash
pulumi refresh --yes --stack <STACK> --target '<urn>'
```

`pulumi refresh` does not execute the Pulumi program, so it needs none of the
image environment variables the program demands. In the refresh output, a
`backends: [secret]` line is a state-*representation* change, not a config
change — ignore it.

**3. Verify state now matches live.** Re-run the two commands from *Detecting
it*. State should now be missing the snippet too. That is the point: state has
stopped lying, so the next `up` has real work to do.

**4. Preview, and gate on what you see.**

```bash
pulumi preview --diff --stack <STACK> --target '<urn>'
```

You should see the missing snippet being **created** and nothing else of
substance. If you see anything you did not expect — especially a deletion —
stop and work out why before applying.

The program *does* run for preview and `up`, and it refuses to start without
`<APP>_DOCKER_TAG` or `<APP>_DOCKER_SHA`:

```
OSError: Either MIT_LEARN_DOCKER_TAG or MIT_LEARN_DOCKER_SHA must be set.
```

Read the currently-deployed value out of the stack rather than inventing one,
otherwise an unrelated image roll rides along with your Fastly fix. Read it off
the running Deployments, not with a bare grep of the export — the export also
contains superseded digests from earlier revisions, and a grep returns all of
them with no way to tell which is live:

```bash
pulumi stack export --stack <STACK> | python3 -c '
import json, sys
for r in json.load(sys.stdin)["deployment"]["resources"]:
    if r["type"] == "kubernetes:apps/v1:Deployment":
        for c in r["outputs"]["spec"]["template"]["spec"]["containers"]:
            print(r["outputs"]["metadata"]["name"], c["image"])
' | sort -u
```

All of an app's Deployments normally share one digest. Pass the digest part,
e.g. `MIT_LEARN_DOCKER_SHA=sha256:... pulumi preview ...`.

**5. Apply.**

```bash
pulumi up --yes --stack <STACK> --target '<urn>'
```

**6. Validate against the live service, not against Pulumi.** Allow roughly a
minute for Fastly edge propagation first. Confirm the active version number
advanced and the snippet is present, then exercise the behaviour end to end.
For a cache-key change that means checking that a non-whitelisted query
parameter collapses to one cache key while a whitelisted one still splits:

```bash
for i in 1 2 3; do
  curl -so /dev/null -D - "https://learn.mit.edu/search?q=python&utm_source=t$i" \
    | grep -iE '^(x-cache|age):'
  sleep 1
done
# expect MISS, then HIT with a rising age -- one shared key
```

## Rollback

Fastly versions are immutable, so rollback is instant and does not involve
Pulumi: reactivate the previous version in the Fastly UI or API. **Then run
step 2 again** — otherwise Pulumi's state now describes the version you just
rolled away from, and you have re-created the same lying-state condition by
hand.
