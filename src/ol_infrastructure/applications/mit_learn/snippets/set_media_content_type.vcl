// Set proper Content-Type headers for media files served from S3
// This ensures video files play inline in browsers instead of downloading

if (req.url.ext == "mp4") {
  set beresp.http.Content-Type = "video/mp4";
}

if (req.url.ext == "webm") {
  set beresp.http.Content-Type = "video/webm";
}

if (req.url.ext == "mov") {
  set beresp.http.Content-Type = "video/quicktime";
}

if (req.url.ext == "avi") {
  set beresp.http.Content-Type = "video/x-msvideo";
}

if (req.url.ext == "m4v") {
  set beresp.http.Content-Type = "video/x-m4v";
}
