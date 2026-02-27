/*
 * Perform redirects with dictionary lookups
 */
declare local var.redirect STRING;
declare local var.location STRING;
declare local var.path_prefix STRING;

# Prefix redirects: strip to first path segment, look up in prefix_redirects dictionary,
# then rebuild the URL replacing only that segment.
set var.path_prefix = regsub(req.url.path, "^(/[^/]*).*$", "\1");
if (table.lookup(prefix_redirects, var.path_prefix)) {
  set var.location = table.lookup(prefix_redirects, var.path_prefix) + regsub(req.url.path, "^/[^/]*", "");
  if (std.strlen(req.url.qs) > 0) {
    set var.location = var.location "?" + req.url.qs;
  }
  set req.http.redirect_dest = var.location;
  error 601 "## path redirect";
}

if (table.lookup(path_redirects, req.url)) {
  set var.redirect = table.lookup(path_redirects, req.url);
  if (std.strlen(req.url.qs) > 0) {
    set var.location = var.redirect "?" + req.url.qs;
  } else {
    set var.location = var.redirect;
  }
  set req.http.redirect_dest = var.location;

  error 601 "## path redirect";
}

# if (req.url.path ~ "^/attach/(.*$)") {
#   set var.location = regsub(req.url, "/attach/", "/enrollmentcode/");
#   set req.http.redirect_dest = var.location;

#   error 601 "## path redirect";
# }
