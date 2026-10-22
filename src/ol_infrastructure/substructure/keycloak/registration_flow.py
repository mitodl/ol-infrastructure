"""Keycloak registration flow with a reCAPTCHA step."""

import pulumi
import pulumi_keycloak as keycloak


def create_recaptcha_registration_flow(
    realm_id: str | pulumi.Output[str],
    name_prefix: str,
    recaptcha_site_key: str | pulumi.Output[str],
    recaptcha_secret_key: str | pulumi.Output[str],
    opts: pulumi.ResourceOptions | None = None,
) -> tuple[keycloak.authentication.Flow, keycloak.authentication.ExecutionConfig]:
    """Create a copy of Keycloak's built-in registration flow with reCAPTCHA required.

    Built-in flows can't be owned by Pulumi, so this mirrors the built-in
    ``registration`` flow (same executions and priorities) as a custom flow for the
    realm's registration binding.

    Keycloak returns the secret as ``**********`` on read, so every preview shows the
    config updating ``secret.key`` and every apply writes the configured secret back.
    The secret is not in ``ignore_changes`` because Pulumi creates a replacement from
    the ignored path's stored value, which would pair a new site key with the old or
    masked secret. A new site key replaces the config; the replace deletes the old
    config first (Keycloak allows one per execution), and registration errors until
    the new one is created. A new secret alone updates in place.

    To stop managing the flow, bind the realm back to the built-in ``registration``
    flow in one apply and remove this flow in the next. Keycloak refuses to delete a
    bound flow, and dropping ``registration_flow`` from the binding leaves the realm
    bound to it.

    :param realm_id: The realm the flow belongs to.
    :param name_prefix: Prefix for the resource names and flow aliases, e.g.
        ``ol-registration``. The realm's flow binding points at the alias, so changing
        the prefix of a deployed realm replaces the bound flow.
    :param recaptcha_site_key: reCAPTCHA v2 checkbox site key.
    :param recaptcha_secret_key: The site key's secret, used for siteverify.
    :param opts: Resource options applied to every resource in the flow.
    :returns: The top-level flow, to bind as the realm's registration flow, and the
        reCAPTCHA config, the last resource created, which the binding should depend
        on so the flow is complete before it is bound.
    :rtype: tuple[keycloak.authentication.Flow, keycloak.authentication.ExecutionConfig]
    """
    registration_flow = keycloak.authentication.Flow(
        f"{name_prefix}-flow",
        realm_id=realm_id,
        alias=f"{name_prefix}-flow",
        provider_id="basic-flow",
        description="registration with reCAPTCHA",
        opts=opts,
    )
    registration_form = keycloak.authentication.Subflow(
        f"{name_prefix}-form",
        realm_id=realm_id,
        alias=f"{name_prefix}-form",
        parent_flow_alias=registration_flow.alias,
        provider_id="form-flow",
        authenticator="registration-page-form",
        requirement="REQUIRED",
        priority=10,
        opts=opts,
    )
    user_creation = keycloak.authentication.Execution(
        f"{name_prefix}-user-creation",
        realm_id=realm_id,
        parent_flow_alias=registration_form.alias,
        authenticator="registration-user-creation",
        requirement="REQUIRED",
        priority=20,
        opts=opts,
    )
    password_validation = keycloak.authentication.Execution(
        f"{name_prefix}-password-validation",
        realm_id=realm_id,
        parent_flow_alias=registration_form.alias,
        authenticator="registration-password-action",
        requirement="REQUIRED",
        priority=50,
        opts=opts,
    )
    recaptcha_execution = keycloak.authentication.Execution(
        f"{name_prefix}-recaptcha",
        realm_id=realm_id,
        parent_flow_alias=registration_form.alias,
        authenticator="registration-recaptcha-action",
        requirement="REQUIRED",
        priority=60,
        opts=opts,
    )
    recaptcha_config = keycloak.authentication.ExecutionConfig(
        f"{name_prefix}-recaptcha-config",
        realm_id=realm_id,
        execution_id=recaptcha_execution.id,
        alias=f"{name_prefix}-recaptcha",
        config={
            "site.key": recaptcha_site_key,
            "secret.key": recaptcha_secret_key,
            "action": "register",
            "recaptcha.v3": "false",
            "useRecaptchaNet": "true",
        },
        opts=pulumi.ResourceOptions.merge(
            opts,
            pulumi.ResourceOptions(
                # Every execution is created before the config, so a binding that
                # depends on the config sees the whole form.
                depends_on=[user_creation, password_validation],
                replace_on_changes=['config["site.key"]'],
                delete_before_replace=True,
            ),
        ),
    )
    return registration_flow, recaptcha_config
