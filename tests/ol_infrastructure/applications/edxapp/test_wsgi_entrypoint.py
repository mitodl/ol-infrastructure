"""Unit tests for the granian WSGI entrypoint that restores RAW_URI.

opentelemetry-instrumentation-wsgi crashes on any request path holding a
codepoint above U+00FF unless RAW_URI (or REQUEST_URI) is present in the environ.
These tests render the generated module, execute it against a stub application,
and assert both halves of that contract: the value it writes is a faithful target
for the request, and collect_request_attributes' fallback -- reproduced here as
``_otel_url_fallback`` -- is never reached.

The targets below are synthetic. The production failures were learner usernames
in the URL path; the codepoint ranges are what matters, not the names, and this
repository is public.
"""

import ast
import sys
import types
from typing import Any
from urllib.parse import unquote_to_bytes
from wsgiref.util import request_uri

import pytest

from ol_infrastructure.applications.edxapp.k8s_configmaps import (
    OL_WSGI_ENTRYPOINT_TEMPLATE,
)

# Each entry holds at least one codepoint above U+00FF, which is what the
# latin-1 re-encode in wsgiref.util.request_uri() cannot represent: CJK
# (U+6D4B, U+8BD5), Vietnamese (U+1EBF, U+1EC7) and Turkish (U+011F, U+015F).
NON_LATIN1_TARGETS = [
    "/api/user_tours/v1/learner-%E6%B5%8B%E8%AF%95",
    "/api/user_tours/v1/learner-ti%E1%BA%BFng-vi%E1%BB%87t",
    "/api/user_tours/v1/learner-t%C3%BCrk%C3%A7e-%C4%9F%C5%9F",
    (
        "/api/instructor/v2/courses/course-v1:MITxT+CTL.CFx+3T2026"
        "/special_exams/484/reset/learner-%E6%B5%8B%E8%AF%95"
    ),
]
# Controls: "jos%C3%A9" decodes to characters that all fit in latin-1, so it
# never tripped the bug, and the course key exercises the safe-character set.
LATIN1_SAFE_TARGETS = [
    "/api/user_tours/v1/learner-plain",
    "/api/user_tours/v1/learner-jos%C3%A9",
    "/api/instructor/v2/courses/course-v1:MITxT+CTL.CFx+3T2026/special_exams",
]


def _granian_environ(target: str) -> dict[str, Any]:
    """Build the environ granian hands to the WSGI app for ``target``.

    Mirrors granian >= 1.4.3: percent-decode the path to bytes, then decode those
    bytes as latin-1 so every character is <= 255, as PEP 3333 requires.
    """
    raw_path, _, query = target.partition("?")
    return {
        "PATH_INFO": unquote_to_bytes(raw_path).decode("latin-1"),
        "QUERY_STRING": query,
        "SCRIPT_NAME": "",
        "REQUEST_METHOD": "GET",
        "SERVER_NAME": "courses.learn.mit.edu",
        "SERVER_PORT": "443",
        "HTTP_HOST": "courses.learn.mit.edu",
        "wsgi.url_scheme": "https",
    }


def _django_rewrites_path_info(environ: dict[str, Any]) -> None:
    """Apply the in-place environ rewrite Django's WSGIRequest.__init__ performs.

    ``self.META = environ`` aliases the dict, so ``self.META["PATH_INFO"] =
    path_info`` replaces the latin-1 value with the decoded UTF-8 path.
    """
    environ["PATH_INFO"] = environ["PATH_INFO"].encode("iso-8859-1").decode("utf-8")


def _otel_url_fallback(environ: dict[str, Any]) -> str:
    """Reproduce the branch of collect_request_attributes that raises.

    Taken from opentelemetry-instrumentation-wsgi: RAW_URI and REQUEST_URI are
    consulted first, and only their absence reaches ``request_uri()``, which
    re-encodes PATH_INFO as latin-1.
    """
    target = environ.get("RAW_URI") or environ.get("REQUEST_URI")
    if target:
        return target
    return request_uri(environ)


def _load_entrypoint(service: str, stub_application) -> types.ModuleType:
    """Render the template for ``service`` and exec it over a stubbed app module."""
    rendered = OL_WSGI_ENTRYPOINT_TEMPLATE.format(service=service)
    wsgi_stub = types.ModuleType(f"{service}.wsgi")
    wsgi_stub.application = stub_application  # type: ignore[attr-defined]
    package_stub = types.ModuleType(service)
    package_stub.wsgi = wsgi_stub  # type: ignore[attr-defined]
    module = types.ModuleType(f"ol_{service}_wsgi")
    saved = {name: sys.modules.get(name) for name in (service, f"{service}.wsgi")}
    sys.modules[service] = package_stub
    sys.modules[f"{service}.wsgi"] = wsgi_stub
    try:
        exec(compile(rendered, f"ol_{service}_wsgi.py", "exec"), module.__dict__)  # noqa: S102
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


@pytest.fixture
def entrypoint():
    """Return (module, seen) where ``seen`` collects environs the app received."""
    seen = []

    def stub_application(environ, start_response):  # noqa: ARG001
        seen.append(environ)
        return [b"ok"]

    return _load_entrypoint("lms", stub_application), seen


