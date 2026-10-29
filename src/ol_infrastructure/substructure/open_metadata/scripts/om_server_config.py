"""OpenMetadata server-side configuration bootstrap.

Applies settings that live in the OM server's own database and cannot be
expressed through connector workflow configs.  Designed to be idempotent —
safe to re-run on every deploy; each step checks current state before writing.

Tag configuration is declared in ``TAG_CONFIG`` below.  Each entry maps a
fully-qualified tag name to the subset of tag fields we want to manage.
Fields not listed are left untouched.  To activate a tag for
AutoClassificationWorkflow scanning, set ``autoClassificationEnabled: True``.

Currently managed tags
──────────────────────
PII.Sensitive / PII.NonSensitive
  Both ship with ``autoClassificationEnabled=False``.  The
  AutoClassificationWorkflow (trino-classifier CronOMJob) queries the server
  for tags where this flag is True before it starts scanning; if none are
  found it processes ~2,700 tables but writes 0 tags.

PersonalData.Personal / PersonalData.SpecialCategory
  GDPR personal-data categories.  These ship with *zero recognizers*, so
  ``autoClassificationEnabled=False`` is intentional here: enabling the flag
  without recognizers is a no-op.  Once recognizers are configured in the OM
  UI (Settings → Classifications → PersonalData → <tag> → Edit → Recognizers),
  flip the flag to True and redeploy to start applying the tag automatically.

Domains are declared in ``DOMAIN_CONFIG``.  Which assets belong to each is set
from ol-data-platform's dbt manifest and domain rules, not here: by the
om-ingest-dbt job for dbt's tables, and by the om-ingest-domains job
(domain_assignment.py) for the other lake tables and Superset.

Custom properties are declared in ``CUSTOM_PROPERTY_CONFIG``, per entity type.
A missing property is added and one whose description or displayName differs
is put back.  A property removed from the config is left on the type, because
deleting it drops its value from every entity that has one.

Access policies, roles, teams and bots are not managed here: the stack
declares them with the OpenMetadata providers
(ol_infrastructure.providers.openmetadata), which connect as an admin, from
substructure/open_metadata access_control.py.  This job runs after the teams
exist, because the glossary is owned by one.

The business glossary is declared in ``GLOSSARY_CONFIG`` and ``GLOSSARY_TERMS``,
and this code is the source of truth for it.  The glossary and each term are
created when missing.  After that the glossary's description, and each term's
description, synonyms, references and relatedTerms, are patched back to what is
declared here whenever the job runs (on every change to this script), so an
edit to them in the OpenMetadata UI is overwritten: change them here.  The job's
bot is not a glossary reviewer, so OpenMetadata's glossary approval workflow
puts a patched term back In Review and opens an approval task for the owning
team; rejecting it rolls the change back until the next run.

displayName and mutuallyExclusive are set only on create.  The bot's policy
denies EditDisplayName, and OpenMetadata ignores a change to mutuallyExclusive
on a glossary or term that exists.

A term a steward soft-deletes or moves under another parent is left alone, and
so is a missing term declared under it or under a soft-deleted glossary.
Renaming a term or hard-deleting it frees its name, so the next run creates it
again: rename or remove it in ``GLOSSARY_TERMS`` as well.  Which columns carry
each term is set from ol-data-platform's dbt column meta
(``meta.openmetadata.glossary``).
"""

import os
import sys
from collections.abc import Callable
from functools import partial
from typing import Any

import requests

OM_SERVER_URL = os.environ["OM_SERVER_URL"].rstrip("/")
_AUTH_HEADER = {"Authorization": f"Bearer {os.environ['OM_BOT_JWT_TOKEN']}"}
_PATCH_HEADER = {**_AUTH_HEADER, "Content-Type": "application/json-patch+json"}

# Desired state for each tag.
#
# Supported fields (all optional; omitted fields are not touched):
#   autoClassificationEnabled  bool   Whether the tag is a candidate for
#                                     AutoClassificationWorkflow scanning.
#                                     Has no effect if the tag has no
#                                     recognizers configured.
#   autoClassificationPriority int    Tie-breaking priority when multiple tags
#                                     could apply to the same column (higher
#                                     wins).  Default in OM is 50.
#   description                str    The tag's description, as Markdown.
TAG_CONFIG: dict[str, dict[str, Any]] = {
    # ── PII classification ────────────────────────────────────────────────
    # Ships with 45 content + column-name recognizers (email, SSN, credit
    # card, phone, person-name, spaCy NER, …).  Flag defaults False.
    "PII.Sensitive": {
        "autoClassificationEnabled": True,
        "autoClassificationPriority": 50,
    },
    # Ships with 8 recognizers (date, phone, URL, location, spaCy NRP, …).
    "PII.NonSensitive": {
        "autoClassificationEnabled": True,
        "autoClassificationPriority": 40,  # lower priority than Sensitive
    },
    # ── PersonalData classification (GDPR) ───────────────────────────────
    # Ships with 0 recognizers — autoClassification is intentionally False
    # until recognizers are defined.  To activate, add recognizers via the
    # OM UI then flip the flag to True here and redeploy.
    "PersonalData.Personal": {
        "autoClassificationEnabled": False,
        "autoClassificationPriority": 50,
    },
    "PersonalData.SpecialCategory": {
        "autoClassificationEnabled": False,
        "autoClassificationPriority": 60,  # higher than Personal when active
    },
    # ── Tier classification ──────────────────────────────────────────────
    # Tier is criticality only.  The tiers themselves are assigned from dbt
    # meta (ol-data-platform src/ol_dbt/dbt_project.yml) by the om-ingest-dbt
    # job; these descriptions replace the generic ones OpenMetadata ships so
    # the UI says what each tier means here.
    "Tier.Tier1": {
        "description": (
            "Assets with a data contract. Assigned per asset by the data platform team."
        ),
    },
    "Tier.Tier2": {
        "description": "Dimensional, mart and reporting models without a contract.",
    },
    "Tier.Tier3": {"description": "Intermediate models."},
    "Tier.Tier4": {"description": "Staging and external models."},
    "Tier.Tier5": {"description": "Raw and migration tables."},
}

