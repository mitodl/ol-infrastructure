/*
 * Prevent a shield POP from serving stale-while-revalidate content to an edge POP.
 *
 * With shielding enabled, a request path is client -> edge POP -> shield POP ->
 * origin. `stale-while-revalidate` is intended to let a *client-facing* cache
 * answer immediately from a stale object while it refreshes in the background.
 * When the shield tier does the same thing, the edge POP receives a stale object
 * that still carries a low `Age`, cannot tell it is stale, and caches it as
 * fresh for a full `s-maxage`.
 *
 * Fastly documents three ways this happens (soft purge, edge code that rewrites
 * TTLs, and conditional GETs that the shield answers with a 304 from its own
 * stale copy) and recommends this guard under "Shielding considerations":
 * https://www.fastly.com/documentation/guides/concepts/cache/stale/#shielding-considerations
 *
 * `fastly.ff.visits_this_service` counts how many Fastly nodes in this service a
 * request has already passed through, so it is 0 for a request arriving from a
 * client and > 0 for one forwarded by an edge POP -- i.e. this branch is taken
 * only when we are acting as the shield.
 *
 * `req.max_stale_while_revalidate = 0s` caps stale-while-revalidate for this
 * request, so the shield does a real fetch instead of answering from a stale
 * object. Background revalidation absorbs the extra latency: the edge POP keeps
 * its own stale-while-revalidate, so clients still get an immediate response.
 *
 * `req.max_stale_if_error` is deliberately left alone -- the shield should still
 * serve stale content when the origin is genuinely failing.
 *
 * NOTE: of the three trigger conditions, soft purge is active today -- MITxOnline
 * still soft-purges its surrogate keys (mitodl/mitxonline#3792 switches it to hard),
 * so until that lands this snippet is what keeps a soft purge from being laundered
 * back into a fresh response. The other two are inactive: nothing here rewrites
 * TTLs, and the origin sends neither `ETag` nor `Last-Modified`. Once #3792 ships
 * this becomes a guard rail, kept so that adding origin validators, rewriting TTLs,
 * or reaching for a soft purge again does not silently reintroduce
 * stale-served-as-fresh content.
 */
if (fastly.ff.visits_this_service > 0) {
  set req.max_stale_while_revalidate = 0s;
}
