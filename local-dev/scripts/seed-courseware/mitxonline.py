# Not an importable module: this file is piped into the mitxonline pod's
# `manage.py shell`, which execs stdin. It only ever runs inside that app's
# Django process, against that app's models.
"""
Create the local-dev test courseware inside mitxonline.

Piped into `python manage.py shell` by local-dev/scripts/seed-courseware.sh,
prefixed by that script with the seed file's contents as ``SEED_JSON``.

Kept as a file rather than an inline heredoc (the idiom the other tutor seed
scripts use) purely because of its size.

Everything here is ``update_or_create`` or explicitly guarded, so re-running the
seed is a no-op rather than an error. That is the reason this exists at all
instead of calling mitxonline's own `create_courseware`: that command calls
`exit(-1)` the moment a readable_id already exists.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import reversion
from cms.api import (
    create_default_certificate_page,
    create_default_courseware_page,
    get_optional_placeholder_values_for_courseware_type,
)
from courses.models import Course, CourseRun, Department, EnrollmentMode, Program
from django.contrib.contenttypes.models import ContentType
from ecommerce.constants import (
    DISCOUNT_TYPE_PERCENT_OFF,
    PAYMENT_TYPE_STAFF,
    REDEMPTION_TYPE_UNLIMITED,
)
from ecommerce.models import Discount, Product
from flexiblepricing.api import create_default_flexible_pricing_page
from flexiblepricing.models import CurrencyExchangeRate
from flexiblepricing.utils import ensure_flexprice_form_fields
from openedx.constants import EDX_ENROLLMENT_AUDIT_MODE, EDX_ENROLLMENT_VERIFIED_MODE

# Injected by seed-courseware.sh, which prepends the seed file to this one as a
# string literal. Annotation only: it declares the contract for the linters
# without creating a name that would shadow the real value at run time.
SEED_JSON: str

SEED = json.loads(SEED_JSON)  # noqa: F821  (ruff does not count the annotation)

NOW = datetime.now(tz=UTC)
# Wide enough that a seeded run is enrollable today and stays that way for a
# development cycle, and started in the past so it shows as in-progress.
RUN_DATES = {
    "start_date": NOW - timedelta(days=7),
    "end_date": NOW + timedelta(days=180),
    "enrollment_start": NOW - timedelta(days=14),
    "enrollment_end": NOW + timedelta(days=180),
}

# Enough to let a financial-assistance request be priced without reaching
# openexchangerates.org. `update_exchange_rates` is the real source but needs
# OPEN_EXCHANGE_RATES_APP_ID and internet access; flexiblepricing.api looks a
# rate up for the applicant's currency and errors without one.
EXCHANGE_RATES = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "INR": 83.0}


def ok(message):
    """Report one completed step, matching the seed script's log prefix."""
    print(f"  ✓ {message}")


def skip(message):
    """Report one step that was already done on an earlier run."""
    print(f"  · {message}")


def get_departments(names):
    """Return Department rows for ``names``, creating any that are missing."""
    departments = []
    for name in names:
        department, _ = Department.objects.get_or_create(name=name)
        departments.append(department)
    return departments


def ensure_page(courseware):
    """
    Create and publish the Wagtail page for a Course or Program.

    Uses mitxonline's own CMS API so the page carries the same placeholder
    values the app's `create_courseware --create-page` would produce, which is
    what makes it renderable rather than merely present.

    Deliberately does not create a signatory page: mitxonline's
    `create_default_signatory_page(..., include_placeholder_image=True)` fetches
    a placeholder image over the network, and this seed has to work offline.
    """
    existing = (
        courseware.course_page
        if isinstance(courseware, Course)
        else courseware.program_page
    )
    if existing is not None:
        skip(f"CMS page for {courseware.readable_id} already exists")
        return existing

    page = create_default_courseware_page(
        courseware,
        live=True,
        include_in_learn_catalog=True,
        optional_kwargs=get_optional_placeholder_values_for_courseware_type(courseware),
    )
    page.save_revision().publish()
    ok(f"CMS page {page.slug} for {courseware.readable_id} (published)")

    # course_page / program_page are cached_propertys, and the check above just
    # evaluated one of them as None. create_default_certificate_page reads the
    # same property to find the parent, so without dropping the cache it gets
    # None and fails on `None.add_child`.
    courseware.__dict__.pop("course_page", None)
    courseware.__dict__.pop("program_page", None)

    certificate_page = create_default_certificate_page(courseware)
    certificate_page.save_revision().publish()
    ok(f"certificate page for {courseware.readable_id}")

    return page