# Fields from TAG_CONFIG that map directly to a JSON Patch /path.
_PATCHABLE_FIELDS: list[str] = [
    "autoClassificationEnabled",
    "autoClassificationPriority",
    "description",
]


# Desired state for each domain, keyed by domain name.  Domains split the
# warehouse by business area rather than source platform, because dim_user and
# the combined marts span every platform.  Assets are assigned to them by the
# rules in ol-data-platform src/ol_dbt/openmetadata_domains.yml, which refer to
# these names; the om-ingest-dbt and om-ingest-domains jobs skip a name that
# does not exist here.
# Every domain is "Aggregate": each holds raw tables from the source apps as
# well as the models built for consumers.
DOMAIN_CONFIG: dict[str, dict[str, str]] = {
    "learners-identity": {
        "displayName": "Learners & Identity",
        "description": (
            "Who our learners are: user accounts on each platform, profiles, "
            "and the cross-platform identity in dim_user."
        ),
    },
    "courses-content": {
        "displayName": "Courses & Content",
        "description": (
            "Courses, runs, programs and their content: catalog pages, course "
            "structure, videos, problems, instructors, topics, and the "
            "resources published to MIT Learn."
        ),
    },
    "enrollments-progress": {
        "displayName": "Enrollments & Progress",
        "description": (
            "Enrollments in course runs and programs, grades, certificates and "
            "learner progress."
        ),
    },
    "commerce": {
        "displayName": "Commerce",
        "description": (
            "Products, orders, payments, discounts, coupons and financial assistance."
        ),
    },
    "b2b": {
        "displayName": "B2B",
        "description": (
            "Organizations, contracts and their learners, and corporate sales."
        ),
    },
    "feedback": {
        "displayName": "Feedback",
        "description": (
            "Learner feedback from support tickets, forums, chatbot ratings and "
            "content feedback, with its categories and sentiment."
        ),
    },
    "engagement": {
        "displayName": "Engagement",
        "description": (
            "What learners do in courses and on MIT Learn: tracking events, "
            "video, problem, discussion and navigation activity, and chatbot use."
        ),
    },
}

_DOMAIN_TYPE = "Aggregate"

# displayName is set when a domain is created and never patched afterwards:
# IngestionBotPolicy, which this job's token carries, denies EditDisplayName
# on every resource.
_DOMAIN_PATCHABLE_FIELDS: list[str] = ["description", "domainType"]

# Custom properties, keyed by entity type and then property name.  propertyType
# names one of OpenMetadata's property types (e.g. "string"), and cannot be
# changed once the property exists.
#
# ol-data-platform's `ol-dbt metrics sync` writes metricFileSha256 on every
# Metric it syncs, so the sync authors a version of the metric that it takes as
# its drift baseline.  The server rejects the sync's PATCH without it.
CUSTOM_PROPERTY_CONFIG: dict[str, dict[str, dict[str, str]]] = {
    "metric": {
        "metricFileSha256": {
            "propertyType": "string",
            "displayName": "Metric file SHA-256",
            "description": (
                "SHA-256 of the metrics/ file this metric was last synced from. "
                "Written by ol-dbt metrics sync; do not edit."
            ),
        },
    },
}

# A PUT of an existing property replaces it, and the server records a change to
# either field.  The PUT is authorized as Create on the type, so the bot's
# EditDisplayName deny does not apply.
_CUSTOM_PROPERTY_UPDATABLE_FIELDS: list[str] = ["description", "displayName"]

# The business glossary.  Terms are referred to by FQN from ol-data-platform's
# dbt column meta (e.g. "open-learning.course-run"), so renaming one breaks its
# column links; the om-ingest-dbt job skips an FQN that does not exist.
#
# Definitions follow what the dimensional models in ol-data-platform
# (src/ol_dbt/models/dimensional) implement.  The glossary's reviewers are its
# owning team, so OpenMetadata creates each term as a draft and opens an
# approval task for that team.
GLOSSARY_NAME = "open-learning"
_GLOSSARY_TEAM = "data-platform"

GLOSSARY_CONFIG: dict[str, Any] = {
    "displayName": "Open Learning",
    "description": (
        "What the terms used about MIT Open Learning's learners, courses and "
        "enrollments mean in the warehouse, and the privacy categories its "
        "columns fall into. Each term names the dimensional model that decides "
        "it."
    ),
    # A column can carry several terms, e.g. Direct identifier and Directory
    # information.  Exclusivity is set per parent term below instead.
    "mutuallyExclusive": False,
}

