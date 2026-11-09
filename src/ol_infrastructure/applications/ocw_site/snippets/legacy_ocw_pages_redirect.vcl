declare local var.path STRING;
declare local var.course STRING;
declare local var.section STRING;
declare local var.page STRING;

# Pre-2021 course URLs put sections and video pages straight under the course:
#   /courses/<course>/<section>/         -> /courses/<course>/pages/<section>/
#   /courses/<course>/<section>/<page>/  -> /courses/<course>/resources/<page>/
# Only tried once S3 has 404'd, so current URLs are never touched.
if (beresp.status == 404) {
  # With shielding, the shield sees the URL after the edge's miss snippet has
  # appended index.html, so match on the directory either way.
  set var.path = regsub(req.url.path, "/index\.html$", "/");
  if (var.path ~ "^/courses/([^/]+)/([^/.]+)/(([^/.]+)/)?$") {
    set var.course = re.group.1;
    set var.section = re.group.2;
    set var.page = re.group.4;
    # The targets live under pages/ and resources/, so a 404 there is final.
    # Anything else would redirect to itself or bounce between the two.
    if (var.section != "pages" && var.section != "resources") {
      if (var.page == "") {
        error 301 "https://" + req.http.host + "/courses/" + var.course + "/pages/" + var.section + "/";
      }
      error 301 "https://" + req.http.host + "/courses/" + var.course + "/resources/" + var.page + "/";
    }
  }
  error 901 "Fastly Internal"; # Let the synthetic 404 take over
}
