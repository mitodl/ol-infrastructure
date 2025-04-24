{
  declare local var.qs STRING;
  set var.qs = req.url.qs;
  # If the request is for the root ("/"), rewrite it to "/frontend/index.html"
  if (req.url.path == "/" || req.url.path == "") {
    set req.url = "/frontend/index.html";
  }

  # If the request does NOT have an extension and is NOT a directory, append ".html"
  if (req.url.path !~ "\.[a-zA-Z0-9]+$" && req.url.path !~ "/$") {
    set req.url = req.url.path + ".html";
  }

  # Prepend "/frontend" unless it's already prefixed
  if (req.method == "GET" && req.url !~ "^/frontend/") {
    set req.url = "/frontend" + req.url.path;
  }
  # Peserve the query string if it exists
  if (var.qs != "") {
    set req.url = req.url + "?" + var.qs;
  }
}