_GLOSSARY_PATCHABLE_FIELDS: list[str] = ["description"]

_NIST_800_122 = {
    "name": "NIST SP 800-122",
    "endpoint": "https://nvlpubs.nist.gov/nistpubs/legacy/sp/nistspecialpublication800-122.pdf",
}
_FERPA_99_3 = {
    "name": "34 CFR 99.3 (FERPA definitions)",
    "endpoint": "https://www.ecfr.gov/current/title-34/subtitle-A/part-99/subpart-A/section-99.3",
}

# Created in order, so a parent term comes before its children.  ``parent`` is
# the name of a term declared earlier, and so is each name in ``relatedTerms``.
# OpenMetadata stores a related-term link on both terms, so it is declared once,
# on the later term; the earlier term leaves links to later terms to them.
#
# description, synonyms, references and relatedTerms are put back to what is
# declared here on every run, overwriting edits made in the OpenMetadata UI
# (see the module docstring).  displayName and mutuallyExclusive are set only
# when the term is created.
GLOSSARY_TERMS: list[dict[str, Any]] = [
    {
        "name": "platform",
        "displayName": "Platform",
        "synonyms": ["platform_code", "source platform"],
        "description": (
            "The system a record came from, by its code in `dim_platform`: "
            "`mitxonline` (MITx Online), `mitxpro` (MIT xPRO), `edxorg` (edX.org), "
            "`residential` (MITx Residential), `bootcamps`, `emeritus` and "
            "`global_alumni` (xPRO courses run on partner platforms), "
            "`micromasters`, `ocw` and `mitlearn`. Facts and `dim_course_run` "
            "carry it as `platform`, `dim_course` as `primary_platform` and "
            "`dim_program` as `platform_code`.\n\n"
            "A course or course run readable id is unique only within a platform: "
            "courses that moved from edX.org to MITx Online kept their ids. A join "
            "on `courserun_readable_id` or `course_readable_id` must also match "
            "the platform, or it fans out.\n\n"
            "MicroMasters is not an Open edX instance of its own. Its course runs "
            "are stored under the platform that ran them, and `micromasters` "
            "appears only on programs and certificates."
        ),
    },
    {
        "name": "user",
        "displayName": "User",
        "synonyms": ["account holder", "person"],
        "description": (
            "A person, as `dim_user` resolves them from their accounts on every "
            "platform. Accounts are the same person when they share a lowercased "
            "email address or were already linked upstream (MITx Online with "
            "edX.org, MIT Learn accounts sharing a global id). For xPRO, Bootcamps, "
            "Residential, Emeritus and Global Alumni the email is the only signal, "
            "so the same person under two emails is two users.\n\n"
            "`user_pk` is derived from the person's most durable account id, so it "
            "survives an email change; `user_pk_source` says which id it came "
            "from. Facts refer to it as `user_fk`.\n\n"
            "A user need not have enrolled in anything, e.g. an MIT Learn sign-up. "
            "See Learner."
        ),
    },
    {
        "name": "learner",
        "displayName": "Learner",
        "synonyms": ["student", "enrollee"],
        "description": (
            "A user who has enrolled in a course run or a program: a `user_fk` "
            "that appears in `tfact_enrollment`. `dim_user` has no learner flag "
            "and holds every account, enrolled or not, so counting its rows counts "
            "users, not learners.\n\n"
            "`afact_learner_courserun_progress` has one row per learner and course "
            "run, for course enrollments whose `user_fk` and `courserun_fk` both "
            "resolved. `afact_learner_courserun_daily_activity` is built from "
            "tracked events, not enrollments, so it also holds users with activity "
            "but no enrollment (e.g. staff previewing a course); join it to "
            "`tfact_enrollment` to keep learners only.\n\n"
            "See Active learner."
        ),
    },
    {
        "name": "course",
        "displayName": "Course",
        "synonyms": ["course concept"],
        "description": (
            "A course as a catalog entry, independent of when it is taught, e.g. "
            "6.00.1x. One current row per (`primary_platform`, "
            "`course_readable_id`) in `dim_course`, which keeps history as SCD "
            "Type 2 (filter on `is_current`). A course has one or more course "
            "runs; enrollments, grades and certificates belong to the runs.\n\n"
            "edX.org, Emeritus and Global Alumni have no course table, so their "
            "courses are built from course runs. Residential has no courses in "
            "`dim_course`, so its runs have a null `course_fk`. OCW courses are in "
            "`dim_course` but have no runs or enrollments in the dimensional "
            "models."
        ),
    },
    {
        "name": "course-run",
        "displayName": "Course run",
        "synonyms": ["run", "course offering", "section"],
        "description": (
            "One offering of a course, with its own dates, enrollment window, "
            "enrollments, grades and certificates. One current row per "
            "(`platform`, `courserun_readable_id`) in `dim_course_run` (SCD Type "
            "2, filter on `is_current`); `course_fk` points at its course.\n\n"
            "`courserun_readable_id` is not unique across platforms (e.g. "
            "`MITx/14.73x/2013_Spring` is on both `edxorg` and `mitxonline`), so "
            "always pair it with `platform`."
        ),
    },
    {
        "name": "program",
        "displayName": "Program",
        "synonyms": ["MicroMasters", "professional certificate program"],
        "description": (
            "A set of courses that leads to a program certificate, e.g. a "
            "MicroMasters program or an xPRO professional certificate. One row per "
            "(`source_id`, `platform_code`) in `dim_program`; "
            "`bridge_program_course` lists its courses and whether each is "
            "required. Programs have no runs.\n\n"
            "A program enrollment is a `tfact_enrollment` row with "
            "`enrollment_type = 'program'`, and the fact holds MITx Online program "
            "enrollments only: xPRO, edX.org and MicroMasters program enrollments "
            "are not in it. On a MicroMasters course enrollment `program_fk` is "
            "looked up from the run's program membership, and on an xPRO course "
            "enrollment it is the program the course was bought under."
        ),
    },
    {
        "name": "enrollment",
        "displayName": "Enrollment",
        "synonyms": ["registration"],
        "description": (
            "A learner's registration in a course run or a program: one "
            "`tfact_enrollment` row per (`enrollment_id`, `platform`, "
            "`enrollment_type`), where `enrollment_type` is `course` or "
            "`program`. The row stays after the learner unenrolls, so counting rows "
            "counts everyone who ever enrolled. Filter on `enrollment_is_active` "
            "for current enrollments (see Active enrollment).\n\n"
            "A learner can hold several enrollments in one run after refunds, "
            "deferrals or transfers; `afact_learner_courserun_progress` reports "
            "the active one, else the newest. Program enrollments in the fact are "
            "MITx Online only (see Program)."
        ),
    },
    {
        "name": "active-enrollment",
        "displayName": "Active enrollment",
        "synonyms": ["currently enrolled"],
        "description": (
            "An enrollment the learner has not left: "
            "`tfact_enrollment.enrollment_is_active = true`, the platform's own "
            'flag. This is the canonical "currently enrolled" test. Do not derive '
            "it from `enrollment_status`, which records why an enrollment changed, "
            "is null on edX.org and Residential, and differs by platform: "
            "`deferred` marks active audit enrollments on MITx Online but inactive "
            "ones on xPRO and Bootcamps.\n\n"
            "It says nothing about whether the learner did anything in the course. "
            "See Active learner."
        ),
    },
    {
        "name": "active-learner",
        "displayName": "Active learner",
        "synonyms": ["engaged learner"],
        "description": (
            "A user with tracked course activity in a period: a row in "
            "`afact_learner_courserun_daily_activity` for that day, month or "
            "course run. The table is built from events, not enrollments, so it "
            "also counts users who never enrolled (e.g. staff previewing a "
            "course); join to `tfact_enrollment` to count enrolled learners "
            "only. Activity is a video play, problem check, course "
            "navigation, discussion event or Open edX chatbot submit; enrolling "
            "and earning a certificate are not activity. "
            "`afact_learner_courserun_progress.last_active_on` is the latest such "
            "day.\n\n"
            "Only MITx Online, xPRO, Residential and edX.org have tracked "
            "activity. The day is the event's date in the source's timezone, not "
            "UTC.\n\n"
            '"Active" has three meanings in the warehouse and this is the '
            "behavioural one. An Active enrollment is a current enrollment, and an "
            "Active account is an account that has not been deactivated."
        ),
    },
    {
        "name": "active-account",
        "displayName": "Active account",
        "synonyms": ["is_active"],
        "description": (
            "A platform account that has not been deactivated: the "
            "`dim_user.user_is_active_on_<platform>` flags. On MITx Online, xPRO, "
            "Residential, Bootcamps and MIT Learn the flag is the account's own "
            "`is_active`, which the applications use to soft-delete accounts. On "
            "edX.org it is derived instead: false when the account's email or "
            "username starts with `retired__user` (a retired account), else true. "
            "Null when the user has no account on that platform.\n\n"
            "It is not a measure of use: an account can be active without ever "
            "being used. See Active learner."
        ),
    },
    {
        "name": "enrollment-mode",
        "displayName": "Enrollment mode",
        "synonyms": ["track", "course mode"],
        "description": (
            "The track a learner enrolled in, as the platform recorded it in "
            "`tfact_enrollment.enrollment_mode`. The common values are `audit` and "
            "`verified` (see Verified); edX.org also has `honor` and `credit`. "
            "Null for Bootcamps, Emeritus and Global Alumni, which have no modes."
        ),
    },
    {
        "name": "verified",
        "displayName": "Verified",
        "synonyms": ["verified track", "verified certificate", "upgraded"],
        "description": (
            "Two related uses.\n\n"
            "As an enrollment mode, `enrollment_mode = 'verified'` is the "
            "certificate track a learner pays for, as opposed to `audit`. The "
            "warehouse also sets it on some DEDP course enrollments, whatever mode "
            "the platform recorded. On edX.org it does so when a fulfilled "
            "MicroMasters order paid for the run. On MITx Online it does so only "
            "for an enrollment with no `enrollment_status`, paid for by a "
            "fulfilled MicroMasters order for the 3T2021, 1T2022 and 2T2022 runs "
            "and by a fulfilled MITx Online order for any other run.\n\n"
            "As a certificate type, `dim_certificate_type.certificate_type_code = "
            "'verified'`. `tfact_certificate` gives it to every MITx Online course "
            "and program certificate, MicroMasters course certificate, edX.org "
            "program certificate and Bootcamps certificate. edX.org course "
            "certificates take the mode recorded on the certificate, and xPRO "
            "certificates are `professional`. "
            "`certificate_requires_id_verification` is a fixed attribute of the "
            "type, not a record that the learner's identity was checked."
        ),
    },
    {
        "name": "certificate",
        "displayName": "Certificate",
        "synonyms": ["credential", "certified"],
        "description": (
            "A certificate a platform issued to a learner for a course run "
            "(`certificate_scope = 'course'`) or a program (`'program'`): one "
            "`tfact_certificate` row per (`certificate_id`, `platform`, "
            "`certificate_scope`), typed by `certificate_type_fk` (see Verified).\n\n"
            "A revoked certificate keeps its row (`certificate_is_revoked`). When "
            "a learner has more than one for a run, `is_current` marks the one "
            "that stands: unrevoked first, then the latest issued. A MicroMasters "
            "course certificate that arrives from both edX.org and MicroMasters "
            "is collapsed to the edX.org row.\n\n"
            "A learner is certified in a run "
            "(`afact_learner_courserun_progress.is_certified`) when they hold an "
            "unrevoked course certificate for it, which does not require a "
            "passing grade. edX.org certificates are never marked revoked, and "
            "Residential, Emeritus and Global Alumni have no certificates in the "
            "warehouse."
        ),
    },
    {
        "name": "pii",
        "displayName": "Personally identifiable information (PII)",
        "synonyms": ["PII", "personal data"],
        "references": [_NIST_800_122],
        "description": (
            "Per NIST SP 800-122, any information about an individual that can be "
            "used to distinguish or trace their identity (e.g. name, email "
            "address, date and place of birth), and any other information that is "
            "linked or linkable to them (e.g. educational, financial and "
            "employment information). In the warehouse that covers `dim_user` and "
            "every fact row that carries a `user_fk`.\n\n"
            "OpenMetadata's auto-classification sets the `PII.Sensitive` and "
            "`PII.NonSensitive` tags from column names and content. The terms "
            "under Identifiability say how directly a column identifies a person, "
            "and the terms under FERPA classification say how FERPA treats it."
        ),
    },
    {
        "name": "identifiability",
        "displayName": "Identifiability",
        "references": [_NIST_800_122],
        "mutuallyExclusive": True,
        "description": (
            "How directly a column identifies a person, after NIST SP 800-122. A "
            "column carries at most one of these terms."
        ),
    },
    {
        "name": "direct-identifier",
        "parent": "identifiability",
        "displayName": "Direct identifier",
        "synonyms": ["direct PII"],
        "references": [_NIST_800_122],
        "description": (
            "A value that identifies one person on its own: a name, an email "
            "address, or a platform user id or username. In `dim_user`: "
            "`full_name`, `email`, `user_global_id`, and every `*_user_id`, "
            "`*_username` and `*_identity_key` column."
        ),
    },
    {
        "name": "indirect-identifier",
        "parent": "identifiability",
        "displayName": "Indirect identifier",
        "synonyms": ["quasi-identifier"],
        "references": [_NIST_800_122, _FERPA_99_3],
        "description": (
            "A value that does not identify a person alone but can in combination "
            "with others, e.g. date of birth, gender, location, employer or job "
            "title. In `dim_user`: `birth_year`, `gender`, `address_country`, "
            "`address_state`, `company`, `job_title`, `industry` and "
            "`highest_education`. A small group broken down by these columns can "
            "single a person out."
        ),
    },
    {
        "name": "ferpa-classification",
        "displayName": "FERPA classification",
        "references": [_FERPA_99_3],
        "mutuallyExclusive": True,
        "description": (
            "How FERPA treats a column of student data. Under 34 CFR 99.3 "
            "directory information is a part of a student's education record, so "
            "a column carries the narrower term: Directory information when the "
            "value is a directory item, Education record otherwise. A column "
            "carries at most one of these terms."
        ),
    },
    {
        "name": "directory-information",
        "parent": "ferpa-classification",
        "displayName": "Directory information",
        "references": [_FERPA_99_3],
        "description": (
            "Information from a student's education record that would not "
            "generally be considered harmful or an invasion of privacy if "
            "disclosed, and that an institution may disclose without consent "
            "after giving notice, unless the student has opted out. 34 CFR 99.3 "
            "lists e.g. name, address, telephone listing, email address, "
            "photograph, date and place of birth, enrollment status, dates of "
            "attendance, and degrees, honors and awards received; each institution "
            "designates its own list. It excludes social security numbers and, "
            "with narrow exceptions, student ID numbers.\n\n"
            "Directory information is still PII. In `dim_user`: `full_name` and "
            "`email`."
        ),
    },
    {
        "name": "education-record",
        "parent": "ferpa-classification",
        "displayName": "Education record",
        "synonyms": ["educational record"],
        "references": [_FERPA_99_3],
        "description": (
            "A record directly related to a student and maintained by the "
            "institution or a party acting for it (34 CFR 99.3), e.g. grades, "
            "assignments and submissions, and financial aid records. Disclosing "
            "the parts that are not directory information needs the student's "
            "written consent unless a FERPA exception applies.\n\n"
            "In the dimensional models: grades (`tfact_grade` and the grade columns of "
            "`afact_learner_courserun_progress`), the financial aid income in "
            "`dim_user`, and the profile's `gender`, `company`, `job_title` and "
            "`industry`."
        ),
    },
]

