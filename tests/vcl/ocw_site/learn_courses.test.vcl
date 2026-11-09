// Course URLs on hosts listed in learn_course_redirects go to the same course on
// Learn. The suite enables it for live-qa.ocw.mit.edu (origin rc.learn.mit.edu),
// as the QA stack does, and not for the production hosts.

// @scope: recv
// @suite: a course home page goes to Learn
sub test_course_home_to_learn {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://rc.learn.mit.edu/courses/o/18-06-linear-algebra-spring-2010/");
}

// @scope: recv
// @suite: course pages, resources and files keep their path and query string
sub test_course_paths_to_learn {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/pages/syllabus/?utm_source=x";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://rc.learn.mit.edu/courses/o/18-06-linear-algebra-spring-2010/pages/syllabus/?utm_source=x");

  set req.url = "/courses/8-05-quantum-physics-ii-fall-2013/005979fa741c3ea2e0430456b70caf93_MIT8_05F13_Chap_05.pdf";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://rc.learn.mit.edu/courses/o/8-05-quantum-physics-ii-fall-2013/005979fa741c3ea2e0430456b70caf93_MIT8_05F13_Chap_05.pdf");

  set req.url = "/courses/8-04-quantum-physics-i-spring-2013/video_galleries/lecture-videos/";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://rc.learn.mit.edu/courses/o/8-04-quantum-physics-i-spring-2013/video_galleries/lecture-videos/");
}

// @scope: recv
// @suite: legacy rewrites and the trailing slash land on Learn in one hop
sub test_rewrites_to_learn {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/courses/mathematics/18-06-linear-algebra-spring-2010/index.htm";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://rc.learn.mit.edu/courses/o/18-06-linear-algebra-spring-2010/");

  set req.url = "/courses/18-06-linear-algebra-spring-2010";
  testing.call_subroutine("vcl_recv");
  assert.error(301, "https://rc.learn.mit.edu/courses/o/18-06-linear-algebra-spring-2010/");
}

// @scope: recv
// @suite: a dictionary entry that targets a course goes to Learn with the entry's status
sub test_dictionary_course_to_learn {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/6-006S20";
  testing.call_subroutine("vcl_recv");
  assert.error(307, "https://rc.learn.mit.edu/courses/o/6-006-introduction-to-algorithms-spring-2020/");
}

// @scope: recv
// @suite: dictionary entries for other pages stay on the OCW host
sub test_dictionary_other_pages_stay {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/terms";
  testing.call_subroutine("vcl_recv");
  assert.error(307, "https://live-qa.ocw.mit.edu/pages/privacy-and-terms-of-use/");
}

// @scope: recv
// @suite: pages other than courses are served by OCW
sub test_other_pages_stay {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/";
  testing.call_subroutine("vcl_recv");
  assert.not_error();

  set req.url = "/search/?q=calculus";
  testing.call_subroutine("vcl_recv");
  assert.not_error();

  set req.url = "/collections/introductory-programming/";
  testing.call_subroutine("vcl_recv");
  assert.not_error();

  set req.url = "/static_shared/js/main.js";
  testing.call_subroutine("vcl_recv");
  assert.not_error();
}

// @scope: recv
// @suite: a pre-2021 section URL is left to the 404 rewrite
sub test_legacy_section_not_redirected_in_recv {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/syllabus/";
  testing.call_subroutine("vcl_recv");
  assert.not_error();
}

// @scope: recv
// @suite: hosts without Learn course redirects keep serving courses
sub test_production_hosts_unchanged {
  set req.http.host = "ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/";
  testing.call_subroutine("vcl_recv");
  assert.not_error();
}

// @scope: fetch
// @suite: the 404 rewrite of a pre-2021 section URL goes straight to Learn
sub test_legacy_section_to_learn {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/syllabus/index.html";
  set beresp.status = 404;
  testing.call_subroutine("vcl_fetch");
  assert.error(301, "https://rc.learn.mit.edu/courses/o/18-06-linear-algebra-spring-2010/pages/syllabus/");
}

// @scope: fetch
// @suite: the 404 rewrite of a pre-2021 video page goes straight to Learn
sub test_legacy_page_to_learn {
  set req.http.host = "live-qa.ocw.mit.edu";
  set req.url = "/courses/18-06-linear-algebra-spring-2010/video-lectures/lecture-1-the-geometry-of-linear-equations/";
  set beresp.status = 404;
  testing.call_subroutine("vcl_fetch");
  assert.error(301, "https://rc.learn.mit.edu/courses/o/18-06-linear-algebra-spring-2010/resources/lecture-1-the-geometry-of-linear-equations/");
}
