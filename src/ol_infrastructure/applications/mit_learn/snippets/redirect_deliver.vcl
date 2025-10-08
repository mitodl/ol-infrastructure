if (obj.status == 601) {
  set obj.status = 301;
  set obj.http.Location = "https://" + req.http.host + req.http.redirect_dest;
  return(deliver);
}