# The term fields put back on every run.  relatedTerms is reconciled on its own,
# because the server returns links rather than the names declared here.
_GLOSSARY_TERM_PATCHABLE_FIELDS: list[str] = ["description", "synonyms", "references"]


def _apply_patch(label: str, url: str, ops: list[dict[str, Any]]) -> None:
    """Send a JSON Patch, or report that there is nothing to change.

    :param label: What is being reconciled, for the log line.
    :param url: The entity's ``/v1/<collection>/<id>`` URL.
    :param ops: JSON Patch operations; empty when the entity is already right.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    """
    if not ops:
        print(f"[ok]   {label}: already at desired state — skipping")  # noqa: T201
        return

    changed = ", ".join(f"{op['path'].lstrip('/')}={op['value']!r}" for op in ops)
    patch_resp = requests.patch(url, headers=_PATCH_HEADER, json=ops, timeout=30)
    patch_resp.raise_for_status()
    print(f"[done] {label}: patched {changed}")  # noqa: T201


def _reconcile_tag(fqn: str, desired: dict[str, Any]) -> None:
    """Read the current tag state and PATCH only fields that differ.

    Uses a read-before-write pattern to avoid unnecessary audit-log churn.
    Only fields present in *desired* are compared and potentially patched;
    all other tag fields are left untouched.

    :param fqn: Fully-qualified tag name, e.g. ``"PII.Sensitive"``.
    :param desired: Mapping of field names to desired values.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    """
    get_resp = requests.get(
        f"{OM_SERVER_URL}/v1/tags/name/{fqn}",
        headers=_AUTH_HEADER,
        timeout=30,
    )
    get_resp.raise_for_status()
    tag = get_resp.json()

    ops = [
        {"op": "replace", "path": f"/{field}", "value": value}
        for field in _PATCHABLE_FIELDS
        if (value := desired.get(field)) is not None and tag.get(field) != value
    ]

    _apply_patch(fqn, f"{OM_SERVER_URL}/v1/tags/{tag['id']}", ops)


