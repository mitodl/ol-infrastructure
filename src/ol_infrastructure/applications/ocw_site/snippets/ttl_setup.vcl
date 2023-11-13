if (beresp.status == 404) {
  set beresp.ttl = 5m;
  set beresp.http.Cache-Control = "max-age=300";
  return(deliver);
}
# One month cache is for hashed static assets (first three cases), while one week is for unhashed assets (last three cases)
if (bereq.url.path ~ ".*(main|common|www|course_v2|instructor_insights|fields)\.[0-9a-f]+\.(css|js)\z") || (bereq.url.path ~ ".*[0-9a-f]+\.[0-9a-f]+\.(css|js)\z") {
  # Hashed static theme assets and dynamic imports
  set beresp.ttl = 2629743s;
  set beresp.http.Cache-Control = "max-age=2629743";
} elsif (bereq.url.path ~ ".*/static_shared/images/.*\.[0-9a-f]+\.(png|jpg|jpeg|svg|gif)\z") || (bereq.url.path ~ ".*/static_shared/fonts/.*\.subset\.[0-9a-f]+\.(ttf|woff|woff2)\z") {
  # Hashed static images and fonts
  set beresp.ttl = 2629743s;
  set beresp.http.Cache-Control = "max-age=2629743";
} elsif (bereq.url.path ~ ".*/static_shared/mathjax/.*\.[0-9a-f]+.*\z") {
  # Hashed static mathjax assets
  set beresp.ttl = 2629743s;
  set beresp.http.Cache-Control = "max-age=2629743";
} elsif (bereq.url.path ~ ".*/static_shared/(images|fonts)/.*\.(png|jpg|jpeg|svg|gif|ttf|woff|woff2)\z") {
  # Non-hashed static images and fonts (if any)
  set beresp.ttl = 604800s;
  set beresp.http.Cache-Control = "max-age=604800";
} elsif (bereq.url.path ~ ".*/(ocw-www|courses|images)/.*\.(png|jpg|jpeg|svg|gif)\z") {
  # Any other images
  set beresp.ttl = 604800s;
  set beresp.http.Cache-Control = "max-age=604800";
} elsif (bereq.http.Host ~ "www.ocw-openmatters.org") && (bereq.url.path ~ "^/wp-content/uploads/.*\.(png|jpg|jpeg|svg|gif)\z") {
  # Uploaded images in openmatters
  set beresp.ttl = 604800s;
  set beresp.http.Cache-Control = "max-age=604800";
}
