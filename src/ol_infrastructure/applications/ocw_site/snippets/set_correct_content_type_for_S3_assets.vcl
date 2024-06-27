if (req.url.ext == "css") {
  set beresp.http.Content-type = "text/css";
}

if (req.url.ext == "js") {
  set beresp.http.Content-type = "application/javascript";
}

if (req.url.ext == "png") {
  set beresp.http.Content-type = "image/png";
}

if (req.url.ext == "jpg") {
  set beresp.http.Content-type = "image/jpeg";
}

if (req.url.ext == "gif") {
  set beresp.http.Content-type = "image/gif";
}

if (req.url.ext == "pdf") {
  set beresp.http.Content-type = "application/pdf";
}
