declare local var.path STRING;
declare local var.course STRING;
declare local var.section STRING;
declare local var.page STRING;
declare local var.base STRING;

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
      # Hosts that send courses to Learn get the Learn URL in this one hop
      if (table.lookup_bool(learn_course_redirects, req.http.host, false)) {
        set var.base = table.lookup(course_v3_redirects, req.http.host) + "/courses/o/" + var.course;
      } else {
        set var.base = "https://" + req.http.host + "/courses/" + var.course;
      }
      if (var.page == "") {
        error 301 var.base + "/pages/" + var.section + "/";
      }
      error 301 var.base + "/resources/" + var.page + "/";
    }
  }
  error 901 "Fastly Internal"; # Let the synthetic 404 take over
}
