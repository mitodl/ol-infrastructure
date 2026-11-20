# The Image Optimizer drops Last-Modified from the images it transforms, which
# it marks with Fastly-Io-Info, and the OCW bucket sends no Cache-Control. A
# browser would then revalidate each image on every page view. One week is what
# ocw.mit.edu sends for the same files.
#
# Only the response to the client gets it, so Fastly keeps its own TTL.
if (fastly.ff.visits_this_service == 0 &&
    resp.status == 200 &&
    resp.http.Fastly-Io-Info &&
    !resp.http.Cache-Control) {
  set resp.http.Cache-Control = "max-age=604800";
}
