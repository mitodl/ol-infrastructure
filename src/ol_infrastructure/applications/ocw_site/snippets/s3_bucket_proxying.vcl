set bereq.url = querystring.remove(bereq.url);
set bereq.url = regsub(bereq.url, "/$", "/index.html");
set bereq.url = regsub(bereq.url, "^/ans7870", "/largefiles");
set bereq.url = regsub(bereq.url, "^/ans15436", "/zipfiles");
