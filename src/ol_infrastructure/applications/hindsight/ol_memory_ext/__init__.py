"""Hindsight extensions for the Open Learning shared memory service.

Runs inside the Hindsight API image, not against this project's environment: the
stack mounts this package from a ConfigMap and selects each class with a
``HINDSIGHT_API_<KIND>_EXTENSION`` setting (see ../__main__.py).

``KeycloakTenantExtension``
    Apps call with a learner token scoped to the memory service's audience,
    normally obtained by Keycloak standard token exchange (RFC 8693) from the
    learner's own session token. The extension verifies it (JWKS signature,
    issuer, audience, expiry), maps ``azp`` to the app, and takes the learner
    from ``sub``, so an app can only act for a learner whose session it holds.
    Minting such a token takes the app client's secret, whether by exchange or
    by requesting the audience scope in the app's own login; the service cannot
    tell the two apart.

    Two grants use client credentials instead. A token carrying the general
    scope writes general memory (facts about the product, courses and so on, in
    ``g-<name>`` banks) and can never reach a learner's bank. The platform admin
    client names the learner in ``x-ol-learner``, for the forget path run on
    account deletion. App and learner go on the RequestContext in the two fields
    the engine carries into worker tasks (``tenant_id`` and ``api_key_id``).

``LearnerBankValidator``
    Confines a learner caller to the learner's own bank (``u-<keycloak sub>``).
    The bank is shared: every app reads all of it. Every write must carry exactly
    the tag ``app:<name>`` for the writing app (the write tag scope refuses
    anything else), and only items carrying an app's tag can be changed or
    deleted by it. General banks are readable by every app and writable only
    under the general grant, on the same terms. Apps may retain, update and
    delete their own notes and nothing else: config changes, server-side LLM
    features, knowledge pages, directives and the other bank operations are
    refused. The admin client is limited to the forget sequence, and a deleted
    bank is fenced so nothing can recreate it until in-flight work has drained.

``OLMemoryDefense``
    The built-in sensitive-data screen, plus email addresses and phone numbers.

Config (lowercased, prefix stripped by the loader):
    HINDSIGHT_API_TENANT_ISSUER, _AUDIENCE, _JWKS_URL, _CLIENTS
    ("client_id=app,..."), _ADMIN_CLIENTS ("client_id,..."), _GENERAL_SCOPE
    (default "ol-memory-general"), _SCHEMA (default "public").
    HINDSIGHT_API_OPERATION_VALIDATOR_DATABASE_URL, _FENCE_SECONDS (default 3600).
"""

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

import asyncpg
import jwt
from hindsight_api.extensions.builtin.memory_defense_regex import (
    MemoryDefenseRegexExtension,
)
from hindsight_api.extensions.memory_defense import (
    DefenseAction,
    DefenseDecision,
    apply_redaction,
)
from hindsight_api.extensions.operation_validator import (
    BankListScope,
    BankReadContext,
    BankReadOperation,
    BankWriteContext,
    BankWriteOperation,
    CreateBankContext,
    MemoryUpdateContext,
    MentalModelGetContext,
    MentalModelRefreshContext,
    OperationValidatorExtension,
    PrecheckContext,
    PrecheckOperation,
    RecallContext,
    ReflectContext,
    RetainContext,
    TagScopeContext,
    ValidationResult,
)
from hindsight_api.extensions.tenant import (
    AuthenticationError,
    Tenant,
    TenantContext,
    TenantExtension,
)
from hindsight_api.models import RequestContext

logger = logging.getLogger(__name__)

LEARNER_HEADER = "x-ol-learner"
BANK_PREFIX = "u-"
ADMIN_APP = "platform-admin"
# api_key_id of a general-grant caller. Not UUID-shaped, so it never names a
# learner bank.
GENERAL_PRINCIPAL = "general"
DEFAULT_GENERAL_SCOPE = "ol-memory-general"
# The learner id is the Keycloak user id (sub), a UUID shared by every app, so
# one learner has one bank whichever app writes. Bank ids are VARCHAR(64).
LEARNER_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
GENERAL_BANK_RE = re.compile(r"^g-[a-z0-9][a-z0-9-]{0,61}$")
DEFAULT_FENCE_SECONDS = 3600
JWKS_CACHE_SECONDS = 600

