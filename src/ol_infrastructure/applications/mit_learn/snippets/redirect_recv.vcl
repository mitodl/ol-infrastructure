/*
 * Perform redirects with dictionary lookups
 */
declare local var.redirect STRING;
declare local var.location STRING;

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
