# Handle robots.txt requests with synthetic responses
if (req.url.path == "/robots.txt") {
  error 900 "robots.txt";
}
