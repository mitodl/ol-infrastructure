if (req.url.ext == "css") {
  set beresp.http.Content-type = "text/css";
}

if (req.url.ext == "js") {
  set beresp.http.Content-type = "application/javascript";
}

# Only infer an image Content-Type from the extension when one isn't already
# set to an image type. This avoids clobbering the Content-Type that Fastly
# Image Optimization produces (e.g. webp/avif via format negotiation).
if (beresp.http.Content-Type !~ "(?i)^image/") {
  if (req.url.ext == "png") {
    set beresp.http.Content-type = "image/png";
  }

  if (req.url.ext == "jpg") {
    set beresp.http.Content-type = "image/jpeg";
  }

  if (req.url.ext == "gif") {
    set beresp.http.Content-type = "image/gif";
  }
}

if (req.url.ext == "pdf") {
  set beresp.http.Content-type = "application/pdf";
}