# The bank writes an app or general-grant caller may make, beyond retain: its
# own notes, retagged or removed. Everything else (memory unit edits, knowledge
# pages and folders, directives, mental models, webhooks, config, imports,
# consolidation) is refused. Memory Defense screens only retain, so a unit edit
# would store its text unredacted; an app replaces a note by retaining it again.
# A delete stays allowed while the bank is fenced.
_APP_WRITES = {
    BankWriteOperation.DELETE_DOCUMENT,
    BankWriteOperation.UPDATE_DOCUMENT,
}
_FENCE_EXEMPT_WRITES = {BankWriteOperation.DELETE_DOCUMENT}

# What the forget sequence needs to see: the bank's operations, to drain them.
_ADMIN_READS = {
    BankReadOperation.LIST_OPERATIONS,
    BankReadOperation.GET_OPERATION_STATUS,
}

_PRECHECK_WRITES = {PrecheckOperation.RETAIN, PrecheckOperation.FILES_RETAIN}
# Server-side LLM features. The deployment runs with no LLM provider, so the
# engine refuses them too, but only after a mental model row exists and with a
# 500. Refusing here happens before anything is read or written.
_PRECHECK_REFUSED = {
    PrecheckOperation.DRY_RUN_EXTRACT,
    PrecheckOperation.MENTAL_MODEL_CREATE,
    PrecheckOperation.MENTAL_MODEL_REFRESH,
    PrecheckOperation.REFLECT,
}

# The fence table, and a trigger that refuses any insert into a fenced bank.
#
# The validator checks a worker task once, when it starts, so the hooks alone
# cannot stop a task that was already running when the fence went up. Hindsight
# also lets some of a retain's concurrent sub-batches keep writing after the task
# itself has failed and been rescheduled: in the harness, a retain whose
# embeddings timed out inserted 24 notes 45s after its learner's bank had been
# deleted, with nothing logged. The trigger closes that at the store, whatever
# the writer.
#
# It goes on every table with a bank_id except bookkeeping: the operation log,
# audit log and stats cache the forget sequence writes, and the maintenance
# queues a note delete writes. A content table added by a later migration is
# covered without a change here; it is reapplied on every start. In public,
# beside Hindsight's own tables, because the deployed role can create tables and
# triggers there but not schemas. The lock timeout keeps the DDL, which locks
# each table, from queueing behind long writes and stalling the request that
# runs it; it fails and is retried on the next use instead.
_FENCE_SCHEMA_SQL = """
SET lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS public.ol_memory_bank_fence (
    bank_id text PRIMARY KEY,
    fenced_until timestamptz NOT NULL
);
DO $do$
DECLARE
    tbl text;
    guarded boolean;
BEGIN
    -- Every process runs this on start; one at a time.
    PERFORM pg_advisory_xact_lock(hashtext('ol_memory_bank_fence'));
    CREATE OR REPLACE FUNCTION public.ol_memory_refuse_fenced() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, public AS $fn$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM public.ol_memory_bank_fence
            WHERE bank_id = NEW.bank_id AND fenced_until > now()
        ) THEN
            RAISE EXCEPTION 'bank % is being deleted', NEW.bank_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END
    $fn$;
    -- Excluded tables lose a trigger an earlier version put on them, so the
    -- list below stays the whole truth.
    FOR tbl, guarded IN
        SELECT c.table_name, c.table_name <> ALL (ARRAY[
            'async_operations', 'audit_log', 'bank_stats_cache',
            'entity_maintenance_queue', 'graph_maintenance_queue',
            'ol_memory_bank_fence'
        ])
        FROM information_schema.columns c
        JOIN information_schema.tables t USING (table_schema, table_name)
        WHERE c.table_schema = 'public'
          AND c.column_name = 'bank_id'
          AND t.table_type = 'BASE TABLE'
    LOOP
        IF guarded THEN
            EXECUTE format(
                'CREATE OR REPLACE TRIGGER ol_memory_refuse_fenced '
                'BEFORE INSERT ON public.%I FOR EACH ROW '
                'EXECUTE FUNCTION public.ol_memory_refuse_fenced()',
                tbl
            );
        ELSE
            EXECUTE format(
                'DROP TRIGGER IF EXISTS ol_memory_refuse_fenced ON public.%I', tbl
            );
        END IF;
    END LOOP;
END
$do$;
RESET lock_timeout;
"""

