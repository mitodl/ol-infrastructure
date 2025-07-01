import django
django.setup()
from django.core import management

from random import uniform
from time import sleep

while True:
    # Execute randomly once every 24-48 hours
    sleep(uniform(86400,172800))
    management.call_command("saml", ["--pull"])

exit(0)
