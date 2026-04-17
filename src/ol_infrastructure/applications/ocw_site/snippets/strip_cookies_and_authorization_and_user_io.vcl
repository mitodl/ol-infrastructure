unset req.http.Cookie;
unset req.http.Authorization;
# Disallow user provided X-Fastly-Imageopto-Api
unset req.http.X-Fastly-Imageopto-Api;