def _get_by_name(
    collection: str,
    name: str,
    include: str | None = None,
    fields: str | None = None,
) -> dict[str, Any] | None:
    """Fetch an entity by name, or None if it does not exist.

    :param collection: The API collection, e.g. ``"teams"``.
    :param name: The entity's name.
    :param include: ``"all"`` to also find a soft-deleted entity; by default
        the server returns only entities that are not deleted.
    :param fields: Comma-separated fields to include, e.g. ``"policies"``.
    :raises requests.HTTPError: On any non-2xx response other than 404.
    """
    params = {"include": include, "fields": fields}
    get_resp = requests.get(
        f"{OM_SERVER_URL}/v1/{collection}/name/{name}",
        headers=_AUTH_HEADER,
        params={k: v for k, v in params.items() if v} or None,
        timeout=30,
    )
    if get_resp.status_code == requests.codes.not_found:
        return None
    get_resp.raise_for_status()
    return get_resp.json()


def _add_ops(
    current: dict[str, Any], wanted: dict[str, Any], fields: list[str]
) -> list[dict[str, Any]]:
    """Build JSON Patch ``add`` operations for the fields that differ.

    ``add`` rather than ``replace``, because the server leaves an empty field
    out of the entity and ``replace`` fails on a missing member.

    :param current: The entity as the server returned it.
    :param wanted: Mapping of field names to desired values.
    :param fields: The fields of *wanted* to compare.
    :returns: One operation per field whose value differs.
    """
    return [
        {"op": "add", "path": f"/{field}", "value": wanted[field]}
        for field in fields
        if current.get(field) != wanted[field]
    ]


