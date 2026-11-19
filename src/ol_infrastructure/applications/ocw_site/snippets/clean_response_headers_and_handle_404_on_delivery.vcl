# Remove AWS headers returned from S3
unset resp.http.x-amz-id-2;
unset resp.http.x-amz-request-id;
unset resp.http.x-amz-version-id;
unset resp.http.x-amz-meta-s3cmd-attrs;
unset resp.http.server;

# Remove unnecessary headers that add weight
unset resp.http.via;
unset resp.http.x-timer;
