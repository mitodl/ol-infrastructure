# Not an importable module: this file is piped into the tutor LMS container's
# `./manage.py lms shell`, which execs stdin.
"""
Create the enrollment modes for the seeded Open edX course runs.

Piped into `./manage.py lms shell` by local-dev/scripts/seed-courseware.sh,
prefixed by that script with the seed file's contents as ``SEED_JSON``.

There is no management command for this on either side. Studio's
`generate_courses` (the previous phase) creates the course itself but no modes,
and a course with no `verified` mode rejects mitxonline's upgrade enrollment
with a 400 -- "Specified course mode 'verified' unavailable for course".
"""

import json

# The fully-qualified path, not the bare `course_modes.models` that
# edx-platform's own code uses: that shortcut only resolves inside a process
# with common/djangoapps on sys.path, which `manage.py shell` is not.
from common.djangoapps.course_modes.models import CourseMode
from opaque_keys.edx.keys import CourseKey
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

# Injected by seed-courseware.sh, which prepends the seed file to this one as a
# string literal. Annotation only: it declares the contract for the linters
# without creating a name that would shadow the real value at run time.
SEED_JSON: str

SEED = json.loads(SEED_JSON)  # noqa: F821  (ruff does not count the annotation)

print("▶ Seeding Open edX course modes")

# This phase is also where a failed phase 1 is caught: `cms generate_courses`
# exits 0 whether or not it created anything, so the courses existing here is
# the real evidence that it worked.
missing = []

for course in SEED["courses"]:
    course_key = CourseKey.from_string(f"{course['readable_id']}+{course['run_tag']}")

    # CourseMode.course is a FK to CourseOverview with db_constraint=False, so
    # the rows would insert without this -- but the LMS resolves a mode through
    # the overview, so materialise it rather than leaving a dangling id.
    try:
        CourseOverview.get_from_id(course_key)
    except CourseOverview.DoesNotExist:
        print(f"  ✗ {course_key} does not exist in Open edX")
        missing.append(str(course_key))
        continue

    modes = [("audit", "Audit", 0)]
    if course.get("price"):
        # min_price is an integer and `verified` is rejected below the minimum
        # in COURSE_ENROLLMENT_MODES, so the seed price is floored, not rounded
        # down to zero.
        min_price = max(1, int(float(course["price"])))
        modes.append(("verified", "Verified Certificate", min_price))

    for mode_slug, display_name, min_price in modes:
        # get_or_create + an explicit save, not update_or_create: CourseMode
        # overrides save() with a narrower signature that has no update_fields,
        # so update_or_create's update branch dies with a TypeError -- on the
        # *second* run, once the rows it created the first time need updating.
        mode, created = CourseMode.objects.get_or_create(
            course_id=course_key,
            mode_slug=mode_slug,
            currency="usd",
            defaults={
                "mode_display_name": display_name,
                "min_price": min_price,
            },
        )
        if not created:
            mode.mode_display_name = display_name
            mode.min_price = min_price
            mode.save()
        print(
            "  {} {} mode for {} (${})".format(
                "✓" if created else "·", mode_slug, course_key, min_price
            )
        )

if missing:
    message = (
        f"▶ {len(missing)} of {len(SEED['courses'])} run(s) are not in Open edX: "
        f"{', '.join(missing)}\n"
        "  Studio did not create them. Check the openedx-tutor-config and "
        "openedx-tutor resources,\n  then re-run the seed."
    )
    raise SystemExit(message)

print(f"▶ Open edX: modes set for {len(SEED['courses'])} run(s)")
