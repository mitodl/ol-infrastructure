
/*
 * Perform redirects with dictionary lookups
 */
declare local var.redirect STRING;
declare local var.location STRING;

set var.redirect = table.lookup(redirects, req.url.path);

if (var.redirect) {

  if (std.strlen(req.url.qs) > 0) {
    set var.location = var.redirect "?" + req.url.qs;
  } else {
    set var.location = var.redirect;
  }
  set req.http.redirect_dict = var.location;

  error 601 req.http.redirect_dict;
}
