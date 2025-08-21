# Perform redirect for chalkradiopodcast
if (req.http.host == "chalkradiopodcast.net" || req.http.host == "chalkradiopodcast.com" || req.http.host == "chalkradiopodcast.org") {
  error 600 req.http.host;
}

# Perform redirect for starcellbio
if (req.http.host == "starcellbio.mit.edu") {
  error 603 req.http.host;
}

# Perform redirect for mitxpro
declare local var.last_path_part STRING;
if (req.http.host == "certificates.mitxpro.mit.edu") {
  if (req.url.path ~ "^/credentials/(.*?)$") {
    set req.http.cert_redirect = "https://certificates.mitxpro.mit.edu/program/" + re.group.1 + ".pdf";
    error 602 req.http.cert_redirect;
  }
}

if (req.http.host == "mitxpro.mit.edu") {
  if (req.url.path ~ "^/credentials/(.*?)/(.*?)$") {
    set req.http.cert_redirect = "https://certificates.mitxpro.mit.edu/program/" + re.group.2 + ".pdf";
    error 602 req.http.cert_redirect;
  }
  if (req.url.path ~ "^/certificates/(.*)$") {
    set req.http.cert_redirect = "https://certificates.mitxpro.mit.edu/course/" + re.group.1 + ".pdf";
    error 602 req.http.cert_redirect;
  }
  error 601 req.http.host;
}

/*
 * Perform redirects with dictionary lookups
 */
declare local var.redirect STRING;
declare local var.location STRING;
declare local var.url STRING;
declare local var.status INTEGER;

set var.redirect = table.lookup(redirects, req.url.path);

if (var.redirect ~ "^([0-9]{3})\|([^|]*)\|(.*)$") {

  set var.status = std.atoi(re.group.1);

  if (std.strlen(req.url.qs) > 0) {
    set var.location = re.group.3 + if(re.group.2 == "keep", "?" + req.url.qs, "");
  } else {
    set var.location = re.group.3;
  }
  set var.location = regsub(var.location, "{{AK_HOSTHEADER}}", req.http.host);
  set var.location = regsub(var.location, "{{PMUSER_PATH}}", "");
  set req.http.redirect_dict = var.location;


  error 301 req.http.redirect_dict;
}

# Redirect bare directory names to add a trailing slash
if (req.url.path ~ "/([^/]+)$") {               // ends with non-slash character
  set var.location = re.group.1;
  if (var.location !~ "\.[a-zA-Z0-9]{1,4}$") {  // is directory, not file w. extension
    set req.http.slash_header = req.url.path + "/";
    if (std.strlen(req.url.qs) > 0) {
      set req.http.slash_header = req.http.slash_header + "?" + req.url.qs;
    }
    error 301 req.http.slash_header;
  }
}
