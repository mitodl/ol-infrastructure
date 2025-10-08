
/*
 * Perform redirects with dictionary lookups
 */
declare local var.redirect STRING;
declare local var.location STRING;

set var.redirect = table.lookup(path_redirects, req.url.path);

if (var.redirect) {

  if (std.strlen(req.url.qs) > 0) {
    set var.location = var.redirect "?" + req.url.qs;
  } else {
    set var.location = var.redirect;
  }
  set req.http.redirect_dest = var.location;

  error 601 req.http.redirect_dest;
}

if (req.url.path ~ "^/attach/(.*$)") {
  set var.location = regsub(req.url, "/attach/", "/enrollmentcode/");
  set req.http.redirect_dest = var.location;

  error 601 req.http.redirect_dest;
}
