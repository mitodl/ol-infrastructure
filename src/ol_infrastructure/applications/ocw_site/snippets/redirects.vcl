declare local var.location STRING;
declare local var.redirect STRING;
declare local var.path STRING;
declare local var.status INTEGER;
declare local var.query STRING;

# Send public v3 course URLs to their canonical Learn host
set var.location = table.lookup(course_v3_redirects, req.http.host, "");
if (var.location != "" && req.url.path ~ "^/(ocw-course-v3/courses|courses/o)/") {
  set var.location = var.location +
    regsub(req.url, "^/(ocw-course-v3/courses|courses/o)/", "/courses/o/");
  error 301 var.location;
}

# OCW Legacy /high-school/ to zendesk article
if (req.url.path ~ "^/high-school") {
  error 301 "https://mitocw.zendesk.com/hc/en-us/articles/5332864282907";
}

# Dictionary entries are "<status>|<keep|discard query string>|<target>". Keys
# are lowercase with no trailing slash or /index.htm, so normalize the same way.
set var.path = std.tolower(regsub(req.url.path, "(/index\.htm|/)$", ""));
if (var.path != "") {
  set var.redirect = table.lookup(redirects, var.path, "");
  if (var.redirect ~ "^([0-9]{3})\|(keep|discard)\|(.+)$") {
    # Copy the groups out before the matches below overwrite them
    set var.status = std.atoi(re.group.1);
    set var.query = re.group.2;
    set var.location = re.group.3;
    if (var.query == "keep" && std.strlen(req.url.qs) > 0) {
      set var.location = var.location + if(var.location ~ "\?", "&", "?") + req.url.qs;
    }
    if (var.location ~ "^/") {
      set var.location = "https://" + req.http.host + var.location;
    }
    error var.status var.location;
  }
}

# Legacy URL shapes are rewritten in sequence so the client gets one redirect to
# the final path instead of one per rule.
set var.path = req.url.path;

# Department segment from the pre-2021 site: /courses/<department>/<course>/...
if (var.path ~ "^/courses/([^/]+)/(.+)$") {
  if (table.lookup_bool(departments, re.group.1, false)) {
    set var.path = "/courses/" + re.group.2;
  }
}

# /resources/res-* to /courses/res-*
if (var.path ~ "^/resources/(res.*)$") {
  set var.path = "/courses/" + re.group.1;
}

# Remove index.htm
set var.path = regsub(var.path, "/index\.htm$", "/");

# Add a trailing slash to directory paths (no file extension in the last segment)
if (var.path !~ "^/static" && var.path ~ "/[^/]+$" && var.path !~ "\.[a-zA-Z0-9]+$") {
  set var.path = var.path + "/";
}

if (var.path != req.url.path) {
  set var.location = "https://" + req.http.host + var.path;
  if (std.strlen(req.url.qs) > 0) {
    set var.location = var.location + "?" + req.url.qs;
  }
  error 301 var.location;
}