@pytest.mark.parametrize("service", ["lms", "cms"])
def test_rendered_module_is_valid_python(service):
    """The generated module parses and imports the matching service's app."""
    rendered = OL_WSGI_ENTRYPOINT_TEMPLATE.format(service=service)
    ast.parse(rendered)
    assert f"from {service}.wsgi import application" in rendered


@pytest.mark.parametrize("target", NON_LATIN1_TARGETS + LATIN1_SAFE_TARGETS)
def test_raw_uri_matches_a_canonically_encoded_target(target, entrypoint):
    """RAW_URI reproduces the target for canonically encoded requests.

    See test_raw_uri_is_canonical_not_verbatim for what this does not promise.
    """
    module, _ = entrypoint
    environ = _granian_environ(target)
    module.application(environ, None)
    assert environ["RAW_URI"] == target


@pytest.mark.parametrize(
    ("sent", "reconstructed"),
    [
        ("/api/%41", "/api/A"),  # escape the client did not need
        ("/api/x%3ay", "/api/x:y"),  # lowercase escape of a safe character
        ("/api/a%2Fb", "/api/a/b"),  # encoded separator becomes a real one
    ],
)
def test_raw_uri_is_canonical_not_verbatim(sent, reconstructed, entrypoint):
    """RAW_URI is a canonical re-encoding, not the client's original bytes.

    granian percent-decodes into PATH_INFO and the environ carries the raw
    target nowhere else, so escapes the client did not need are unrecoverable.
    Harmless for a span attribute, and it does not affect routing: Django
    resolves from PATH_INFO, never from RAW_URI.
    """
    module, _ = entrypoint
    environ = _granian_environ(sent)
    module.application(environ, None)
    assert environ["RAW_URI"] == reconstructed


@pytest.mark.parametrize("target", NON_LATIN1_TARGETS)
def test_otel_does_not_crash_on_non_latin1_paths(target, entrypoint):
    """With the wrapper, OTel reads RAW_URI instead of re-encoding PATH_INFO."""
    module, _ = entrypoint
    environ = _granian_environ(target)
    module.application(environ, None)
    _django_rewrites_path_info(environ)
    assert _otel_url_fallback(environ) == target


@pytest.mark.parametrize("target", NON_LATIN1_TARGETS)
def test_otel_crashes_without_the_wrapper(target):
    """Guard the premise: the fallback really does raise on these paths.

    If this stops failing, granian or OTel has changed and the wrapper can go.
    """
    environ = _granian_environ(target)
    _django_rewrites_path_info(environ)
    with pytest.raises(UnicodeEncodeError):
        _otel_url_fallback(environ)


def test_query_string_is_preserved(entrypoint):
    """A query string is appended to RAW_URI, matching gunicorn's origin-form."""
    module, _ = entrypoint
    environ = _granian_environ(
        "/api/user_tours/v1/learner-%E6%B5%8B%E8%AF%95?next=%2Fdash"
    )
    module.application(environ, None)
    assert environ["RAW_URI"].endswith("?next=%2Fdash")


def test_script_name_is_included(entrypoint):
    """RAW_URI is the full origin-form target, so SCRIPT_NAME leads the path."""
    module, _ = entrypoint
    environ = _granian_environ("/api/user_tours/v1/learner-plain")
    environ["SCRIPT_NAME"] = "/lms"
    module.application(environ, None)
    assert environ["RAW_URI"] == "/lms/api/user_tours/v1/learner-plain"


def test_existing_raw_uri_is_not_overwritten(entrypoint):
    """A server that already sets RAW_URI keeps its own value."""
    module, _ = entrypoint
    environ = _granian_environ("/api/user_tours/v1/learner-plain")
    environ["RAW_URI"] = "/set/by/the/server"
    module.application(environ, None)
    assert environ["RAW_URI"] == "/set/by/the/server"


def test_request_uri_is_also_respected(entrypoint):
    """REQUEST_URI counts too -- OTel consults it before the failing fallback."""
    module, _ = entrypoint
    environ = _granian_environ("/api/user_tours/v1/learner-plain")
    environ["REQUEST_URI"] = "/set/by/the/server"
    module.application(environ, None)
    assert "RAW_URI" not in environ


def test_wrapped_application_is_called_and_response_returned(entrypoint):
    """The wrapper delegates rather than short-circuiting the request."""
    module, seen = entrypoint
    environ = _granian_environ("/api/user_tours/v1/learner-plain")
    assert module.application(environ, None) == [b"ok"]
    assert seen == [environ]


def test_safe_characters_leave_course_keys_unescaped(entrypoint):
    """Course keys keep their ":" and "+" so traces stay readable."""
    module, _ = entrypoint
    target = "/api/instructor/v2/courses/course-v1:MITxT+CTL.CFx+3T2026/special_exams"
    environ = _granian_environ(target)
    module.application(environ, None)
    assert "course-v1:MITxT+CTL.CFx+3T2026" in environ["RAW_URI"]
