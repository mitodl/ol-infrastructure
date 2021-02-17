from ol_configuration_management.components.baseline.setup import (
    install_baseline_packages,
)
from ol_configuration_management.components.concourse.build import (
    configure_concourse,
    install_concourse,
    register_concourse_service,
)
from ol_configuration_management.components.concourse.models import (
    ConcourseBaseConfig,
    ConcourseWebConfig,
    ConcourseWorkerConfig,
)

concourse_config = ConcourseBaseConfig()
web_config = ConcourseWebConfig()
worker_config = ConcourseWorkerConfig()
install_baseline_packages()
install_concourse(concourse_config)
# register_concourse_service(web_config)
configure_concourse(web_config)
