# Enabled segmented caching on files that tend to be large
# Where "large" means > 2GB in size

if (req.url.ext ~ "(zip|ova)") {
   set req.enable_segmented_caching = true;
}
