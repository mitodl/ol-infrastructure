if (obj.status == 601 && obj.response == "redirect") {
  set obj.status = 301;
  set obj.http.Location = req.protocol + "://" + req.http.host + req.http.header_sans_department;
  return (deliver);
}

if (obj.status == 602 && obj.response == "redirect" && !req.http.redirected) {
  set obj.status = 301;
  set obj.http.Location = req.protocol + "://" + req.http.host + req.http.pages_header;
  set req.http.redirected = "1";
  return (deliver);
}

if (obj.status == 604 && obj.response == "redirect") {
  set obj.status = 301;
  set obj.http.location = req.http.courses_instead_resources;
  return(deliver);
}

if (obj.status == 605 && obj.response == "redirect") {
  set obj.status = 301;
  set obj.http.location = req.http.remove_index_htm;
  return(deliver);
}

if (obj.status == 606 && obj.response == "redirect") {
  set obj.status = 301;
  set obj.http.location = req.http.high_school_to_article;
  return(deliver);
}

if (obj.status == 301) {
  set obj.status = 301;
  set obj.http.Location = req.protocol + "://" + req.http.host + req.http.slash_header;
  return (deliver);
}

if (obj.status == 307) {
  set obj.http.Location = obj.response;
  return(deliver);
 }
