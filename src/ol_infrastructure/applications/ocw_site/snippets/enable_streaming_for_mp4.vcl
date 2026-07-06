# Enable streaming of the backend response for mp4 files so that playback
# can begin before the entire object has been fetched from the origin.

if (req.url.ext ~ "(?i)^mp4$") {
  set beresp.do_stream = true;
}
