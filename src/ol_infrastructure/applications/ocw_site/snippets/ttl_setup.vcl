if (beresp.status == 404) {
  set beresp.ttl = 5m;
  set beresp.http.Cache-Control = "max-age=300";
  return(deliver);
}

if (bereq.url.path ~ ".*(main|common|www|course_v2|instructor_insights|fields|[0-9a-f]+)\.[0-9a-f]+\.(css|js)") {
  set beresp.ttl = 2629743s;  // one month
  set beresp.http.Cache-Control = "max-age=2629743";
}

if (bereq.url.path ~ ".*\/static_shared\/(images|fonts)\/[a-zA-Z0-9|-|_]+(\.subset)?\.[0-9a-f]+\.(png|jpg|jpeg|svg|ttf|woff|woff2)") {
  set beresp.ttl = 2629743s;  // one month
  set beresp.http.Cache-Control = "max-age=2629743";
} elsif (bereq.url.path ~ ".*\/static_shared\/(images|fonts)\/.*\.(png|jpg|jpeg|svg|ttf|woff|woff2)") {
  set beresp.ttl = 604800s;  // one week
  set beresp.http.Cache-Control = "max-age=604800";
}
