// Redirects raised in vcl_recv by snippets/redirects.vcl. The redirects table is
// the real redirect_dict.json, so entries named here must stay in it.

// @scope: recv
// @suite: dictionary entries are matched case-insensitively and point at the final URL
sub test_dictionary_redirect {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/6-006S20";
  testing.call_subroutine("vcl_recv");
  assert.error(307, "https://ocw.mit.edu/courses/6-006-introduction-to-algorithms-spring-2020/");
}

// @scope: recv
// @suite: dictionary lookups ignore a trailing slash and index.htm
sub test_dictionary_normalizes_path {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/terms/index.htm";
  testing.call_subroutine("vcl_recv");
  assert.error(307, "https://ocw.mit.edu/pages/privacy-and-terms-of-use/");
}

// @scope: recv
// @suite: a "discard" entry drops the query string
sub test_dictionary_discards_query {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/6-006S20?utm_source=x";
  testing.call_subroutine("vcl_recv");
  assert.error(307, "https://ocw.mit.edu/courses/6-006-introduction-to-algorithms-spring-2020/");
}

// @scope: recv
// @suite: a "keep" entry appends the query string, after any query the target already has
sub test_dictionary_keeps_query {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/this-course-at-mit/?utm_source=x";
  testing.call_subroutine("vcl_recv");
  assert.error(307, "https://ocw.mit.edu/search/?f=Instructor%20Insights&s=-runs.best_start_date&utm_source=x");
}

// @scope: recv
// @suite: off-site dictionary targets are used as-is
sub test_dictionary_external_target {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/contact";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://mitocw.zendesk.com/hc/en-us/requests/new");
}

// @scope: recv
// @suite: on-site dictionary targets keep the request host
sub test_dictionary_keeps_host {
  set req.http.host = "www.ocw.mit.edu";
  set req.url = "/6-006S20";
  testing.call_subroutine("vcl_recv");
  assert.error(307, "https://www.ocw.mit.edu/courses/6-006-introduction-to-algorithms-spring-2020/");
}

// @scope: recv
// @suite: 302 dictionary entries redirect
sub test_dictionary_302 {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/how_to_speak";
  testing.call_subroutine("vcl_recv");
  assert.error(302, "https://ocw.mit.edu/courses/res-tll-005-how-to-speak-january-iap-2018/");
}

// @scope: recv
// @suite: v3 course URLs go to Learn with the path and query string
sub test_v3_course_to_learn {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/ocw-course-v3/courses/7-01sc-fundamentals-of-biology-fall-2011/pages/syllabus/?a=b";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://learn.mit.edu/courses/o/7-01sc-fundamentals-of-biology-fall-2011/pages/syllabus/?a=b");
}

// @scope: recv
// @suite: v3 course URLs are left alone on hosts without a Learn origin
sub test_v3_course_other_host {
  set req.http.host = "draft.ocw.mit.edu";
  set req.url = "/courses/o/7-01sc-fundamentals-of-biology-fall-2011/";
  testing.call_subroutine("vcl_recv");
  assert.not_error();
}

// @scope: recv
// @suite: the department segment is dropped, keeping the rest of the path and the query
sub test_department_segment {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/mathematics/18-06-linear-algebra-spring-2010/video-lectures/lecture-1/notes.pdf?x=1";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://ocw.mit.edu/courses/18-06-linear-algebra-spring-2010/video-lectures/lecture-1/notes.pdf?x=1");
}

// @scope: recv
// @suite: rewrites combine into one redirect
sub test_rewrites_combine {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/mathematics/18-06-linear-algebra-spring-2010/index.htm";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://ocw.mit.edu/courses/18-06-linear-algebra-spring-2010/");
}

// @scope: recv
// @suite: /resources/res-* moves under /courses/
sub test_resources_res {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/resources/res-6-007-signals-and-systems-spring-2011/";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://ocw.mit.edu/courses/res-6-007-signals-and-systems-spring-2011/");
}

// @scope: recv
// @suite: index.htm is removed
sub test_index_htm {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/index.htm";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://ocw.mit.edu/");
}

// @scope: recv
// @suite: directory paths get a trailing slash, keeping the query string
sub test_trailing_slash {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010?x=1";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://ocw.mit.edu/courses/18-06-linear-algebra-spring-2010/?x=1");
}

// @scope: recv
// @suite: files and /static paths are not given a trailing slash
sub test_no_trailing_slash_for_files {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/notes.pdf";
  testing.call_subroutine("vcl_recv");
  assert.not_error();

  set req.url = "/static_shared/js/main";
  testing.call_subroutine("vcl_recv");
  assert.not_error();
}

// @scope: recv
// @suite: /high-school goes to the Zendesk article
sub test_high_school {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/high-school/physics";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://mitocw.zendesk.com/hc/en-us/articles/5332864282907");
}

// @scope: recv
// @suite: current course and home page URLs pass through
sub test_current_urls_pass_through {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/pages/syllabus/";
  testing.call_subroutine("vcl_recv");
  assert.not_error();

  set req.url = "/";
  testing.call_subroutine("vcl_recv");
  assert.not_error();
}

// @scope: error
// @suite: every redirect status gets its Location from the error response
sub test_error_sets_location {
  set obj.status = 302;
  set obj.response = "https://ocw.mit.edu/courses/res-tll-005-how-to-speak-january-iap-2018/";
  testing.call_subroutine("vcl_error");
  assert.equal(obj.http.Location, "https://ocw.mit.edu/courses/res-tll-005-how-to-speak-january-iap-2018/");
  assert.state(deliver);
}
