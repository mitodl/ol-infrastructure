"""APISIX OIDC callback failure-rate alert rules.

Every OIDC-protected host behind the gateway finishes its login at
`/<login-path>/.apisix/redirect`.  That request either 302s on to the
application or 500s into the branded gateway error page -- there is no third
outcome -- so the 500:total ratio on that one path is a direct measure of how
much of the gateway's login traffic is erroring, which no existing rule watches.

Read that ratio as a rate over *callback requests*, not over users or login
attempts.  The two are not the same here: cause 1 below means a single
successful login can produce one 302 and several 500s, so the ratio moves with
replay volume as well as with real breakage.  It is the right thing to alert on
-- it is what the gateway is actually doing, and it responds to both failure
modes -- but it does not license a statement about how many people could not
log in.  Nothing in the access log identifies a user or ties a replay burst back
to its original login, so this data cannot answer that question at all.

Why not reuse metric_rules/apisix_edge.py: `apisix_http_status` carries
`matched_host` and `code` but no path, so the callback route cannot be isolated
from it.  The signal only exists in the access log, which is why these live in
log_rules/ despite being the same edge concern.

Measured baseline (production, 24h to 2026-08-14)
-------------------------------------------------
  host                     302     500    500 rate
  mitxonline.mit.edu      2,470    932     27.4%
  api.learn.mit.edu       2,973    502     14.4%
  nb.learn.mit.edu           79      7      8.1%

That is ~1,441 callback requests a day answered with a 21KB HTTP 500 page.  How
many *people* that represents is unknown and is certainly much smaller: per
cause 1 below, most of these 500s trail a login that already succeeded.

The 2026-07 SOA audit logged this as "~780/day state-mismatch errors, each a
user bounced back through login".  Both halves are wrong, in opposite
directions.  The count is too low because it reads the error log, where APISIX
emits two lines per failure (openidc.lua:1118 and openid-connect.lua:876), so it
double-counts a smaller number than the access log actually shows.  The
*interpretation* is too high because these are not users bounced back through
login.  The failures split into two distinct causes that the raw string count
conflates:

1. **Stale-code replay (~55%).**  The callback URL is fetched repeatedly with
   the same `state`.  The *first* fetch succeeds -- carrying the small pre-auth
   session cookie (~400B, holding state+nonce) -- and swaps in the full
   authenticated session (~3.3KB, holding access_token/id_token/user per
   `OLApisixOIDCConfig.oidc_session_contents`).  Every replay then presents
   that authenticated cookie, which has no `state` key, and lua-resty-openidc
   reports `state restored from session: nil`.  Confirmed by tracing a single
   state value end to end: one 302 at 15:13:25 with a 404-byte session cookie,
   then six 500s at 15:14--15:20 all carrying the same 3,260-byte one.  So the
   user is *not* bounced back through login -- their first attempt worked, and
   the 500 lands on a duplicate request.
2. **Keycloak error callbacks (~45%, 652/day).**  Keycloak redirects back with
   `error=temporarily_unavailable&error_description=authentication_expired`
   instead of a code, and the openid-connect plugin turns that into a 500
   rather than restarting the authorization flow.

Neither cause is a cookie-scoping problem, which is what the audit's remediation
notes assumed.  Cookie chunking was ruled out directly -- no `*_apisix_session_2`
cookie appears on any request in 24h -- so the existing
`stale_session_cookie_cleanup_plugin` is not implicated either.

These rules do not fix either cause.  They exist so the rate is watched, and so
that whatever fix lands for the two causes above can be shown to have worked.

Two windows, same shape as metric_rules/apisix_edge.py
------------------------------------------------------
  fast    -- a step change. 45% over 30m, confirmed 15m.  Deliberately set above
             the 27.4% worst-host baseline so it stays silent today and fires
             only on a clear departure from it.
  chronic -- the standing condition. 5% over 6h, confirmed 1h.  This one fires
             *right now*, on purpose, for the two hosts that clear the gate --
             api.learn.mit.edu and mitxonline.mit.edu.  It is the tracker for
             the two causes above, and it going quiet is the signal that they
             were actually fixed rather than merely re-explained.  (nb.learn is
             below the gate and so is not covered by either rule; see below.)

Minimum-traffic gate
--------------------
`_MIN_CALLBACKS` is the number of callbacks a host must serve within the same
window before its ratio counts, written as the LEFT operand of `and` so the
threshold stage in base.py::_rule_data sees the gate's value (the idiom
documented at metric_rules/apisix_edge.py:34-42).  A login route that sees three
requests a day produces a 33% ratio off one 500; the gate is what separates that
arithmetic from an outage.  It is scaled per window to hold the same rate:
20 per 30m and 240 per 6h both mean ~40 callbacks/hour.  At the volumes above
that admits mitxonline and api.learn and excludes nb.learn (~1.8 per 30m), which
is the intended trade -- nb.learn's 7 failures/day are real but too sparse to
support a ratio.

Calibration: these rules carry NO severity label
------------------------------------------------
Same deliberate choice as metric_rules/apisix_edge.py, for the same reason: the
default route in alertmanager.py is `oblivion`, so an unlabelled alert is
evaluated and recorded in `grafanacloud-alert-state-history` but delivered
nowhere.  Promote by adding `labels={"severity": "warning"}` -- and nothing else,
because these aggregate `sum by (matched_host)`, a label already present in
`NotificationPolicy.group_bies` for the apisix_edge rules.  That reuse is why
this file renames the log's `host` field to `matched_host` rather than grouping
by a new `host` label that would need adding there.

Do not promote the chronic rule before the two causes above are fixed -- it is
firing continuously by design and would page on a known condition.  The fast
rule is the one that is safe to promote today.

Measure with:
  sum by (ruleTitle) (count_over_time(
    {from="state-history"} | json | current =~ `Alerting.*` [14d]))
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting

# The OIDC callback access-log stream, reduced to one line per callback with
# `matched_host` and `status` promoted to labels.
#
# `stream="stdout"` selects the access log; APISIX writes its error log to
# stderr, and those lines match `.apisix/redirect` too (they quote the whole
# request) but are not logfmt, so they would otherwise inflate the denominator.
# The `__error__=""` filter drops anything that still fails to parse.
_CALLBACK_STREAM = (
    '{namespace="operations", container="apisix", stream="stdout"}'
    ' |= ".apisix/redirect"'
    ' | logfmt | __error__=""'
    " | label_format matched_host=host"
)

# Callbacks a host must serve within the window before its failure ratio is
# treated as meaningful. Keyed by window; both entries mean ~40 callbacks/hour.
_MIN_CALLBACKS = {"30m": "20", "6h": "240"}


def _failure_ratio_expr(window: str, threshold: str) -> str:
    """Build a gated OIDC-callback failure-ratio expression for one window.

    Returns series only for hosts that both serve real login traffic and exceed
    the failure threshold, so the rule fires per `matched_host`.
    """
    total = f"sum by (matched_host) (count_over_time({_CALLBACK_STREAM} [{window}]))"
    failed = (
        "sum by (matched_host) (count_over_time("
        f'{_CALLBACK_STREAM} | status="500" [{window}]))'
    )
    return f"{total} >= {_MIN_CALLBACKS[window]} and {failed} / {total} > {threshold}"


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create APISIX OIDC callback failure-rate alert rule groups."""
    alerting.RuleGroup(
        "loki-apisix-oidc-callback-failure-rate",
        name="apisix-oidc-callback-failure-rate",
        folder_uid=folder_uid,
        interval_seconds=300,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="APISIXOIDCCallbackFailureRateFast",
                condition="C",
                for_="15m",
                no_data_state="OK",
                exec_err_state="OK",
                # No severity label: routes to `oblivion` while calibrating.
                # See the module docstring.
                labels={},
                annotations={
                    "summary": "{{ $labels.matched_host }} OIDC callback 500 rate has jumped above 45%",
                    "description": "More than 45% of requests to {{ $labels.matched_host }}'s /.apisix/redirect callback returned a 500 over the last 30 minutes, against a measured baseline of 27% (mitxonline) and 14% (api.learn) on 2026-08-14. This is a rate over callback requests, not over users: a rise can mean the authorization code exchange is genuinely broken (check the Keycloak client's secret and redirect_uri, and the openid-connect plugin's discovery endpoint), or that the volume of one of the two chronic causes has grown -- stale-code replay, or Keycloak authentication_expired error callbacks. Separate them before assuming an outage: replays carry a `code` and a ~3KB authenticated session cookie, error callbacks carry `error=` and no code, and a genuinely broken exchange fails on the *first* callback with a small pre-auth cookie. Query the access log by request_uri and cookie_sizes to tell which.",
                },
                datas=rd(_failure_ratio_expr("30m", "0.45")),
            ),
            alerting.RuleGroupRuleArgs(
                name="APISIXOIDCCallbackFailureRateChronic",
                condition="C",
                for_="1h",
                no_data_state="OK",
                exec_err_state="OK",
                labels={},
                annotations={
                    "summary": "{{ $labels.matched_host }} OIDC callback 500 rate has been over 5% for hours",
                    "description": "More than 5% of requests to {{ $labels.matched_host }}'s /.apisix/redirect callback returned a 500 over the last 6 hours. This rule is expected to be firing until the two known causes are fixed -- stale-code replay against an already-authenticated session, and Keycloak authentication_expired error callbacks being turned into 500s -- and its going quiet is the check that a fix actually worked. See the module docstring for the measurements behind both.",
                },
                datas=rd(_failure_ratio_expr("6h", "0.05")),
            ),
        ],
        opts=resource_opts,
    )
