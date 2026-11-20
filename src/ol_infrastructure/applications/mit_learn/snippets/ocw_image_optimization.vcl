# Send OCW course images through the Fastly Image Optimizer. It reads width,
# format and quality from the query string, so the OCW routing snippet, which
# runs after this one, keeps the query string of the requests marked here.
#
# Fastly bills every request that carries this header, so it is set only for
# the formats the optimizer reads, and only under the OCW course paths. A shield
# POP sees the path the edge already rewrote.
if (req.url.path ~ "^/(courses/o|ocw-course-v3)/" &&
    req.url.ext ~ "(?i)^(?:gif|png|jpe?g|webp|avif|jxl|heic)\z") {
  set req.http.X-Fastly-Imageopto-Api = "fastly";
}
