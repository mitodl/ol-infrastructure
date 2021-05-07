import tempfile
from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import apt, files, systemd


@deploy("Set up DNS proxy")
def proxy_consul_dns(state=None, host=None):
    apt.packages(
        name="Install Unbound for DNS proxying",
        packages=["unbound"],
        present=True,
        update=True,
        state=state,
        host=host,
    )
    with tempfile.NamedTemporaryFile(delete=False, mode="w") as source_file:
        source_file.write(
            'make_resolv_conf\necho "nameserver 127.0.0.1\\n$(cat /etc/resolv.conf)" '
            "> /etc/resolv.conf"
        )
        files.put(
            name="Configure dhclient to use local DNS",
            dest="/etc/dhcp/dhclient-enter-hooks.d/consul",
            src=source_file.name,
            create_remote_dir=True,
            mode="0755",
            state=state,
            host=host,
        )
    files.put(
        name="Configure Unbound to resolve .consul domains locally",
        dest="/etc/unbound/unbound.conf.d/consul.conf",
        src=Path(__file__).parent.joinpath("files", "unbound_config.conf"),
        create_remote_dir=True,
        state=state,
        host=host,
    )
    systemd.service(
        name="Enable Unbound DNS proxy",
        service="unbound",
        enabled=True,
        running=True,
        state=state,
        host=host,
    )