def _reconcile_domain(name: str, desired: dict[str, str]) -> None:
    """Create the domain if it is missing, else PATCH the fields that differ.

    :param name: Domain name, e.g. ``"commerce"``.
    :param desired: Mapping of domain field names to desired values.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    """
    wanted = {**desired, "domainType": _DOMAIN_TYPE}
    domain = _get_by_name("domains", name)
    if domain is None:
        post_resp = requests.post(
            f"{OM_SERVER_URL}/v1/domains",
            headers=_AUTH_HEADER,
            json={"name": name, **wanted},
            timeout=30,
        )
        post_resp.raise_for_status()
        print(f"[done] domain {name}: created")  # noqa: T201
        return

    ops = _add_ops(domain, wanted, _DOMAIN_PATCHABLE_FIELDS)
    _apply_patch(f"domain {name}", f"{OM_SERVER_URL}/v1/domains/{domain['id']}", ops)


def _reconcile_custom_property(
    entity_type: str, name: str, desired: dict[str, str]
) -> None:
    """Add the custom property if it is missing, else put back a changed one.

    :param entity_type: The entity type the property extends, e.g. ``"metric"``.
    :param name: Property name, e.g. ``"metricFileSha256"``.
    :param desired: The property's propertyType name, displayName and description.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    :raises ValueError: If the entity type or property type does not exist, or
        the property exists with another property type.
    """
    label = f"{entity_type} custom property {name}"
    entity_def = _get_by_name("metadata/types", entity_type, fields="customProperties")
    if entity_def is None:
        msg = f"entity type {entity_type!r} does not exist"
        raise ValueError(msg)

    current = next(
        (prop for prop in entity_def["customProperties"] if prop["name"] == name),
        None,
    )
    changed: list[str] = []
    if current is not None:
        # The server's updater matches properties on name and type but stores
        # them by name, so a PUT with another type does not replace the
        # property cleanly.  Changing it needs a person.
        if current["propertyType"]["name"] != desired["propertyType"]:
            msg = (
                f"exists with propertyType {current['propertyType']['name']!r}, "
                f"expected {desired['propertyType']!r}"
            )
            raise ValueError(msg)
        changed = [
            field
            for field in _CUSTOM_PROPERTY_UPDATABLE_FIELDS
            if current.get(field) != desired[field]
        ]
        if not changed:
            print(f"[ok]   {label}: already at desired state — skipping")  # noqa: T201
            return

    property_type = _get_by_name("metadata/types", desired["propertyType"])
    if property_type is None:
        msg = f"property type {desired['propertyType']!r} does not exist"
        raise ValueError(msg)

    put_resp = requests.put(
        f"{OM_SERVER_URL}/v1/metadata/types/{entity_def['id']}",
        headers=_AUTH_HEADER,
        json={
            "name": name,
            "displayName": desired["displayName"],
            "description": desired["description"],
            "propertyType": {"id": property_type["id"], "type": "type"},
        },
        timeout=30,
    )
    put_resp.raise_for_status()
    if current is None:
        print(f"[done] {label}: created")  # noqa: T201
    else:
        print(f"[done] {label}: updated {', '.join(changed)}")  # noqa: T201


