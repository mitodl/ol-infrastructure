/*
 * MIT Learn's Next.js origin sees dozens of query parameters that never
 * change the response it renders -- analytics/tracking params, and a
 * firehose of stale Drupal facet params arriving via a retired redirect
 * -- but Fastly's default cache key includes the full querystring. Every
 * irrelevant param variant fragments the cache into its own object, so
 * equivalent requests keep missing origin instead of collapsing to one
 * cached response.
 *
 * This narrows the *cache key* to a whitelist of query params that
 * actually affect what the origin sends. req.url itself is left
 * untouched, so the origin still receives the request exactly as the
 * client sent it (some redirect routes, e.g. `playlist`, depend on
 * seeing their original query string).
 *
 * The whitelist itself lives in __main__.py as
 * CACHE_KEY_QUERY_PARAM_WHITELIST (substituted into the whitelist_expr
 * placeholder below) rather than here, so it's a real Python list -- see
 * that constant's docstring for provenance and how it maps to mit-learn's
 * SERVER_KEYED_PARAMS. See also https://github.com/mitodl/hq/issues/12925
 * for the traffic analysis behind this change.
 */
declare local var.cache_key_url STRING;
set var.cache_key_url = req.url;

# Leave Next.js's own asset requests alone -- they're already
# content-hashed, so query params there aren't cache-key noise.
if (req.url.path !~ "^/_next/") {
  set var.cache_key_url = querystring.filter_except(
    var.cache_key_url,
    ${whitelist_expr}
  );

  # _rsc's value is a per-navigation token (133 distinct values seen in a
  # 5-minute sample); only whether it's present distinguishes a React
  # Server Component response from full HTML, so normalize the value out
  # of the cache key. It can arrive bare (`?_rsc`, no `=`), so detect
  # presence against the raw querystring rather than requiring `_rsc=`.
  if (req.url.qs ~ "(^|&)_rsc(=|&|$$)") {
    set var.cache_key_url = querystring.set(var.cache_key_url, "_rsc", "1");
  }

  set var.cache_key_url = querystring.sort(var.cache_key_url);
}

set req.http.X-Cache-Key-Url = var.cache_key_url;
