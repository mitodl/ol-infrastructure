# Redirects are raised as `error <status> "<location>"`
if (obj.status == 301 || obj.status == 302 || obj.status == 307) {
  set obj.http.Location = obj.response;
  return(deliver);
}
