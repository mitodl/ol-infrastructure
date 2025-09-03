if (obj.status == 601) {
  set obj.status = 301;
  set obj.http.Location = req.http.redirect_dict;
  if (req.http.slash_header) {
    set obj.http.Location = req.http.slash_header;
  }
  return(deliver);
}

if (obj.status == 603) {
  set obj.status = 301;
  set obj.http.Location = req.http.cert_redirect;
  return(deliver);
}
