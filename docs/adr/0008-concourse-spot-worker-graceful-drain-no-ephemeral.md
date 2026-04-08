# 0008. Concourse Spot Worker Graceful Drain Without Ephemeral Mode

**Status:** Accepted
**Date:** 2026-04-08
**Deciders:** Platform Team
**Technical Story:** Spot-terminated Concourse workers leaving stale registrations on web nodes

## Context

### Current Situation

MIT Open Learning runs Concourse CI workers on AWS spot instances to reduce compute costs.
Workers are managed in EC2 Auto Scaling Groups. Prior to this change, spot terminations were
leaving stale worker registrations on the web nodes because the workers had no mechanism to
cleanly unregister before being terminated.

### Problem Statement

When a spot instance is reclaimed by AWS, the Concourse worker process is hard-terminated
without going through the retirement flow (SIGUSR2 → drain → unregister). This leaves the
web node with a stale worker entry in `stalled` state. Over time, stalled workers accumulate
and clutter the worker list. Additionally, scale-in events from the ASG have the same problem.

The secondary concern was whether enabling `ephemeral` workers could serve as a self-cleaning
mechanism: an ephemeral worker that disappears would be removed from the web node automatically
rather than sitting stalled indefinitely.

### Options Considered

#### Option A: `ephemeral=True` (rejected)

Marking workers as ephemeral causes the web node to **immediately remove** the worker
registration when the worker misses heartbeats and is marked `stalled`. This would auto-clean
dead workers without any additional machinery.

**Why this was rejected:**

The Concourse team itself discovered and documented this problem in
[issue #2719](https://github.com/concourse/concourse/issues/2719). Under high load, the
worker's TSA heartbeat goroutine can be delayed by CPU/IO pressure from running builds. The
default heartbeat interval is 30 s; after approximately two missed intervals (~60 s) the web
node marks the worker stalled.

With `ephemeral=True`, a stalled worker is not merely flagged — the TSA closes the connection
and **the worker process exits**, behaving identically to `concourse retire-worker`. This
terminates every build that was running on the worker at that moment.

Concourse's own lead maintainer (`vito`) filed the issue and the resolution was to
**remove `ephemeral=true` from their own docker-compose.yml**:

> *"This can be closed. The `docker-compose.yml` no longer configures the workers to be
> ephemeral, so they should just stall instead of dropping out."*

A contributor confirmed:
> *"Dropping the `- CONCOURSE_EPHEMERAL=true` flag in `docker-compose.yml` results in test
> failures, however, the worker doesn't disappear."*

For VM-based workers running many concurrent builds (our use case), a 60 s heartbeat gap
under load is plausible. Choosing `ephemeral=True` would trade minor stalled-worker clutter
for unpredictable mid-build failures — an unacceptable tradeoff.

A secondary risk was identified in
[issue #2827](https://github.com/concourse/concourse/issues/2827): if a worker dies and
restarts with the same name before the garbage collector runs, the old ephemeral registration
is not cleaned up. This is low risk for EC2 instances (each new instance gets a unique
hostname) but reinforces that the ephemeral mechanism is fragile.

#### Option B: Graceful drain with lifecycle hooks (chosen)

Instrument the worker AMI and ASG to drive the normal Concourse retirement flow
(SIGUSR2 → drain → unregister) before the instance disappears.

Three complementary mechanisms:

1. **Spot interruption watcher** (`concourse-worker-spot-watch` systemd service)
   Polls EC2 IMDS every 5 s for `spot/termination-time`. When a 2-minute termination
   notice is detected, it calls the drain script immediately.

2. **ASG lifecycle hook handler** (`concourse-worker-lifecycle-hook` systemd service)
   Polls `autoscaling:DescribeAutoScalingInstances` every 10 s. When the instance enters
   `Terminating:Wait`, it runs the drain script (9-minute timeout) then calls
   `autoscaling:CompleteLifecycleAction CONTINUE` to release the hold.

3. **Drain script** (`concourse-worker-drain`)
   Sends SIGUSR2 to the `concourse` process and waits (up to `$DRAIN_TIMEOUT` seconds)
   for the PID to exit. The existing `concourse.service` systemd unit already uses
   `KillSignal=SIGUSR2` and `TimeoutStopSec=300`, so this extends that same mechanism.

On the web side, the ASG lifecycle hooks are provisioned via Pulumi
(`pulumi_aws.autoscaling.LifecycleHook`) with a 10-minute heartbeat timeout and
`default_result=CONTINUE`, ensuring AWS does not wait forever if a hook is missed.

`connection_drain_timeout="5m"` is set in the Concourse worker config to reduce the
default 1-hour drain window and bound how long a retiring worker can hold up a build.

### Why Not a Periodic `fly prune-worker` Cron

A cron job using `fly prune-worker` could clean up stalled workers from outside the
cluster. This was considered but deprioritised because:

- It requires embedding Concourse credentials in a scheduled job
- It runs on a fixed schedule, so there is always a window where stalled workers are
  visible
- It adds operational complexity (credential rotation, failure alerting)

The lifecycle hook approach removes workers *immediately* upon clean termination, which
covers the vast majority of cases. Hard-terminated workers (e.g., AWS force-reclaim
without a two-minute notice) will still leave stalled entries, but these are harmless
(no work is scheduled on them) and can be pruned manually with `fly prune-worker` if
needed.

## Decision

**Use graceful drain via systemd services, spot-interruption IMDS polling, and ASG
lifecycle hooks. Do not set `ephemeral=True` on workers.**

## Consequences

### Positive

- Builds are not killed by transient heartbeat delays or load spikes
- Workers cleanly unregister on normal ASG scale-in and spot reclaim events
- The web node worker list stays clean for the large majority of terminations
- No Concourse credentials required in the cleanup path

### Negative

- Workers that are hard-terminated (no 2-minute notice, no lifecycle hook) will leave
  a stalled entry until manually pruned
- `awscli` must be installed on worker instances for the lifecycle hook script
- IAM policy requires `autoscaling:DescribeAutoScalingInstances`,
  `autoscaling:DescribeLifecycleHooks`, and `autoscaling:CompleteLifecycleAction`
  on `Resource: "*"` (could be tightened with ASG name prefix if desired)

### Neutral

- `connection_drain_timeout="5m"` means a retiring worker will force-stop long-running
  builds after 5 minutes; previously the timeout was 1 hour. Tune as needed.

## Related Decisions

- [0001 - Use ADR for Architecture Decisions](0001-use-adr-for-architecture-decisions.md)

## References

- [Concourse issue #2719 — Ephemeral workers exit under load](https://github.com/concourse/concourse/issues/2719)
- [Concourse issue #2827 — Race condition with same-name worker restart](https://github.com/concourse/concourse/issues/2827)
- [Concourse issue #629 — Worker lifecycle design doc](https://github.com/concourse/concourse/issues/629)
- [Concourse docs — Running a Worker](https://concourse-ci.org/docs/install/running-worker/)
- [AWS EC2 Spot Instance interruption notices (IMDS)](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-interruptions.html)
- [AWS ASG lifecycle hooks](https://docs.aws.amazon.com/autoscaling/ec2/userguide/lifecycle-hooks.html)