def _create(collection: str, label: str, body: dict[str, Any]) -> None:
    """POST a new entity.

    :param collection: The API collection, e.g. ``"glossaries"``.
    :param label: What is being created, for the log line.
    :param body: The create request.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    """
    post_resp = requests.post(
        f"{OM_SERVER_URL}/v1/{collection}",
        headers=_AUTH_HEADER,
        json=body,
        timeout=30,
    )
    post_resp.raise_for_status()
    print(f"[done] {label}: created")  # noqa: T201


def _reconcile_glossary() -> None:
    """Create the glossary, owned and reviewed by its team, or patch its description.

    :raises requests.HTTPError: On any non-2xx response from the OM API.
    :raises ValueError: If the owning team does not exist.
    """
    label = f"glossary {GLOSSARY_NAME}"
    glossary = _get_by_name("glossaries", GLOSSARY_NAME, include="all")
    if glossary is not None:
        if not _is_deleted(label, glossary):
            _apply_patch(
                label,
                f"{OM_SERVER_URL}/v1/glossaries/{glossary['id']}",
                _add_ops(glossary, GLOSSARY_CONFIG, _GLOSSARY_PATCHABLE_FIELDS),
            )
        return

    team = _get_by_name("teams", _GLOSSARY_TEAM)
    if team is None:
        msg = f"owning team {_GLOSSARY_TEAM!r} does not exist"
        raise ValueError(msg)
    team_ref = {"id": team["id"], "type": "team"}
    _create(
        "glossaries",
        label,
        {
            "name": GLOSSARY_NAME,
            **GLOSSARY_CONFIG,
            "owners": [team_ref],
            "reviewers": [team_ref],
        },
    )


def _is_deleted(label: str, entity: dict[str, Any]) -> bool:
    """Whether a glossary or term was soft-deleted, logging that it is skipped.

    A soft-deleted glossary or term was deleted by a steward, so it is left
    alone rather than patched or recreated (the server would refuse the name
    anyway).

    :param label: What is being reconciled, for the log line.
    :param entity: The entity, fetched with ``include=all``.
    """
    if entity.get("deleted"):
        print(f"[skip] {label}: deleted in OpenMetadata — left alone")  # noqa: T201
        return True
    return False


def _term_fqn(name: str) -> str:
    """Return the FQN of the term declared in ``GLOSSARY_TERMS`` as *name*.

    :param name: The term's name.
    :raises StopIteration: If no term of that name is declared.
    """
    term = next(term for term in GLOSSARY_TERMS if term["name"] == name)
    parent = term.get("parent")
    return f"{_term_fqn(parent) if parent else GLOSSARY_NAME}.{name}"


