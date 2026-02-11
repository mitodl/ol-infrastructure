# Handle robots.txt synthetic responses
if (obj.status == 900 && obj.response == "robots.txt") {
  set obj.status = 200;
  set obj.http.Content-Type = "text/plain";
  synthetic {"ROBOTS_CONTENT_PLACEHOLDER"};
  return(deliver);
}
