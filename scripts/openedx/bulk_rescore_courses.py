"""
Standalone script to efficiently schedule bulk rescore tasks for all problems
in a list of courses that have student submissions.

Execute via Django shell:
    python manage.py shell < bulk_rescore_courses.py

Or interactively:
    python manage.py shell
    >>> exec(open('bulk_rescore_courses.py').read())
"""

import logging

from django.contrib.auth import get_user_model
from django.test.client import RequestFactory
from lms.djangoapps.courseware.models import StudentModule
from lms.djangoapps.instructor_task.api import submit_rescore_problem_for_all_students
from lms.djangoapps.instructor_task.api_helper import (
    AlreadyRunningError,
    QueueConnectionError,
)
from opaque_keys.edx.keys import CourseKey, UsageKey
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.exceptions import ItemNotFoundError

User = get_user_model()
log = logging.getLogger(__name__)


class BulkRescorer:
    """Helper class to manage bulk rescoring across courses."""

    def __init__(
        self,
        requester_username="admin",
        only_if_higher=False,
        dry_run=False,
        batch_size=50,
    ):
        """
        Initialize the bulk rescorer.

        Args:
            requester_username: Username of the user submitting rescore tasks
            only_if_higher: Only rescore if new score is higher
            dry_run: Print what would be rescored without submitting tasks
            batch_size: Number of problems per batch (for progress reporting)
        """
        self.requester = self._get_requester(requester_username)
        self.only_if_higher = only_if_higher
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.total_tasks = 0
        self.failed_problems = []

    def _get_requester(self, username):
        """Get the User object for the requester."""
        try:
            return User.objects.get(username=username)
        except User.DoesNotExist:
            msg = f"User '{username}' does not exist."
            raise ValueError(msg)

    def rescore_courses(self, course_ids):
        """
        Rescore all problems with submissions in the given courses.

        Args:
            course_ids: List of course ID strings (e.g., ['course-v1:MIT+18.02.3x+2024'])
        """
        if not course_ids:
            print("ERROR: No courses specified.")
            return

        print(f"\n{'=' * 70}")
        print(f"Starting bulk rescore for {len(course_ids)} course(s)")
        print(f"Dry run: {self.dry_run}")
        print(f"Only if higher: {self.only_if_higher}")
        print(f"{'=' * 70}\n")

        for course_id in course_ids:
            try:
                course_key = CourseKey.from_string(course_id)
            except Exception as e:
                print(f'ERROR: Invalid course ID "{course_id}": {e}')
                continue

            self._rescore_course(course_key)

        # Summary
        print(f"\n{'=' * 70}")
        print(f"Total rescore tasks scheduled: {self.total_tasks}")

        if self.failed_problems:
            print(f"\nFailed problems ({len(self.failed_problems)}):")
            for problem in self.failed_problems:
                print(f"  - {problem}")

        if self.dry_run:
            print("\n(Dry run - no tasks were actually submitted to Celery)")
        print(f"{'=' * 70}\n")

    def _rescore_course(self, course_key):
        """Rescore all problems with submissions in a single course."""
        print(f"\nProcessing course: {course_key}")

        # Verify course exists
        course = modulestore().get_course(course_key)
        if not course:
            print(f"  ERROR: Course {course_key} not found in modulestore")
            return

        # Get all unique problems with submissions in this course
        problems = self._get_problems_with_submissions(course_key)

        if not problems:
            print("  No problems with student submissions found.")
            return

        print(f"  Found {len(problems)} unique problems with submissions")

        # Process problems with batch progress reporting
        for i, usage_key in enumerate(problems, 1):
            if (i - 1) % self.batch_size == 0 and i > 1:
                print(f"  Processed {i - 1}/{len(problems)} problems...")

            try:
                self._submit_rescore_task(usage_key)
                self.total_tasks += 1
            except AlreadyRunningError:
                error_msg = f"{usage_key}: Rescore already running for this problem"
                print(f"  SKIPPED: {error_msg}")
                # Don't add to failed_problems - this is expected behavior
            except QueueConnectionError as e:
                error_msg = f"{usage_key}: Queue error - {e}"
                print(f"  ERROR: {error_msg}")
                self.failed_problems.append(error_msg)
            except Exception as e:
                error_msg = f"{usage_key}: {e}"
                print(f"  ERROR: {error_msg}")
                self.failed_problems.append(error_msg)

        print(f"  Successfully scheduled {len(problems)} rescore tasks for this course")

    def _get_problems_with_submissions(self, course_key):
        """
        Get all unique problems in a course that have student submissions.

        Returns a list of UsageKey objects for problems with StudentModule entries.
        """
        # Query for all StudentModule entries in this course
        student_modules = (
            StudentModule.objects.filter(course_id=course_key)
            .values("module_state_key")
            .distinct()
        )

        total_modules = student_modules.count()
        print(f"  Found {total_modules} unique module state keys")

        problems = []
        processed = 0
        skipped = 0
        errors = 0
        block_types = {}

        for module_entry in student_modules:
            processed += 1
            module_state_key = module_entry["module_state_key"]

            try:
                # Try to parse the usage key - handle both string and object formats
                if isinstance(module_state_key, str):
                    try:
                        usage_key = UsageKey.from_string(module_state_key)
                    except (AttributeError, ValueError) as parse_error:
                        # If from_string fails, try direct instantiation
                        log.debug(
                            f"UsageKey.from_string failed for {module_state_key}: {parse_error}, skipping"
                        )
                        errors += 1
                        continue
                else:
                    # Already a UsageKey-like object
                    usage_key = module_state_key

                # Verify it exists and is a problem block
                try:
                    block = modulestore().get_item(usage_key)
                    block_type = getattr(block, "category", "unknown")

                    # Track block types for debugging
                    if block_type not in block_types:
                        block_types[block_type] = {"count": 0}
                    block_types[block_type]["count"] += 1

                    if self._is_problem_block(block):
                        problems.append(usage_key)
                    else:
                        skipped += 1
                except ItemNotFoundError:
                    block_type = "NOT_FOUND"
                    if block_type not in block_types:
                        block_types[block_type] = {"count": 0}
                    block_types[block_type]["count"] += 1
                    skipped += 1
                except Exception as e:
                    # Log the actual error for debugging
                    error_type = type(e).__name__
                    if error_type not in block_types:
                        block_types[f"ERROR_{error_type}"] = {"count": 0}
                    block_types[f"ERROR_{error_type}"]["count"] += 1
                    errors += 1
                    if errors <= 3:  # Print first 3 errors
                        log.warning(
                            f"Error loading block {module_state_key}: {error_type}: {e}"
                        )

            except Exception as e:
                error_type = type(e).__name__
                log.debug(
                    f"Error processing block {module_state_key}: {error_type}: {e}"
                )
                errors += 1

        # Print debugging info if no problems found
        if not problems and total_modules > 0:
            print(f"\n  DEBUG: Analysis of {total_modules} blocks (no problems found):")
            for block_type in sorted(block_types.keys()):
                count = block_types[block_type]["count"]
                print(f"    - {block_type}: {count} blocks")
            if skipped > 0:
                print(f"  Skipped {skipped} non-problem blocks")
            if errors > 0:
                print(f"  ⚠️  Encountered {errors} ERRORS while processing blocks!")
                print("  Check logs for error details (see above WARNING messages)")
            print("\n  Possible issues:")
            print("  1. Block loading errors (check WARNING messages above)")
            print("  2. No problem blocks in this course")
            print("  3. Course has been deleted/moved from modulestore\n")
        elif skipped > 0:
            print(f"  (Skipped {skipped} non-problem blocks)")

        return sorted(problems, key=str)

    def _is_problem_block(self, block):
        """Check if a block is a problem that supports rescoring."""
        # Get block type - try multiple attribute names
        block_type = (
            getattr(block, "category", None)
            or getattr(block, "type", None)
            or getattr(block, "block_type", None)
        )

        # Common problem block types that support rescoring
        problem_types = [
            "problem",
            "capa",
            "capa_problem",
            "combinedopenended",
            "ubcproblem",
        ]

        # If block_type matches known problem types, it's a problem
        if block_type in problem_types:
            return True

        # If type is unknown but contains 'problem', likely a problem block
        return bool(block_type and "problem" in block_type.lower())

    def _submit_rescore_task(self, usage_key):
        """Submit a rescore task for a problem."""
        if self.dry_run:
            print(f"  [DRY RUN] Would rescore: {usage_key}")
            return

        # Create a fake request object with the requester as user
        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.requester
        request.META["REMOTE_ADDR"] = "127.0.0.1"
        request.META["SERVER_NAME"] = "localhost"
        request.META["HTTP_USER_AGENT"] = "bulk_rescore_script"

        # Submit the rescore task
        task = submit_rescore_problem_for_all_students(
            request, usage_key, only_if_higher=self.only_if_higher
        )
        print(f"  Scheduled rescore task {task.task_id} for {usage_key}")


