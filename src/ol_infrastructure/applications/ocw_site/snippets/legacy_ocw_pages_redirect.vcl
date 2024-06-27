# Add /pages/ to URL in case of 404
if (beresp.status == 404) {
  if (req.url.path ~ "(/courses/)([\w-]+)/([\w-]+)/(.*)" && !req.http.redirected) {
    if (std.strlen(re.group.4) > 0) {
      set req.http.pages_header = re.group.1 + re.group.2 + "/resources/" + re.group.4;
    } else {
      set req.http.pages_header = re.group.1 + re.group.2 + "/pages/" + re.group.3;
    }
    error 602 "redirect";
  }
  error 901 "Fastly Internal"; # Let the synthetic 404 take over
}
