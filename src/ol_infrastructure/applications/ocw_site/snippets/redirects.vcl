declare local var.location STRING;
declare local var.redirect STRING;
declare local var.path STRING;
declare local var.query STRING;
declare local var.status INTEGER;
declare local var.from_dictionary BOOL;
declare local var.mode STRING;
declare local var.target_query STRING;
declare local var.learn STRING;
declare local var.course STRING;
declare local var.course_path STRING;

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

# Every rule below settles on a path and query string; the URL is built once at
# the end, so a course path can go to Learn whichever rule produced it.
set var.status = 301;
set var.path = req.url.path;
set var.query = req.url.qs;
set var.from_dictionary = false;

# Dictionary entries are "<status>|<keep|discard query string>|<target>". Keys
# are lowercase with no trailing slash or /index.htm, so normalize the same way.
set var.redirect = table.lookup(
  redirects, std.tolower(regsub(req.url.path, "(/index\.htm|/)$", "")), ""
);
if (var.redirect ~ "^([0-9]{3})\|(keep|discard)\|([^?]+)\??(.*)$") {
  # Copy the groups out before the matches below overwrite them
  set var.status = std.atoi(re.group.1);
  set var.mode = re.group.2;
  set var.path = re.group.3;
  set var.target_query = re.group.4;
  set var.from_dictionary = true;
  if (var.mode == "discard" || std.strlen(req.url.qs) == 0) {
    set var.query = var.target_query;
  } elsif (std.strlen(var.target_query) > 0) {
    set var.query = var.target_query + "&" + req.url.qs;
  }
  if (var.path !~ "^/") {
    # Off-site target
    if (std.strlen(var.query) > 0) {
      error var.status var.path + "?" + var.query;
    }
    error var.status var.path;
  }
}

if (!var.from_dictionary) {
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
}

# Hosts in learn_course_redirects send course URLs to the same course on Learn.
# Only the paths a current course site has are mapped here; pre-2021 section
# URLs fall through to S3 and the 404 rewrite in vcl_fetch sends them to Learn.
set var.location = "";
if (table.lookup_bool(learn_course_redirects, req.http.host, false)) {
  set var.learn = table.lookup(course_v3_redirects, req.http.host);
  # The outer group around the optional part always takes part in the match, so
  # a course home page reads it as "" rather than an unset group.
  if (var.path ~ "^/courses/([^/]+)/(((pages|resources|download|external-resources|video_galleries|lists)/.*|[^/]+\.[a-zA-Z0-9]+)?)$") {
    set var.course = re.group.1;
    set var.course_path = re.group.2;
    # A department index page is not a course
    if (!table.lookup_bool(departments, var.course, false)) {
      set var.location = var.learn + "/courses/o/" + var.course + "/" + var.course_path;
    }
  }
}
if (var.location == "" && (var.from_dictionary || var.path != req.url.path)) {
  set var.location = "https://" + req.http.host + var.path;
}
if (var.location != "") {
  if (std.strlen(var.query) > 0) {
    set var.location = var.location + "?" + var.query;
  }
  error var.status var.location;
}
