// The 404-time rewrite of pre-2021 course URLs in snippets/legacy_ocw_pages_redirect.vcl.
// On a shield POP req.url ends in index.html, because the edge's vcl_miss
// appended it, so each case is checked in that form too.

// @scope: fetch
// @suite: a missing section directory redirects to its page
sub test_section_to_page {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/syllabus/";
  set beresp.status = 404;
  testing.call_subroutine("vcl_fetch");
  assert.error(301, "https://ocw.mit.edu/courses/18-06-linear-algebra-spring-2010/pages/syllabus/");
}

// @scope: fetch
// @suite: the shield's index.html URL redirects to the same page
sub test_section_to_page_on_shield {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/syllabus/index.html";
  set beresp.status = 404;
  testing.call_subroutine("vcl_fetch");
  assert.error(301, "https://ocw.mit.edu/courses/18-06-linear-algebra-spring-2010/pages/syllabus/");
}

// @scope: fetch
// @suite: a missing page under a section redirects to its resource
sub test_section_page_to_resource {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/video-lectures/lecture-1-the-geometry-of-linear-equations/index.html";
  set beresp.status = 404;
  testing.call_subroutine("vcl_fetch");
  assert.error(301, "https://ocw.mit.edu/courses/18-06-linear-algebra-spring-2010/resources/lecture-1-the-geometry-of-linear-equations/");
}

// @scope: fetch
// @suite: a 404 under resources/ is final, so it cannot redirect to itself
sub test_missing_resource_is_404 {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/8-05-quantum-physics-ii-fall-2013/resources/does-not-exist/index.html";
  set beresp.status = 404;
  testing.call_subroutine("vcl_fetch");
  assert.error(901);
}

// @scope: fetch
// @suite: a 404 under pages/ is final, so it cannot bounce to resources/
sub test_missing_page_is_404 {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/8-05-quantum-physics-ii-fall-2013/pages/does-not-exist/";
  set beresp.status = 404;
  testing.call_subroutine("vcl_fetch");
  assert.error(901);
}

// @scope: fetch
// @suite: a missing file is a 404, not a guessed resource page
sub test_missing_file_is_404 {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/lecture-notes/mit18_06s10_lec1.pdf";
  set beresp.status = 404;
  testing.call_subroutine("vcl_fetch");
  assert.error(901);
}

// @scope: fetch
// @suite: responses that are not 404 are untouched
sub test_found_is_untouched {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/syllabus/";
  set beresp.status = 200;
  testing.call_subroutine("vcl_fetch");
  assert.not_error();
}
