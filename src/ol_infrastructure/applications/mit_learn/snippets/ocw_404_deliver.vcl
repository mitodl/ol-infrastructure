# Fetch the Learn 404 page only at the client-facing edge
if (fastly.ff.visits_this_service == 0 &&
    (req.method == "GET" || req.method == "HEAD") &&
    req.http.orig-req-url ~ "^/courses/o/") {
  if (req.restarts == 0 && resp.status == 404) {
    # Fetch HTML instead of Next.js component data
    unset req.http.RSC;
    unset req.http.Next-Router-State-Tree;
    unset req.http.Next-Router-Prefetch;
    unset req.http.Next-Router-Segment-Prefetch;

    set req.url = "/not-found";
    restart;
  } else if (req.restarts == 1 && req.url == "/not-found") {
    # Preserve the requested URL in access logs
    set req.url = req.http.orig-req-url;
  }
}