def ensure_product(course_run, price):
    """
    Make ``course_run`` purchasable at ``price``.

    The revision is not optional: checkout reads `Line.product_version`, a
    django-reversion Version, so a Product created outside a revision block
    breaks the basket rather than the seed.
    """
    content_type = ContentType.objects.get(app_label="courses", model="courserun")
    with reversion.create_revision():
        product, created = Product.objects.update_or_create(
            content_type=content_type,
            object_id=course_run.id,
            is_active=True,
            defaults={
                "price": Decimal(price),
                "description": course_run.courseware_id,
            },
        )
    ok(
        "product for {} at {} ({})".format(
            course_run.courseware_id, product.price, "created" if created else "updated"
        )
    )
    return product


def ensure_discount(config):
    """
    Create the fully-discounting code that makes local checkout completable.

    local-dev has no working payment gateway -- the CyberSource keys in
    local-dev/apps/mitxonline/secrets.yaml are placeholders -- but mitxonline
    short-circuits a zero-total basket straight into `fulfill_completed_order`,
    so a 100%-off code is the whole of a working purchase flow here.

    `Discount.discount_code` is not unique in the model, so this looks the code
    up rather than using `update_or_create` on it.
    """
    code = config["code"]
    defaults = {
        "amount": Decimal(str(config["amount"])),
        "discount_type": config.get("discount_type", DISCOUNT_TYPE_PERCENT_OFF),
        "redemption_type": REDEMPTION_TYPE_UNLIMITED,
        "payment_type": PAYMENT_TYPE_STAFF,
        # No expiration_date: Discount.save() rejects one in the past, which a
        # fixed date in a seed file would eventually become.
        "expiration_date": None,
        "activation_date": None,
    }

    discount = Discount.objects.filter(discount_code=code).first()
    if discount is None:
        discount = Discount.objects.create(discount_code=code, **defaults)
        ok(f"discount {code}: {discount.amount} {discount.discount_type}")
    else:
        for field, value in defaults.items():
            setattr(discount, field, value)
        discount.save()
        skip(f"discount {code} already exists (refreshed)")
    return discount


def ensure_finaid_form(courseware):
    """
    Create and publish the financial-assistance request form.

    Not `manage.py create_finaid_form`: that command is not re-runnable. On a
    second run the slug collides, and while it does catch the ValidationError,
    it then hands the exception object to `self.style.ERROR`, which wants a
    string -- so the "handled" path dies with an AttributeError. Guarding on the
    existing form here avoids the whole thing.
    """
    from cms.models import FlexiblePricingRequestForm  # noqa: PLC0415

    parent = (
        courseware.course_page
        if isinstance(courseware, Course)
        else courseware.program_page
    )
    if parent is None:
        skip(f"no CMS page for {courseware.readable_id}, skipping its finaid form")
        return None

    existing = FlexiblePricingRequestForm.objects.child_of(parent).first()
    if existing is not None:
        skip(f"finaid form for {courseware.readable_id} already exists")
        return existing

    # forceCourse for a Course: without it the form is attached to the course's
    # *program* page instead, which is not where the guard above looks, so every
    # run would try to create it again and collide on the slug.
    form = create_default_flexible_pricing_page(
        courseware,
        isinstance(courseware, Course),
        live=True,
    )
    # The country list and the rest of the form fields are added by the signal
    # receiver on publish; calling this directly is what the app's own command
    # does for a form created live.
    ensure_flexprice_form_fields(form)
    form.save_revision().publish()
    ok(f"finaid form {form.slug} for {courseware.readable_id} (published)")
    return form


def ensure_exchange_rates():
    """Seed the currency rates a financial-assistance request needs."""
    for currency_code, rate in EXCHANGE_RATES.items():
        CurrencyExchangeRate.objects.update_or_create(
            currency_code=currency_code,
            defaults={"exchange_rate": rate},
        )
    ok(f"exchange rates: {', '.join(sorted(EXCHANGE_RATES))}")