_CONTACT_PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    (
        "phone",
        re.compile(
            r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}"
            r"(?!\d)"
        ),
    ),
]


def _parse_map(raw: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in raw.split(",") if pair)


def _redact(text: str) -> str:
    """Apply the built-in secret and card screen, then the contact patterns."""
    text = apply_redaction(text).content
    for label, pattern in _CONTACT_PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def _redact_item(item: dict[str, Any]) -> dict[str, Any]:
    """Redact every caller-written text field of a retain item.

    Memory Defense screens only ``content``, and only in the worker: a retain's
    ``context``, ``metadata`` and ``entities`` are stored as given, and an async
    retain's items wait in its operation row before the worker sees them.
    """
    item = dict(item)
    for field in ("content", "context"):
        if isinstance(item.get(field), str):
            item[field] = _redact(item[field])
    if item.get("metadata"):
        item["metadata"] = {
            _redact(str(key)): _redact(value) if isinstance(value, str) else value
            for key, value in item["metadata"].items()
        }
    if item.get("entities"):
        item["entities"] = [
            {**entity, "text": _redact(str(entity.get("text", "")))}
            for entity in item["entities"]
        ]
    return item


class OLMemoryDefense(MemoryDefenseRegexExtension):
    """The built-in secret and card screen, plus emails and phone numbers."""

    async def screen(
        self, *, policy, bank_id, document_id, content, tags
    ) -> DefenseDecision:
        """Redact contact details on top of the built-in screen's decision."""
        decision = await super().screen(
            policy=policy,
            bank_id=bank_id,
            document_id=document_id,
            content=content,
            tags=tags,
        )
        if not policy.enabled or decision.action is DefenseAction.BLOCK:
            return decision
        text = (
            decision.redacted_content
            if decision.redacted_content is not None
            else content
        )
        matched = list(decision.matched_types)
        for label, pattern in _CONTACT_PATTERNS:
            text, n = pattern.subn(f"[REDACTED:{label}]", text)
            if n:
                matched.append(label)
        if matched == decision.matched_types:
            return decision
        return DefenseDecision(
            action=DefenseAction.REDACT,
            detector="sensitive_data",
            message=f"Sensitive data pattern matched: {', '.join(matched)}",
            redacted_content=text,
            matched_types=matched,
            hits=decision.hits,
        )