def _related_terms_op(
    label: str, term: dict[str, Any], current: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Build the JSON Patch operation that sets the term's declared related terms.

    The server returns a link on both of its terms, and a link is declared on
    the later one, so links to terms declared after *term* belong to them and
    are kept as they are.  Links are compared by the related term's FQN, so a
    relation type a steward set on a declared link is kept too.

    :param label: What is being reconciled, for the log line.
    :param term: An entry of ``GLOSSARY_TERMS``.
    :param current: The term's relatedTerms as the server returned them, each a
        TermRelation (``relationType`` and a ``term`` entity reference).
    :returns: The operation, or None when the links already match.
    :raises ValueError: If a declared related term is missing or deleted.
    """
    index = GLOSSARY_TERMS.index(term)
    later = {_term_fqn(other["name"]) for other in GLOSSARY_TERMS[index + 1 :]}
    wanted = {_term_fqn(name) for name in term.get("relatedTerms", [])}
    owned = {
        link["term"]["fullyQualifiedName"]
        for link in current
        if link["term"]["fullyQualifiedName"] not in later
    }
    if owned == wanted:
        return None

    kept = [
        link for link in current if link["term"]["fullyQualifiedName"] in later | wanted
    ]
    added = []
    for fqn in sorted(wanted - owned):
        related = _get_by_name("glossaryTerms", fqn)
        if related is None:
            msg = f"{label}: related term {fqn} is missing or deleted in OpenMetadata"
            raise ValueError(msg)
        added.append(
            {
                "relationType": "relatedTo",
                "term": {"id": related["id"], "type": "glossaryTerm"},
            }
        )
    return {"op": "add", "path": "/relatedTerms", "value": kept + added}


def _patch_glossary_term(
    label: str, term: dict[str, Any], entity: dict[str, Any]
) -> None:
    """PATCH the term fields that differ from what ``GLOSSARY_TERMS`` declares.

    :param label: What is being reconciled, for the log line.
    :param term: An entry of ``GLOSSARY_TERMS``.
    :param entity: The term as the server returned it, with its relatedTerms.
    :raises requests.HTTPError: On any non-2xx response from the OM API.
    :raises ValueError: If a declared related term is missing or deleted.
    """
    wanted = {
        "description": term["description"],
        "synonyms": term.get("synonyms", []),
        "references": term.get("references", []),
    }
    # The server leaves an empty list out of the entity.
    current = {
        **entity,
        "synonyms": entity.get("synonyms") or [],
        "references": entity.get("references") or [],
    }
    ops = _add_ops(current, wanted, _GLOSSARY_TERM_PATCHABLE_FIELDS)
    if related_op := _related_terms_op(label, term, entity.get("relatedTerms") or []):
        ops.append(related_op)
    _apply_patch(label, f"{OM_SERVER_URL}/v1/glossaryTerms/{entity['id']}", ops)


def _reconcile_glossary_term(term: dict[str, Any]) -> None:
    """Create the glossary term if it is missing, else patch the fields that differ.

    A missing term is created only when its parent and glossary exist.

    :param term: An entry of ``GLOSSARY_TERMS``.
    :raises requests.HTTPError: On any non-2xx response from the OM API other
        than the server's refusal of a name the glossary already holds.
    :raises ValueError: If a declared related term is missing or deleted.
    """
    parent = term.get("parent")
    parent_fqn = _term_fqn(parent) if parent else GLOSSARY_NAME
    fqn = _term_fqn(term["name"])
    label = f"glossary term {fqn}"
    entity = _get_by_name("glossaryTerms", fqn, include="all", fields="relatedTerms")
    if entity is not None:
        if not _is_deleted(label, entity):
            _patch_glossary_term(label, term, entity)
        return

    # The server answers 404 for a parent or glossary that is missing or
    # soft-deleted before its duplicate-name check, e.g. for the children of a
    # term a steward moved, which are still declared under the old parent.
    ancestors = [("glossaries", GLOSSARY_NAME)]
    if parent:
        ancestors.append(("glossaryTerms", parent_fqn))
    for collection, ancestor_fqn in ancestors:
        ancestor = _get_by_name(collection, ancestor_fqn, include="all")
        if ancestor is None or ancestor.get("deleted"):
            print(  # noqa: T201
                f"[skip] {label}: {ancestor_fqn} is missing or deleted in "
                "OpenMetadata, left alone"
            )
            return

    body = {k: v for k, v in term.items() if k not in {"parent", "relatedTerms"}}
    if parent:
        body["parent"] = parent_fqn
    if related := term.get("relatedTerms"):
        body["relatedTerms"] = [_term_fqn(name) for name in related]
    try:
        _create("glossaryTerms", label, {"glossary": GLOSSARY_NAME, **body})
    except requests.HTTPError as exc:
        # Term names are unique across a glossary, case-insensitively.  A term
        # a steward moved under another parent is missing at this FQN but
        # still holds the name, and the server answers 400.
        if (
            exc.response is None
            or exc.response.status_code != requests.codes.bad_request
            or "already exists" not in exc.response.text
        ):
            raise
        print(f"[skip] {label}: name held elsewhere in the glossary — left alone")  # noqa: T201


def main() -> None:
    """Reconcile every tag, domain, custom property and glossary term.

    Exits non-zero if any step fails.
    """
    errors: list[str] = []

    steps: list[tuple[str, Callable[[], None]]] = [
        *((fqn, partial(_reconcile_tag, fqn, tag)) for fqn, tag in TAG_CONFIG.items()),
        *(
            (f"domain {name}", partial(_reconcile_domain, name, domain))
            for name, domain in DOMAIN_CONFIG.items()
        ),
        *(
            (
                f"{entity_type} custom property {name}",
                partial(_reconcile_custom_property, entity_type, name, prop),
            )
            for entity_type, props in CUSTOM_PROPERTY_CONFIG.items()
            for name, prop in props.items()
        ),
        (f"glossary {GLOSSARY_NAME}", _reconcile_glossary),
        *(
            (f"glossary term {term['name']}", partial(_reconcile_glossary_term, term))
            for term in GLOSSARY_TERMS
        ),
    ]
    for label, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001
            msg = f"[fail] {label}: {exc}"
            print(msg, file=sys.stderr)  # noqa: T201
            errors.append(msg)

    if errors:
        print(f"\n{len(errors)} error(s) — bootstrap incomplete.", file=sys.stderr)  # noqa: T201
        sys.exit(1)

    print("\nBootstrap complete.")  # noqa: T201


if __name__ == "__main__":
    main()
