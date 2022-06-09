if (beresp.status == 404) {
  set beresp.ttl = 5m;
  set beresp.http.Cache-Control = "max-age=300";
  return(deliver);
 }

if (bereq.url.path ~ "^/main\.[0-9a-f]+\.(css|js)") {
  set beresp.ttl = 2629743s;  // one month
  set beresp.http.Cache-Control = "max-age=2629743";
 }