class KeycloakTenantExtension(TenantExtension):
    """Authenticate every request with a Keycloak token issued for the service."""

    def __init__(self, config: dict[str, str]):
        """Read the issuer, audience and client grants from the extension config."""
        super().__init__(config)
        self.issuer = config["issuer"]
        self.audience = config["audience"]
        self.clients = _parse_map(config["clients"])
        self.admin_clients = {
            c for c in config.get("admin_clients", "").split(",") if c
        }
        self.general_scope = config.get("general_scope", DEFAULT_GENERAL_SCOPE)
        self.schema = config.get("schema", "public")
        # No per-key cache: it never expires, so a key Keycloak removed would
        # stay trusted. The JWKS set cache's lifespan bounds that instead.
        self._jwks = jwt.PyJWKClient(config["jwks_url"], lifespan=JWKS_CACHE_SECONDS)

    async def authenticate(self, context: RequestContext) -> TenantContext:
        """Verify the bearer token and record the app and learner it acts for."""
        if not context.api_key:
            msg = "missing bearer token"
            raise AuthenticationError(msg)
        try:
            # PyJWKClient fetches over blocking urllib on a cache miss.
            key = await asyncio.to_thread(
                self._jwks.get_signing_key_from_jwt, context.api_key
            )
            claims = jwt.decode(
                context.api_key,
                key.key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud", "azp", "sub"]},
            )
        except jwt.PyJWTError as exc:
            msg = f"invalid token: {exc}"
            raise AuthenticationError(msg) from exc

        client = claims["azp"]
        if client in self.admin_clients:
            app = ADMIN_APP
        elif client in self.clients:
            app = self.clients[client]
        else:
            msg = f"client {client!r} is not allowed"
            raise AuthenticationError(msg)

        # Keycloak marks a client-credentials token with a client_id claim and no
        # user session (sid); a learner's token, including one obtained by token
        # exchange, is the reverse.
        is_service_account = "client_id" in claims or "sid" not in claims
        if app == ADMIN_APP:
            # The forget path runs on account deletion, when no learner token
            # exists, so the admin client names the learner. The validator limits
            # it to the forget sequence.
            if not is_service_account:
                msg = "the admin client must use client credentials"
                raise AuthenticationError(msg)
            learner = context.extra_headers.get(LEARNER_HEADER)
            if learner is None or not LEARNER_ID_RE.match(learner):
                msg = f"missing or malformed {LEARNER_HEADER}"
                raise AuthenticationError(msg)
        elif is_service_account:
            # The general grant is a Keycloak client scope, so which clients hold
            # it is managed in Keycloak. It is honoured only on a
            # client-credentials token, so a learner token never carries it into
            # general memory.
            if self.general_scope not in claims.get("scope", "").split():
                msg = (
                    "apps must call with a learner token obtained by token "
                    "exchange, or a client-credentials token with the "
                    f"{self.general_scope} scope"
                )
                raise AuthenticationError(msg)
            learner = GENERAL_PRINCIPAL
        else:
            # An app acts for a learner only with a token exchanged from that
            # learner's own session, so the learner is whoever Keycloak says the
            # token is for; no header can name another.
            learner = claims["sub"]

        context.tenant_id = app
        context.api_key_id = learner
        return TenantContext(schema_name=self.schema)

    async def list_tenants(self) -> list[Tenant]:
        """Return the one schema every caller shares."""
        return [Tenant(schema=self.schema)]

    async def get_tenant_config(
        self,
        context: RequestContext,  # noqa: ARG002
    ) -> dict[str, object]:
        """Turn on redaction, the store's backstop for the never-remember list.

        It catches what a pattern recognizes (secrets, emails, phone numbers).
        Names and assessment content are not pattern-matchable and stay the
        extraction gate's job in ol-llm.
        """
        return {
            "memory_defense": {
                "enabled": True,
                "rules": [{"on": "sensitive_data", "action": "redact"}],
            }
        }

    async def get_allowed_config_fields(
        self,
        context: RequestContext,  # noqa: ARG002
        bank_id: str,  # noqa: ARG002
    ) -> set[str] | None:
        """Allow no per-bank config changes.

        Mission, extraction mode, audit logging and document-text storage are
        platform settings.
        """
        return set()


