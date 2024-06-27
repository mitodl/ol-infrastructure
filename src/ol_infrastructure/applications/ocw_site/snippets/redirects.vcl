declare local var.location STRING;
declare local var.status INTEGER;
declare local var.last_path_part STRING;

# Perform redirects with dictionary lookups
declare local var.redirect STRING;
declare local var.url STRING;

set var.url = regsub(req.url.path, "/$", "");
set var.redirect = table.lookup(redirects, var.url);

if (var.redirect ~ "^\s*?([0-9]{3})\|([^|]*)\|(.*)$") {

  set var.status = std.atoi(re.group.1);

  if (std.strlen(req.url.qs) > 0) {
    set var.location = re.group.3 + if(re.group.2 == "keep", "?" + req.url.qs, "");
  } else {
    set var.location = re.group.3;
  }
  set var.location = regsub(var.location, "{{AK_HOSTHEADER}}", req.http.host);
  set var.location = regsub(var.location, "{{PMUSER_PATH}}", "");


  error 307 var.location;
}

# Redirect bare directory names to add a trailing slash
if (req.url.path !~ "^/static" && req.url.path ~ "/([^/]+)$") {               // ends with non-slash character
  set var.last_path_part = re.group.1;
  if (var.last_path_part !~ "\.[a-zA-Z0-9]+$") {  // is directory, not file w. extension
    set req.http.slash_header = req.url.path + "/";
    if (std.strlen(req.url.qs) > 0) {
      set req.http.slash_header = req.http.slash_header + "?" + req.url.qs;
    }
    error 301 req.http.slash_header;
  }
}

# OCW Legacy Department Redirects
if (req.url.path ~ "(/courses/)([\w-]+)/(.*)/") {
  if (table.lookup_bool(departments, re.group.2, false)) {
    if (std.strlen(re.group.4) > 0) {
      set req.http.header_sans_department = re.group.1 + re.group.3 + "/" + re.group.4;
    } else {
      set req.http.header_sans_department = re.group.1 + re.group.3 + "/";
    }
    error 601 "redirect";
  }
}

# OCW Legacy /resources/res-* to /courses/ redirect
if (req.url.path ~ "(^/resources/res-*)") {
  set req.http.courses_instead_resources = regsub(req.url.path, "/resources/", "/courses/");
  error 604 "redirect";
}

# OCW Legacy remove index.htm
if (req.url.path ~ "/index.htm$") {
  set req.http.remove_index_htm = regsub(req.url.path, "/index.htm", "/");
  error 605 "redirect";
}

# OCW Legacy /high-school/ to zendesk article
if (req.url.path ~ "(^/high-school*)") {
  set req.http.high_school_to_article = "https://mitocw.zendesk.com/hc/en-us/articles/5332864282907";
  error 606 "redirect";
}