def ensure_course(config):
    """Create a Course, its single run, its CMS page and its Product."""
    readable_id = config["readable_id"]
    course, created = Course.objects.update_or_create(
        readable_id=readable_id,
        defaults={"title": config["title"], "live": True},
    )
    course.departments.set(get_departments(config.get("departments", [])))
    ok(f"course {readable_id} ({'created' if created else 'updated'})")

    run_tag = config["run_tag"]
    course_run, run_created = CourseRun.all_objects.update_or_create(
        courseware_id=f"{readable_id}+{run_tag}",
        defaults={
            "course": course,
            "title": config["title"],
            "run_tag": run_tag,
            "live": True,
            # A source run is invisible to CourseRun.objects
            # (UsableCourseRunManager), which is what makes products impossible
            # on one. Seeded runs are the real, enrollable article.
            "is_source_run": False,
            "is_self_paced": True,
            **RUN_DATES,
        },
    )
    ok(f"run {course_run.courseware_id} ({'created' if run_created else 'updated'})")

    # Both modes always: audit so the run is enrollable for free, verified so it
    # can be upgraded. The rows themselves ship in a mitxonline data migration.
    modes = [EDX_ENROLLMENT_AUDIT_MODE]
    if config.get("price"):
        modes.append(EDX_ENROLLMENT_VERIFIED_MODE)
    course_run.enrollment_modes.set(
        EnrollmentMode.objects.filter(mode_slug__in=modes),
    )

    ensure_page(course)

    if config.get("price"):
        ensure_product(course_run, config["price"])
    else:
        skip(f"{course_run.courseware_id} is audit-only, no product")

    if config.get("finaid"):
        ensure_finaid_form(course)

    return course


def ensure_program(config, courses_by_id):
    """Create a Program, its CMS page, and its requirements tree."""
    from courses.models import (  # noqa: PLC0415
        ProgramRequirement,
        ProgramRequirementNodeType,
    )

    readable_id = config["readable_id"]
    program, created = Program.objects.update_or_create(
        readable_id=readable_id,
        defaults={"title": config["title"], "live": True},
    )
    program.departments.set(get_departments(config.get("departments", [])))
    ok(f"program {readable_id} ({'created' if created else 'updated'})")

    # Program.save() creates the requirements root, so the tree is ready to
    # accept children by now. add_requirement/add_elective are NOT idempotent
    # for course nodes -- their de-dup filter looks at the root's children,
    # which are the operator nodes, never the course nodes two levels down -- so
    # the guard has to happen here or a re-run stacks duplicates.
    def already_attached(course):
        return ProgramRequirement.objects.filter(
            program=program,
            course=course,
            node_type=ProgramRequirementNodeType.COURSE,
        ).exists()

    for course_id in config.get("required", []):
        course = courses_by_id[course_id]
        if already_attached(course):
            skip(f"{course_id} already a requirement of {readable_id}")
        else:
            program.add_requirement(course)
            ok(f"{course_id} required by {readable_id}")

    for course_id in config.get("electives", []):
        course = courses_by_id[course_id]
        if already_attached(course):
            skip(f"{course_id} already an elective of {readable_id}")
        else:
            program.add_elective(course)
            ok(f"{course_id} elective of {readable_id}")

    # add_elective creates the elective operator node with operator_value=1;
    # set the configured minimum on it once the node exists.
    min_electives = config.get("min_electives")
    if min_electives and config.get("electives"):
        elective_node = (
            program.get_requirements_root()
            .get_children()
            .filter(operator=ProgramRequirement.Operator.MIN_NUMBER_OF)
            .first()
        )
        if elective_node and str(elective_node.operator_value) != str(min_electives):
            elective_node.operator_value = min_electives
            elective_node.save()
            ok(f"{readable_id} requires {min_electives} elective(s)")

    ensure_page(program)

    if config.get("finaid"):
        ensure_finaid_form(program)

    return program


print("▶ Seeding mitxonline courseware")

courses_by_id = {
    config["readable_id"]: ensure_course(config) for config in SEED["courses"]
}

for config in SEED.get("programs", []):
    ensure_program(config, courses_by_id)

ensure_discount(SEED["discount"])
ensure_exchange_rates()

print(
    "▶ mitxonline: {} course(s), {} program(s)".format(
        len(courses_by_id), len(SEED.get("programs", []))
    )
)