def main():
    # List of course IDs to rescore
    COURSE_IDS = [
        # "course-v1:MITxT+10.50.CH01x+1T2025",
        # "course-v1:MITxT+10.50.CH02x+1T2025",
        # "course-v1:MITxT+10.50.CH03x+1T2025",
        # "course-v1:MITxT+10.50.CH04x+1T2025",
        # "course-v1:MITxT+10.50.CH05x+1T2025",
        # "course-v1:MITxT+10.50.CH06x+1T2025",
        # "course-v1:MITxT+10.50.CH07x+1T2025",
        # "course-v1:MITxT+10.50.CH08x+1T2025",
        # "course-v1:MITxT+10.50.CH09x+1T2025",
        # "course-v1:MITxT+10.MBCx+1T2025",
        # "course-v1:MITxT+11.024x+3T2025",
        # "course-v1:MITxT+11.092x+2T2025",
        # "course-v1:MITxT+11.405x+2T2025",
        # "course-v1:MITxT+11.S198x+2T2025",
        # "course-v1:MITxT+14.003x+3T2025",
        # "course-v1:MITxT+14.01x+2T2025",
        # "course-v1:MITxT+14.100x+3T2025",
        # "course-v1:MITxT+14.310x+3T2025",
        "course-v1:MITxT+14.73x+3T2025",
        # "course-v1:MITxT+14.750x+3T2025",
        # "course-v1:MITxT+15.356.1x+3T2025",
        # "course-v1:MITxT+15.356.2x+3T2025",
        # "course-v1:MITxT+15.671.1x+3T2025",
        # "course-v1:MITxT+17.TAEx+3T2025",
        # "course-v1:MITxT+18.01.1x+3T2025",
        # "course-v1:MITxT+18.02.1x+3T2025",
        # "course-v1:MITxT+18.03.1x+3T2025",
        # "course-v1:MITxT+18.03.Lx+1T2025",
        # "course-v1:MITxT+2.S990x+3T2025",
        # "course-v1:MITxT+21.01.1x+2T2025",
        # "course-v1:MITxT+21L.010x+1T2025",
        # "course-v1:MITxT+24.02x+3T2025",
        # "course-v1:MITxT+24.09x+3T2025",
        # "course-v1:MITxT+3.012Sx+3T2025",
        # "course-v1:MITxT+3.012Tx+3T2025",
        # "course-v1:MITxT+3.022.1x+3T2024",
        # "course-v1:MITxT+3.022.2x+3T2024",
        # "course-v1:MITxT+3.022.3x+3T2024",
        # "course-v1:MITxT+3.022.4x+3T2024",
        # "course-v1:MITxT+3.024x+3T2025",
        # "course-v1:MITxT+3.032.1x+3T2025",
        # "course-v1:MITxT+3.032.2x+3T2025",
        # "course-v1:MITxT+3.032.3x+3T2025",
        # "course-v1:MITxT+3.034.1x+3T2025",
        # "course-v1:MITxT+3.054x+3T2025",
        # "course-v1:MITxT+3.086x+3T2025",
        # "course-v1:MITxT+5.601x+3T2025",
        # "course-v1:MITxT+6.UWTDx+3T2025",
        # "course-v1:MITxT+7.03.1x+2T2025",
        # "course-v1:MITxT+7.03.2x+2T2025",
        # "course-v1:MITxT+7.03.3x+2T2025",
        # "course-v1:MITxT+7.05x+2T2025",
        # "course-v1:MITxT+7.06.1x+2T2025",
        # "course-v1:MITxT+7.06.2x+2T2025",
        # "course-v1:MITxT+7.06.3x+2T2025",
        # "course-v1:MITxT+7.28.1x+2T2025",
        # "course-v1:MITxT+7.28.2x+2T2025",
        # "course-v1:MITxT+7.28.3x+2T2025",
        # "course-v1:MITxT+7.QBWx+2T2025",
        # "course-v1:MITxT+8.01.1x+3T2025",
        # "course-v1:MITxT+8.01.2x+3T2025",
        # "course-v1:MITxT+8.01.3x+3T2025",
        # "course-v1:MITxT+8.014x+2T2025",
        # "course-v1:MITxT+8.03x+3T2025",
        # "course-v1:MITxT+8.EFTx+3T2022",
        # "course-v1:MITxT+Bootcamp0+3T2025",
        # "course-v1:MITxT+Bootcamp1+3T2025",
        # "course-v1:MITxT+Bootcamp2+3T2025",
        # "course-v1:MITxT+Bootcamp3+3T2025",
        # "course-v1:MITxT+JPAL101x+3T2025",
        # "course-v1:MITxT+JPAL102x+3T2025",
        # "course-v1:MITxT+LaunchX+3T2025",
        # "course-v1:MITxT+STL.162x+2T2025",
        # "course-v1:MITxT+VJx+2T2025",
        # "course-v1:MITxT+VPx+2T2025",
        # 'course-v1:MITxT+14.73x+3T2025',
    ]

    # Username of the user requesting the rescore (must exist in database)
    REQUESTER_USERNAME = "tobias-macey"

    # Only rescore if the new score is higher than the existing score
    ONLY_IF_HIGHER = False

    # Set to True to preview what would be rescored without actually submitting tasks
    DRY_RUN = False

    # Batch size for progress reporting (doesn't affect task submission)
    BATCH_SIZE = 150

    # Validate inputs
    if not COURSE_IDS:
        print("ERROR: COURSE_IDS is empty. Please specify at least one course ID.")
        return

    # Create rescorer and execute
    try:
        rescorer = BulkRescorer(
            requester_username=REQUESTER_USERNAME,
            only_if_higher=ONLY_IF_HIGHER,
            dry_run=DRY_RUN,
            batch_size=BATCH_SIZE,
        )
        rescorer.rescore_courses(COURSE_IDS)
    except ValueError as e:
        print(f"ERROR: {e}")


# Execute when script is run
if __name__ == "__main__":
    main()
else:
    # Allow manual execution from Django shell
    print("BulkRescorer class loaded. Use BulkRescorer() to create an instance.")
    print("Example:")
    print("  rescorer = BulkRescorer(requester_username='admin', dry_run=True)")
    print("  rescorer.rescore_courses(['course-v1:MIT+18.02.3x+2024'])")
    main()
