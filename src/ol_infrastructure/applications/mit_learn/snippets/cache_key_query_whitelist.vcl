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
 * See https://github.com/mitodl/hq/issues/12925 for the traffic analysis
 * and the whitelist's provenance -- 28 of these 38 names come from
 * @mitodl/course-search-utils's resourceSearchValidators in the
 * mit-learn frontend repo, not from anything here, so a version bump
 * there can change the real list with no diff in this file.
 */
declare local var.cache_key_url STRING;
set var.cache_key_url = req.url;

# Leave Next.js's own asset requests alone -- they're already
# content-hashed, so query params there aren't cache-key noise.
if (req.url.path !~ "^/_next/") {
  set var.cache_key_url = querystring.filter_except(
    var.cache_key_url,
    "_rsc" +
    querystring.filtersep() +
    "q" +
    querystring.filtersep() +
    "sortby" +
    querystring.filtersep() +
    "resource_type" +
    querystring.filtersep() +
    "department" +
    querystring.filtersep() +
    "level" +
    querystring.filtersep() +
    "platform" +
    querystring.filtersep() +
    "offered_by" +
    querystring.filtersep() +
    "topic" +
    querystring.filtersep() +
    "certification" +
    querystring.filtersep() +
    "professional" +
    querystring.filtersep() +
    "certification_type" +
    querystring.filtersep() +
    "resource_category" +
    querystring.filtersep() +
    "resource_type_group" +
    querystring.filtersep() +
    "delivery" +
    querystring.filtersep() +
    "free" +
    querystring.filtersep() +
    "course_feature" +
    querystring.filtersep() +
    "ocw_topic" +
    querystring.filtersep() +
    "aggregations" +
    querystring.filtersep() +
    "search_mode" +
    querystring.filtersep() +
    "dev_mode" +
    querystring.filtersep() +
    "id" +
    querystring.filtersep() +
    "limit" +
    querystring.filtersep() +
    "offset" +
    querystring.filtersep() +
    "slop" +
    querystring.filtersep() +
    "min_score" +
    querystring.filtersep() +
    "max_incompleteness_penalty" +
    querystring.filtersep() +
    "content_file_score_weight" +
    querystring.filtersep() +
    "yearly_decay_percent" +
    querystring.filtersep() +
    "resource" +
    querystring.filtersep() +
    "page" +
    querystring.filtersep() +
    "vector_search" +
    querystring.filtersep() +
    "playlist" +
    querystring.filtersep() +
    "t" +
    querystring.filtersep() +
    "token" +
    querystring.filtersep() +
    "error_code" +
    querystring.filtersep() +
    "content_type" +
    querystring.filtersep() +
    "next"
  );

  # _rsc's value is a per-navigation token (133 distinct values seen in a
  # 5-minute sample); only whether it's present distinguishes a React
  # Server Component response from full HTML, so normalize the value out
  # of the cache key. It can arrive bare (`?_rsc`, no `=`), so detect
  # presence against the raw querystring rather than requiring `_rsc=`.
  if (req.url.qs ~ "(^|&)_rsc(=|&|$)") {
    set var.cache_key_url = querystring.set(var.cache_key_url, "_rsc", "1");
  }

  set var.cache_key_url = querystring.sort(var.cache_key_url);
}

set req.http.X-Cache-Key-Url = var.cache_key_url;
