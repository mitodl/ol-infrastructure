{
  if ((resp.status == 404 || resp.status == 403) && req.url !~ "^/frontend/404\.html$") {
    set req.url = "/frontend/404.html";
    restart;
  }
}
