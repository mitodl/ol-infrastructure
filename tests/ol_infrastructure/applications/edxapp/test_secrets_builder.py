"""Unit tests for secrets_builder module.

Tests the configuration building and template generation functions
that replace the old f-string concatenation approach.
"""

import pytest

from ol_infrastructure.applications.edxapp.secrets_builder import (
    _apply_deployment_secret_overrides,
    build_base_general_secrets_dict,
    get_database_connections_template,
    get_database_credentials_template,
    get_general_secrets_yaml,
    get_mongodb_credentials_template,
    get_mongodb_forum_template,
    secrets_dict_to_yaml_template,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


@pytest.fixture
def mock_stack_info_mitx() -> StackInfo:
    """Create mock StackInfo for mitx environment."""
    return StackInfo(
        name="infrastructure.aws.edxapp.mitx",
        namespace="infrastructure.aws.edxapp",
        env_prefix="mitx",
        env_suffix="qa",
        full_name="infrastructure.aws.edxapp.mitx",
    )


@pytest.fixture
def mock_stack_info_xpro() -> StackInfo:
    """Create mock StackInfo for xpro environment."""
    return StackInfo(
        name="infrastructure.aws.edxapp.xpro",
        namespace="infrastructure.aws.edxapp",
        env_prefix="xpro",
        env_suffix="qa",
        full_name="infrastructure.aws.edxapp.xpro",
    )


@pytest.fixture
def mock_stack_info_mitxonline() -> StackInfo:
    """Create mock StackInfo for mitxonline environment."""
    return StackInfo(
        name="infrastructure.aws.edxapp.mitxonline",
        namespace="infrastructure.aws.edxapp",
        env_prefix="mitxonline",
        env_suffix="qa",
        full_name="infrastructure.aws.edxapp.mitxonline",
    )


class TestBuildBaseGeneralSecretsDict:
    """Test base secrets dictionary building."""

    def test_base_secrets_has_required_keys(self, mock_stack_info_mitx):
        """Verify base secrets has all required settings."""
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain="mitx.example.com",
        )

        assert "CELERY_BROKER_PASSWORD" in secrets
        assert "SECRET_KEY" in secrets
        assert "JWT_AUTH" in secrets
        assert "OPENAI_SECRET_KEY" in secrets
        assert "SENTRY_DSN" in secrets

    def test_jwt_auth_configuration(self, mock_stack_info_mitx):
        """Verify JWT_AUTH has correct structure."""
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain="mitx.example.com",
        )

        jwt_auth = secrets["JWT_AUTH"]
        assert jwt_auth["JWT_ALGORITHM"] == "HS256"
        assert jwt_auth["JWT_AUDIENCE"] == "mitx"
        assert jwt_auth["JWT_SIGNING_ALGORITHM"] == "RS512"
        assert "JWT_ISSUERS" in jwt_auth
        assert len(jwt_auth["JWT_ISSUERS"]) == 1

    def test_redis_hostname_in_cache_config(self, mock_stack_info_mitx):
        """Verify redis hostname is used in cache config."""
        redis_host = "redis-prod.example.com"
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitx,
            redis_hostname=redis_host,
            lms_domain="mitx.example.com",
        )

        cache_config = secrets["redis_cache_config"]
        assert redis_host in cache_config["LOCATION"]

    def test_lms_domain_in_jwt_issuer(self, mock_stack_info_mitx):
        """Verify LMS domain is used in JWT configuration."""
        lms_domain = "mitx-custom.example.com"
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain=lms_domain,
        )

        jwt_auth = secrets["JWT_AUTH"]
        assert lms_domain in jwt_auth["JWT_ISSUER"]
        assert lms_domain in jwt_auth["JWT_ISSUERS"][0]["ISSUER"]

    def test_mitx_specific_secrets(self, mock_stack_info_mitx):
        """Test mitx-specific configuration."""
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain="mitx.example.com",
        )

        # mitx should have canvas token, email, and SAML
        assert "CANVAS_ACCESS_TOKEN" in secrets
        assert "EMAIL_HOST_USER" in secrets
        assert "EMAIL_HOST_PASSWORD" in secrets
        assert "SOCIAL_AUTH_SAML_SP_PRIVATE_KEY" in secrets

    def test_mitxonline_specific_secrets(self, mock_stack_info_mitxonline):
        """Test mitxonline-specific configuration."""
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitxonline,
            redis_hostname="redis.example.com",
            lms_domain="mitxonline.example.com",
        )

        # mitxonline should have github and deepl
        assert "GITHUB_ACCESS_TOKEN" in secrets
        assert "DEEPL_API_KEY" in secrets
        # But not canvas (mitx-only)
        assert "CANVAS_ACCESS_TOKEN" not in secrets

    def test_xpro_specific_secrets(self, mock_stack_info_xpro):
        """Test xpro-specific configuration."""
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_xpro,
            redis_hostname="redis.example.com",
            lms_domain="xpro.example.com",
        )

        # xpro should have email but not canvas
        assert "EMAIL_HOST_USER" in secrets
        assert "CANVAS_ACCESS_TOKEN" not in secrets

    def test_proctoring_default_backend_xpro(self, mock_stack_info_xpro):
        """Verify xpro uses null as default proctoring backend."""
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_xpro,
            redis_hostname="redis.example.com",
            lms_domain="xpro.example.com",
        )

        assert secrets["PROCTORING_BACKENDS"]["DEFAULT"] == "null"

    def test_proctoring_default_backend_mitx(self, mock_stack_info_mitx):
        """Verify mitx uses proctortrack as default backend."""
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain="mitx.example.com",
        )

        assert secrets["PROCTORING_BACKENDS"]["DEFAULT"] == "proctortrack"

    def test_proctortrack_url_configuration(self, mock_stack_info_mitx):
        """Test proctortrack URL configuration."""
        proctortrack_url = "https://proctortrack.example.com"
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain="mitx.example.com",
            proctortrack_url=proctortrack_url,
        )

        pt_config = secrets["PROCTORING_BACKENDS"]["proctortrack"]
        assert pt_config["base_url"] == proctortrack_url
        assert "client_id" in pt_config
        assert "client_secret" in pt_config
        assert "PROCTORING_USER_OBFUSCATION_KEY" in secrets

    def test_proctortrack_url_not_in_null_backend(self, mock_stack_info_mitx):
        """Verify proctortrack config not added without URL."""
        secrets = build_base_general_secrets_dict(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain="mitx.example.com",
            proctortrack_url=None,
        )

        # Should only have null backend
        assert "proctortrack" not in secrets["PROCTORING_BACKENDS"]
        assert "null" in secrets["PROCTORING_BACKENDS"]


