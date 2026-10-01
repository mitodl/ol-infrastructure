# Runbook: partner-SSO "account already exists" / federated identity mismatch

## Symptom

A learner logs into MIT Learn through a partner's SSO (e.g. a university's
own Keycloak/Shibboleth IdP, brokered in our `olapps` realm). Authentication
succeeds on the partner's side, but instead of landing back on MIT Learn
logged in, they see Keycloak's first-broker-login screen:

> User with email `<email>` already exists. How do you want to continue?

Selecting "Add to existing account" prompts an email verification step, and
after completing it the user is often shown:

> Your account is already linked to the identity provider `<Partner>`.

...and the loop repeats, or they're told to confirm the link but the flow
never actually completes.

Partner IT teams reporting this ticket will usually already tell you their
side of the OIDC exchange completed successfully (`LOGIN` → `CODE_TO_TOKEN`
→ `USER_INFO_REQUEST` in their own logs) and ask you to check the identity
broker on our end.

## Root cause

Keycloak links a broker identity provider account to a local user via
`federated_identity.federated_user_id` — the external IdP's OIDC `sub` claim
(or, for SAML, the NameID/principal attribute), captured the first time that
user linked their account. If the partner's IdP ever re-issues that subject
identifier for the same human — most commonly because they added or
recreated a User Federation provider (e.g. LDAP) on their own Keycloak
realm — every wrapped user gets a new `sub`, even though nothing changed
from the user's point of view.

When that happens, Keycloak sees the *new* subject as a brand-new,
never-before-seen brokered identity. Since the email already belongs to an
existing local account, it routes into the first-broker-login "this email
already exists" flow instead of a normal login — and that flow is fragile on
this stack, so it also tends to loop/fail before actually completing.

A concrete tell: real-world `sub` values that start with `f:<uuid>:` (e.g.
`f:00000000-0000-0000-0000-000000000000:SOME.USERNAME`) are Keycloak's own composite ID
format for a *federated* user (one wrapped by a User Federation provider on
the partner's realm), as opposed to a bare UUID for a locally-managed user on
their side. Seeing the format itself change between what's stored and what
the partner now reports is strong confirmation of this root cause.

## Diagnosis

Use `bin/keycloak-federated-identity-lookup lookup` to compare what's stored
against what the partner currently reports:

```bash
uv run python bin/keycloak-federated-identity-lookup lookup \
  <learner-email> \
  --client-id "$(vault kv get -field=client_id secret-operations/sso/mitlearn-admin)" \
  --client-secret "$(vault kv get -field=client_secret secret-operations/sso/mitlearn-admin)" \
  --expected-external-id <external-id-the-partner-reported> \
  --identity-provider <idp-alias>
```

If it reports `MISMATCH`, that confirms this root cause. If it reports
`MATCH` (identity is already correctly linked) and the learner is still
failing, **stop here and investigate differently** — this is not that bug.
In that case, check whether the request is reaching us at all.

Prefer Grafana Loki over `kubectl logs` for this check: Keycloak runs as 3
replicas behind a Service with no session affinity (see the "Known related
issue" section below), so a learner's requests can land on any pod, and
`kubectl logs` only covers each pod's lifetime since its last restart (often
much shorter than the window you need to check). Query all pods over the
actual time range in question:

```logql
{namespace="keycloak", container="keycloak"} |= "<learner-email-or-user-id>"
```

If you only have `kubectl` access, you must check **every** pod explicitly —
checking just one and treating a miss as conclusive is a false negative
waiting to happen, for the exact cross-pod reason this runbook exists:

```bash
for pod in keycloak-production-0 keycloak-production-1 keycloak-production-2; do
  kubectl --context operations-production -n keycloak logs "$pod" --since=2h \
    | grep -i '<learner-email-or-user-id>'
done
```

No hits at all (not even an error) across every Keycloak pod means the
redirect back to MIT never arrived — the break is upstream of us (partner
side, network, or the learner's browser), not a Keycloak identity mismatch.
Ask the partner to confirm the exact timestamp of the attempt and whether
they can see the redirect to `https://sso.ol.mit.edu/realms/olapps/broker/<idp-alias>/endpoint`
actually fire on their end.

## Remediation

Once a mismatch is confirmed and the partner has told you the current,
correct external ID, re-point the link with `relink`:

```bash
# Dry run first (default — nothing is changed without --confirm)
uv run python bin/keycloak-federated-identity-lookup relink \
  <learner-email> \
  --client-id "$(vault kv get -field=client_id secret-operations/sso/mitlearn-admin)" \
  --client-secret "$(vault kv get -field=client_secret secret-operations/sso/mitlearn-admin)" \
  --identity-provider <idp-alias> \
  --new-external-id <new-external-id>

# Then apply it
... --confirm
```

This deletes and re-adds the broker link via the Admin API — it only
touches the identity link, not the local user record, so enrollments and
course progress are untouched.

Importantly, this **also sidesteps the fragile first-broker-login flow
entirely** for the learner's next attempt: Keycloak only runs that multi-step
"confirm existing account" flow when there's no existing link to find. Once
the link is already correct before they try again, their next login is an
ordinary single-request broker `LOGIN`, not the flow that's been failing.

## Known related issue: this can recur

Patching one learner's stored ID is not necessarily durable if the partner's
identity source keeps changing (e.g. a recurring resync process on their
end). If the same learner or others at the same partner keep hitting this
after being relinked, that's a sign to push the partner for the underlying
cause (is their federation link expected to be stable? what triggers it to
regenerate?) rather than treating each recurrence as an independent
one-off fix.

Separately, there's a suspected contributing infrastructure issue: the
Keycloak Operator CR sets
`spi-sticky-session-encoder-infinispan-should-attach-route: "false"`
(`src/ol_infrastructure/applications/keycloak/__main__.py`), and the Gateway
API route in front of the 3-replica Keycloak Service has no session-affinity
configuration. Multi-step broker flows (like first-broker-login) can bounce
across pods mid-flow as a result. This is a plausible independent
contributor to why the "confirm existing account" flow fails even when
someone does need to go through it, and is worth fixing regardless of any
specific partner's identity-mismatch incident.
