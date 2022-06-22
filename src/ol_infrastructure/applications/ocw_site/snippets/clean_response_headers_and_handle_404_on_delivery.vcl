# Remove AWS headers returned from S3
unset resp.http.x-amz-id-2;
unset resp.http.x-amz-request-id;
unset resp.http.x-amz-version-id;
unset resp.http.x-amz-meta-s3cmd-attrs;
unset resp.http.server;

# Remove unnecessary headers that add weight
unset resp.http.via;
unset resp.http.x-timer;

# Handle repeat 404
declare local var.same_url STRING;
set var.same_url = "https://" req.http.host req.url;

if(var.same_url == resp.http.location && req.proto != "HTTP/1.1") {
  set resp.status = 404;
  return(restart);
}
