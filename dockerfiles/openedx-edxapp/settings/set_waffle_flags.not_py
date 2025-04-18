import django
django.setup()
from django.core import management

import yaml
import sys

with open(sys.argv[1]) as yamlfile:
    config = yaml.safe_load(yamlfile)

# Expected yaml structure
# ---
# waffles:
# - ["waffle.flag.name", "argument1", "argument2", "argument3"]
for argument_set in config["waffles"]:
    management.call_command("waffle_flag", argument_set)

exit(0)
