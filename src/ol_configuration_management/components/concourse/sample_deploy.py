from gevent import monkey

monkey.patch_all()
from collections import defaultdict
from datetime import datetime
from typing import Dict

# from pyinfra import host
from pyinfra.api import BaseStateCallback, Config, Inventory, State
from pyinfra.api.connect import connect_all
from pyinfra.api.connectors.vagrant import make_names_data
from pyinfra.api.deploy import add_deploy
from pyinfra.api.facts import get_facts
from pyinfra.api.operations import run_ops
from pyinfra.operations import server

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
from ol_configuration_management.facts import has_systemd  # noqa: F401

concourse_config = ConcourseBaseConfig()
web_config = ConcourseWebConfig()
worker_config = ConcourseWorkerConfig()
# install_baseline_packages()
# install_changed = install_concourse(concourse_config)
# config_changed = configure_concourse(web_config)
# if host.fact.has_systemd:
#     register_concourse_service(web_config, restart=install_changed or config_changed)


if __name__ == "__main__":

    class StateCallback(BaseStateCallback):
        def host_connect(self, state, host):
            print(f"{datetime.now().isoformat()} Host connected: {host}")

        def operation_start(self, state, op_hash):
            op_name = state.op_meta[op_hash]["names"]
            print(f"{datetime.now().isoformat()} Start operation: {op_name}")

        def operation_end(self, state, op_hash):
            op_name = state.op_meta[op_hash]["names"]
            print(f"{datetime.now().isoformat()} End operation: {op_name}")

    hosts = []
    groups: Dict = defaultdict(lambda: ([], {}))

    for host_name, host_data, group_names in make_names_data():
        hosts.append((host_name, host_data))
        for group_name in group_names:
            if host_name not in groups[group_name][0]:
                groups[group_name][0].append(host_name)

    inventory = Inventory((hosts, {}))
    state = State(inventory, Config())
    state.add_callback_handler(StateCallback())
    connect_all(state)
    add_deploy(state, install_baseline_packages)
    add_deploy(state, install_concourse, concourse_config)
    add_deploy(state, configure_concourse, web_config)
    if get_facts(state, "has_systemd"):
        add_deploy(state, register_concourse_service, web_config)
    run_ops(state)
