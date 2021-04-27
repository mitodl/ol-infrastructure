from pyinfra import host

from bilder.components.baseline.setup import install_baseline_packages
from bilder.components.concourse.models import (
    ConcourseBaseConfig,
    ConcourseWebConfig,
    ConcourseWorkerConfig,
)
from bilder.components.concourse.steps import (
    configure_concourse,
    install_concourse,
    register_concourse_service,
)
from bilder.facts import has_systemd  # noqa: F401

concourse_config = ConcourseBaseConfig()
web_config = ConcourseWebConfig()
worker_config = ConcourseWorkerConfig()
install_baseline_packages()
install_changed = install_concourse(concourse_config)
config_changed = configure_concourse(web_config)
if host.fact.has_systemd:
    register_concourse_service(web_config, restart=install_changed or config_changed)