class TestApplyDeploymentSecretOverrides:
    """Test deployment-specific secret overrides."""

    def test_mitx_overrides(self):
        """Test mitx-specific overrides."""
        base = {"KEY": "base_value"}
        result = _apply_deployment_secret_overrides("mitx", base)

        assert "CANVAS_ACCESS_TOKEN" in result
        assert "EMAIL_HOST_USER" in result

    def test_xpro_overrides(self):
        """Test xpro-specific overrides."""
        base = {"KEY": "base_value"}
        result = _apply_deployment_secret_overrides("xpro", base)

        assert "CANVAS_ACCESS_TOKEN" not in result
        assert "EMAIL_HOST_USER" in result

    def test_mitxonline_overrides(self):
        """Test mitxonline-specific overrides."""
        base = {"KEY": "base_value"}
        result = _apply_deployment_secret_overrides("mitxonline", base)

        assert "GITHUB_ACCESS_TOKEN" in result
        assert "DEEPL_API_KEY" in result


class TestTemplateGeneration:
    """Test template generation functions."""

    def test_database_credentials_template(self):
        """Test database credentials template generation."""
        template, name = get_database_credentials_template(
            db_address="db.example.com", db_port=3306
        )

        assert name == "00-database-credentials.yaml"
        assert "db.example.com" in template
        assert "3306" in template
        assert "mysql_creds" in template

    def test_database_connections_template(self):
        """Test database connections template generation."""
        template, name = get_database_connections_template(
            db_address="db.example.com", db_port=3306
        )

        assert name == "01-database-connections.yaml"
        assert "DATABASES:" in template
        assert "default:" in template
        assert "student_module_history:" in template
        assert "db.example.com" in template

    def test_mongodb_credentials_template(self):
        """Test MongoDB credentials template generation."""
        template, name = get_mongodb_credentials_template(
            replica_set="rs0", host_string="mongodb1.example.com:27017"
        )

        assert name == "02-mongo-db-credentials.yaml"
        assert "rs0" in template
        assert "mongodb1.example.com:27017" in template
        assert "mongodb_settings" in template

    def test_mongodb_forum_template(self):
        """Test MongoDB forum template generation."""
        template, name = get_mongodb_forum_template(
            replica_set="rs0", host_string="mongodb1.example.com:27017"
        )

        assert name == "03-mongo-db-forum-credentials.yaml"
        assert "FORUM_MONGODB_CLIENT_PARAMETERS:" in template
        assert "rs0" in template


