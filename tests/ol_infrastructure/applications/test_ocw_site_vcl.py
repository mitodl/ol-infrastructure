"""Guard the OCW site's Learn course redirect configuration."""

import pytest

from ol_infrastructure.applications.ocw_site.vcl import learn_course_redirects_table


def test_learn_course_redirect_hosts_need_a_learn_origin():
    """Without an origin the VCL would redirect to a relative /courses/o/ path."""
    with pytest.raises(ValueError, match=r"live-qa\.ocw\.mit\.edu"):
        learn_course_redirects_table(["live-qa.ocw.mit.edu"], {})


def test_learn_course_redirect_table_lists_the_hosts():
    table = learn_course_redirects_table(
        ["live-qa.ocw.mit.edu"],
        {"live-qa.ocw.mit.edu": "https://rc.learn.mit.edu"},
    )
    assert table == (
        'table learn_course_redirects BOOL {\n  "live-qa.ocw.mit.edu": true,\n}'
    )
