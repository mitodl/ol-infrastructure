import tempfile
from pathlib import Path

from pyinfra.api import deploy
from pyinfra.operations import apt, files, systemd

from bilder.facts import has_systemd  # noqa: F401


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
    with tempfile.NamedTemporaryFile(delete=False, mode="w") as dhclient_config:
        dhclient_config.write(
            'make_resolv_conf\necho "nameserver 127.0.0.1\\n$(cat /etc/resolv.conf)" '
            "> /etc/resolv.conf"
        )
        files.put(
            name="Configure dhclient to use local DNS",
            dest="/etc/dhcp/dhclient-enter-hooks.d/consul",
            src=dhclient_config.name,
            create_remote_dir=True,
            mode="0755",
            state=state,
            host=host,
        )
    if host.fact.has_systemd and host.fact.systemd_enabled["systemd-resolved.service"]:
        with tempfile.NamedTemporaryFile(delete=False, mode="w") as resolved_conf:
            resolved_conf.write("[Resolve]\nDNS=127.0.0.1\nDomains=~consul")
            consul_resolved_config = files.put(
                name="Configure systemd-resolved to resolve .consul domains locally",
                dest="/etc/systemd/resolved.conf.d/consul.conf",
                src=resolved_conf.name,
                create_remote_dir=True,
                state=state,
                host=host,
            )
        systemd.service(
            name="Enable systemd-resolved",
            service="systemd-resolved",
            enabled=True,
            running=True,
            restarted=consul_resolved_config.changed,
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