class LearnerBankValidator(OperationValidatorExtension):
    """Decide which banks a caller may read and write, and fence deleted ones."""

    def __init__(self, config: dict[str, str]):
        """Read the fence database and duration from the extension config."""
        super().__init__(config)
        self.database_url = config["database_url"]
        self.fence = timedelta(
            seconds=int(config.get("fence_seconds", DEFAULT_FENCE_SECONDS))
        )
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()

    async def _db(self) -> asyncpg.Pool:
        # Hindsight only calls on_startup for tenant and HTTP extensions
        # (api/http.py:5214-5219), and the worker calls neither, so the pool is
        # created on first use.
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    pool = await asyncpg.create_pool(
                        self.database_url, min_size=1, max_size=5
                    )
                    try:
                        await pool.execute(_FENCE_SCHEMA_SQL)
                    except BaseException:
                        await pool.close()
                        raise
                    self._pool = pool
        return self._pool

    @staticmethod
    def _owns(bank_id: str, rc: RequestContext) -> bool:
        return (
            rc.api_key_id is not None
            and LEARNER_ID_RE.match(rc.api_key_id) is not None
            and bank_id == f"{BANK_PREFIX}{rc.api_key_id}"
        )

    def _access(
        self,
        bank_id: str,
        rc: RequestContext,
        *,
        write: bool,
        admin_allowed: bool = False,
    ) -> ValidationResult:
        # Worker tasks carry tenant_id/api_key_id from the submitting request but
        # were authorized at submit time; only the fence applies to them.
        if rc.internal:
            return ValidationResult.accept()
        if rc.tenant_id == ADMIN_APP and not admin_allowed:
            return ValidationResult.reject(
                "the admin client may only run the forget sequence"
            )
        if GENERAL_BANK_RE.match(bank_id) and rc.tenant_id != ADMIN_APP:
            if not write or rc.api_key_id == GENERAL_PRINCIPAL:
                return ValidationResult.accept()
            return ValidationResult.reject(
                "writing general memory needs the general grant", status_code=403
            )
        if self._owns(bank_id, rc):
            return ValidationResult.accept()
        # 404 rather than 403, so a caller cannot probe for other learners' banks.
        return ValidationResult.reject("bank not found", status_code=404)

    # Both sides of the fence use the database's clock, as the trigger does.
    async def _fenced(self, bank_id: str) -> bool:
        return await (await self._db()).fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM public.ol_memory_bank_fence
                WHERE bank_id = $1 AND fenced_until > now()
            )
            """,
            bank_id,
        )

    async def _raise_fence(self, bank_id: str) -> None:
        await (await self._db()).execute(
            """
            INSERT INTO public.ol_memory_bank_fence (bank_id, fenced_until)
            VALUES ($1, now() + $2)
            ON CONFLICT (bank_id) DO UPDATE SET fenced_until = EXCLUDED.fenced_until
            """,
            bank_id,
            self.fence,
        )
        logger.info("fence raised on %s for %s", bank_id, self.fence)

    async def _write(self, bank_id: str, rc: RequestContext) -> ValidationResult:
        result = self._access(bank_id, rc, write=True)
        if not result.allowed:
            return result
        if await self._fenced(bank_id):
            return ValidationResult.reject("bank is being deleted", status_code=409)
        return result

    async def precheck(self, ctx: PrecheckContext) -> ValidationResult:
        """Refuse server-side LLM features, and anyone else's bank, before parsing.

        Writes are fence-checked here too: a retain with attachments stores the
        file bytes before validate_retain runs, in a table with no bank_id for
        the trigger to guard.
        """
        if ctx.operation in _PRECHECK_REFUSED:
            return ValidationResult.reject(
                f"{ctx.operation} is not enabled", status_code=403
            )
        if ctx.operation in _PRECHECK_WRITES:
            return await self._write(ctx.bank_id, ctx.request_context)
        return self._access(ctx.bank_id, ctx.request_context, write=False)

    async def validate_retain(self, ctx: RetainContext) -> ValidationResult:
        """Allow a text retain into a bank the caller may write, not fenced.

        Notes are text. Attachment bytes live in a table with no bank_id, out of
        the fence trigger's reach, and refusing here reclaims bytes the engine
        has already stored.

        An app's document ids carry its name, so one app cannot take a key
        another app will write: the engine refuses to replace a document under
        another app's tag.

        Every item is redacted here, before the engine stores anything: Memory
        Defense runs later and screens only ``content``.
        """
        if ctx.attachments:
            return ValidationResult.reject(
                "attachments are not enabled", status_code=403
            )
        rc = ctx.request_context
        prefix = f"{rc.tenant_id}."
        if not rc.internal and not all(
            str(item.get("document_id") or "").startswith(prefix)
            for item in ctx.contents
        ):
            return ValidationResult.reject(
                f"document ids must start with {prefix!r}", status_code=403
            )
        result = await self._write(ctx.bank_id, rc)
        if not result.allowed:
            return result
        return ValidationResult.accept_with(
            contents=[_redact_item(item) for item in ctx.contents]
        )

    async def validate_recall(self, ctx: RecallContext) -> ValidationResult:
        """Allow a recall from a bank the caller may read."""
        return self._access(ctx.bank_id, ctx.request_context, write=False)

    async def validate_reflect(
        self,
        ctx: ReflectContext,  # noqa: ARG002
    ) -> ValidationResult:
        """Refuse reflect, which runs server-side LLM calls."""
        return ValidationResult.reject("reflect is not enabled", status_code=403)

    async def validate_create_bank(self, ctx: CreateBankContext) -> ValidationResult:
        """Allow creating only a bank the caller may write."""
        return await self._write(ctx.bank_id, ctx.request_context)

    async def validate_bank_read(self, ctx: BankReadContext) -> ValidationResult:
        """Allow reads of a bank the caller may read; the admin sees operations."""
        admin_allowed = ctx.operation in _ADMIN_READS
        return self._access(
            ctx.bank_id, ctx.request_context, write=False, admin_allowed=admin_allowed
        )

    async def validate_bank_write(self, ctx: BankWriteContext) -> ValidationResult:
        """Allow an app's own note changes; only the platform deletes a bank."""
        rc = ctx.request_context
        if ctx.operation == BankWriteOperation.DELETE_BANK:
            if not rc.internal and rc.tenant_id != ADMIN_APP:
                return ValidationResult.reject(
                    "only the platform may delete a learner's memory"
                )
            result = self._access(ctx.bank_id, rc, write=True, admin_allowed=True)
            if result.allowed:
                await self._raise_fence(ctx.bank_id)
            return result
        if ctx.operation == BankWriteOperation.CANCEL_OPERATION:
            return self._access(ctx.bank_id, rc, write=True, admin_allowed=True)
        if not rc.internal and ctx.operation not in _APP_WRITES:
            return ValidationResult.reject(
                f"{ctx.operation} is not enabled", status_code=403
            )
        if ctx.operation in _FENCE_EXEMPT_WRITES:
            return self._access(ctx.bank_id, rc, write=True)
        return await self._write(ctx.bank_id, rc)

    async def validate_mental_model_get(
        self, ctx: MentalModelGetContext
    ) -> ValidationResult:
        """Allow reading a mental model from a bank the caller may read."""
        return self._access(ctx.bank_id, ctx.request_context, write=False)

    async def validate_mental_model_refresh(
        self,
        ctx: MentalModelRefreshContext,  # noqa: ARG002
    ) -> ValidationResult:
        """Refuse mental model refresh, which runs server-side LLM calls."""
        return ValidationResult.reject("mental models are not enabled", status_code=403)

    async def validate_memory_update(
        self, ctx: MemoryUpdateContext
    ) -> ValidationResult:
        """Allow an internal memory update in a writable, unfenced bank.

        validate_bank_write has already refused the update for apps.
        """
        return await self._write(ctx.bank_id, ctx.request_context)

    async def resolve_write_tag_scope(self, ctx: TagScopeContext) -> list[str] | None:
        """Require every app write to carry exactly the app's own tag.

        The engine refuses an untagged write, or one with any other tag, and lets
        an app change or delete only items carrying its tag. There is no read
        tag scope: a learner's memory is shared, so every app
        reads all of it. A write scope also makes app callers tag-scoped to the
        engine, which refuses them whole-bank operations (export, config, clear,
        delete, statistics, operation logs).
        """
        rc = ctx.request_context
        if rc.internal or rc.tenant_id == ADMIN_APP:
            return None
        return [f"app:{rc.tenant_id}"]

    async def bank_list_scope(
        self, request_context: RequestContext
    ) -> BankListScope | None:
        """List only the learner's own bank.

        General banks are addressed by name, not discovered, so no caller lists
        them.
        """
        if request_context.api_key_id == GENERAL_PRINCIPAL:
            return BankListScope(bank_ids=[])
        return BankListScope(bank_ids=[f"{BANK_PREFIX}{request_context.api_key_id}"])
