"""mitxonline and mit_learn must leave bearer tokens to the application.

Setting any of these openid-connect options makes APISIX verify every
``Authorization: Bearer`` header against Keycloak and 401 the ones it did not
issue.  Open edX sends mitxonline its own Django OAuth Toolkit tokens on every
learner login and every enrollment/certificate webhook, so turning this on
broke course login (Rootly INC-10, ol-infrastructure#4810).  See
docs/adr/0013-apisix-leaves-bearer-tokens-to-mitxonline-and-mit-learn.md.

These stacks are Pulumi programs that cannot be imported under test, so the
check is made against their source.
"""

import ast
from pathlib import Path

import pytest

APPLICATIONS_ROOT = (
    Path(__file__).parents[3] / "src" / "ol_infrastructure" / "applications"
)

BEARER_VALIDATION_OPTIONS = frozenset(
    {"bearer_only", "introspection_endpoint", "public_key", "use_jwks"}
)


def _bearer_validation_references(tree: ast.AST) -> list[tuple[int, str]]:
    """Find every keyword argument, attribute or string literal naming a bearer
    option, with or without the ``OLApisixOIDCConfig`` field prefix ``oidc_``.

    Keyword arguments catch ``OLApisixOIDCConfig(oidc_use_jwks=True)``,
    attributes catch ``config.oidc_bearer_only = True``, and string literals
    catch a plugin dict mutated after the fact, e.g.
    ``plugin["config"]["use_jwks"] = True`` or ``**{"oidc_use_jwks": True}``.
    """
    references = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword):
            name, lineno = node.arg, node.value.lineno
        elif isinstance(node, ast.Attribute):
            name, lineno = node.attr, node.lineno
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name, lineno = node.value, node.lineno
        else:
            continue
        if name and name.removeprefix("oidc_") in BEARER_VALIDATION_OPTIONS:
            references.append((lineno, name))
    return references


@pytest.mark.parametrize("application", ["mitxonline", "mit_learn"])
def test_gateway_does_not_validate_bearer_tokens(application):
    offenders = [
        f"{source_file}:{lineno} {name}"
        for source_file in sorted((APPLICATIONS_ROOT / application).rglob("*.py"))
        for lineno, name in _bearer_validation_references(
            ast.parse(source_file.read_text())
        )
    ]
    assert not offenders


@pytest.mark.parametrize(
    "source",
    [
        "OLApisixOIDCConfig(oidc_use_jwks=True)",
        "OLApisixOIDCConfig(oidc_bearer_only=True)",
        'plugin["config"]["use_jwks"] = True',
        'plugin["config"].update({"public_key": key})',
        'plugin["config"]["introspection_endpoint"] = url',
        'cfg.model_copy(update={"oidc_use_jwks": True})',
        'OLApisixOIDCConfig(**{"oidc_bearer_only": True})',
        "cfg.oidc_bearer_only = True",
    ],
)
def test_detects_bearer_validation(source):
    assert _bearer_validation_references(ast.parse(source))


def test_ignores_session_options():
    source = (
        "OLApisixOIDCConfig(oidc_use_session_secret=True, oidc_scope='openid')\n"
        'plugin["config"]["unauth_action"] = "pass"\n'
    )
    assert not _bearer_validation_references(ast.parse(source))
