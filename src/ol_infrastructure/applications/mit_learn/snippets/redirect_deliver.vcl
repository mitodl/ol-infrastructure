if (obj.status == 601) {
  set obj.status = 301;
  set obj.http.Location = req.http.redirect_dest;
  return(deliver);
}