class TestSecretsDictToYamlTemplate:
    """Test YAML template conversion."""

    def test_simple_dict_to_yaml(self):
        """Test converting simple dict to YAML."""
        secrets = {
            "KEY1": "value1",
            "KEY2": "value2",
        }
        yaml_output = secrets_dict_to_yaml_template(secrets)

        assert "KEY1: value1" in yaml_output
        assert "KEY2: value2" in yaml_output

    def test_nested_dict_to_yaml(self):
        """Test converting nested dict to YAML."""
        secrets = {
            "OUTER": {
                "INNER": "value",
            },
        }
        yaml_output = secrets_dict_to_yaml_template(secrets)

        assert "OUTER:" in yaml_output
        assert "INNER: value" in yaml_output

    def test_vault_template_strings_preserved(self):
        """Test that Vault template strings are preserved."""
        secrets = {
            "PASSWORD": '{{ get .Secrets "password" }}',
        }
        yaml_output = secrets_dict_to_yaml_template(secrets)

        assert '{{ get .Secrets "password" }}' in yaml_output


class TestGetGeneralSecretsYaml:
    """Test general secrets YAML generation."""

    def test_generates_valid_yaml(self, mock_stack_info_mitx):
        """Test that general secrets YAML is generated."""
        yaml_output = get_general_secrets_yaml(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain="mitx.example.com",
        )

        assert yaml_output is not None
        assert len(yaml_output) > 0
        # Should contain some known keys
        assert "CELERY_BROKER_PASSWORD" in yaml_output or "CELERY" in yaml_output

    def test_includes_deployment_specific_settings(self, mock_stack_info_mitx):
        """Test that deployment-specific settings are in YAML."""
        yaml_output = get_general_secrets_yaml(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain="mitx.example.com",
        )

        # mitx should have canvas token in the output
        assert "CANVAS_ACCESS_TOKEN" in yaml_output

    def test_includes_redis_hostname(self, mock_stack_info_mitx):
        """Test that redis hostname is in YAML."""
        redis_host = "redis-prod.example.com"
        yaml_output = get_general_secrets_yaml(
            stack_info=mock_stack_info_mitx,
            redis_hostname=redis_host,
            lms_domain="mitx.example.com",
        )

        assert redis_host in yaml_output

    def test_includes_lms_domain(self, mock_stack_info_mitx):
        """Test that LMS domain is in YAML."""
        lms_domain = "mitx-custom.example.com"
        yaml_output = get_general_secrets_yaml(
            stack_info=mock_stack_info_mitx,
            redis_hostname="redis.example.com",
            lms_domain=lms_domain,
        )

        assert lms_domain in yaml_output
