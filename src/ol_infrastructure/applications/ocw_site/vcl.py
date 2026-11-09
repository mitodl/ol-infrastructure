"""The OCW site's Fastly VCL snippets and redirect dictionary.

``__main__`` deploys these and ``tests/vcl/ocw_site/`` tests them through
:mod:`ol_infrastructure.lib.falco`, so both read the same definitions. Standard
library only, so the falco prek hook can import it.
"""

import json
from pathlib import Path

from ol_infrastructure.lib.falco import VclSnippet, VclSuite

PROJECT_DIR = Path(__file__).resolve().parent
SNIPPETS_DIR = PROJECT_DIR.joinpath("snippets")
REDIRECT_DICT_PATH = PROJECT_DIR.joinpath("redirect_dict.json")


def _snippet_file(name: str) -> str:
    return SNIPPETS_DIR.joinpath(name).read_text()


def redirect_dictionary() -> dict[str, str]:
    """Load the ``redirects`` edge dictionary items.

    :returns: Normalized request path -> ``<status>|<keep|discard>|<target>``.
    :rtype: dict[str, str]
    """
    return json.loads(REDIRECT_DICT_PATH.read_text())


def course_v3_redirects_table(course_v3_redirects: dict[str, str]) -> str:
    """Render the host -> Learn origin table read by ``redirects.vcl``.

    :param course_v3_redirects: Hostname -> origin to send v3 course URLs to.
    :returns: A VCL table declaration.
    :rtype: str
    """
    entries = "\n".join(
        f"  {json.dumps(domain)}: {json.dumps(origin)},"
        for domain, origin in course_v3_redirects.items()
    )
    return "table course_v3_redirects {\n" + entries + "\n}"


def ocw_site_snippets(
    *,
    course_v3_redirects: dict[str, str],
    image_optimization: bool,
) -> list[VclSnippet]:
    """Build the snippets for one OCW Fastly service.

    :param course_v3_redirects: Hostname -> origin for this service's domains.
    :param image_optimization: Whether Fastly Image Optimization is enabled.
    :returns: The snippets, in the order the service declares them.
    :rtype: list[VclSnippet]
    """
    snippets = [
        VclSnippet(
            name="TTLs setup",
            type="fetch",
            content=_snippet_file("ttl_setup.vcl"),
            priority=110,
        ),
        VclSnippet(
            name="Departments Table",
            type="init",
            content=_snippet_file("departments_table.vcl"),
        ),
        VclSnippet(
            name="S3 Bucket Proxying",
            type="miss",
            content=_snippet_file("s3_bucket_proxying.vcl"),
            priority=200,
        ),
        VclSnippet(
            name="Course v3 redirect origins",
            type="init",
            content=course_v3_redirects_table(course_v3_redirects),
        ),
        VclSnippet(
            name="Redirects",
            type="recv",
            content=_snippet_file("redirects.vcl"),
        ),
        VclSnippet(
            name="Legacy OCW Pages Redirect",
            type="fetch",
            content=_snippet_file("legacy_ocw_pages_redirect.vcl"),
        ),
        VclSnippet(
            name="Set correct Content-type for S3 assets",
            type="fetch",
            content=_snippet_file("set_correct_content_type_for_S3_assets.vcl"),
        ),
        VclSnippet(
            name="Enable streaming for mp4 files",
            type="fetch",
            content=_snippet_file("enable_streaming_for_mp4.vcl"),
        ),
        VclSnippet(
            name="Clean response headers and handle 404 on delivery",
            type="deliver",
            content=_snippet_file(
                "clean_response_headers_and_handle_404_on_delivery.vcl"
            ),
        ),
        VclSnippet(
            name="Reroute Redirects",
            type="error",
            content=_snippet_file("reroute_redirects.vcl"),
        ),
        VclSnippet(
            name="Segmented Caching",
            type="recv",
            content=_snippet_file("large_object_segmented_caching.vcl"),
        ),
        VclSnippet(
            name="Strip cookies and authorization and user provided io header.",
            type="recv",
            content=_snippet_file("strip_cookies_and_authorization_and_user_io.vcl"),
        ),
    ]
    if image_optimization:
        snippets.append(
            VclSnippet(
                name="Image Optimization",
                type="recv",
                content=_snippet_file("image_optimization.vcl"),
                priority=1000,
            )
        )
    return snippets


def vcl_suite() -> VclSuite:
    """Build the falco suite for the production live service's VCL.

    :returns: The suite ``tests/vcl/ocw_site/`` runs against.
    :rtype: VclSuite
    """
    return VclSuite(
        name="ocw_site",
        snippets=ocw_site_snippets(
            course_v3_redirects={
                "ocw.mit.edu": "https://learn.mit.edu",
                "www.ocw.mit.edu": "https://learn.mit.edu",
            },
            image_optimization=True,
        ),
        dictionaries={"redirects": redirect_dictionary()},
    )
