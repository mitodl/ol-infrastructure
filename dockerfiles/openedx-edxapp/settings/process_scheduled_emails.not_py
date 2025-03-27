import django
django.setup()
from django.core import management
from lms.djangoapps.instructor_task.management.commands import process_scheduled_instructor_tasks

from random import uniform
from time import sleep

while True:
    sleep(uniform(300,600))
    management.call_command(process_scheduled_instructor_tasks.Command(), [])

exit(0)
