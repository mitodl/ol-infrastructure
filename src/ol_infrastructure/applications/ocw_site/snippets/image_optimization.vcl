if (req.url.ext ~ "(?i)^(?:gif|png|jpe?g|webp|avif|jxl|heic)\z") {
  set req.http.X-Fastly-Imageopto-Api = "fastly";
}
