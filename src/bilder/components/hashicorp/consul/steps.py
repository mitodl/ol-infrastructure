import tempfile
from pathlib import Path

from pyinfra import host
from pyinfra.api import deploy
from pyinfra.facts.systemd import SystemdStatus
from pyinfra.operations import apt, files, systemd

from bilder.facts.has_systemd import HasSystemd


@deploy("Set up DNS proxy")
def proxy_consul_dns():
    files.line(
        name="Configure dhclient to always put 127.0.0.1 as the first DNS server.",
        path="/etc/dhcp/dhclient.conf",
        line="#prepend domain-name-servers 127.0.0.1;",
        replace="prepend domain-name-servers 127.0.0.1;",
        present=True,
    )
    # Allow hosts that default to using systemd-resolved to properly resolve Consul
    # domains
    if host.get_fact(HasSystemd) and host.get_fact(SystemdStatus).get(
        "systemd-resolved.service"
    ):
        with tempfile.NamedTemporaryFile(delete=False, mode="w") as resolved_conf:
            resolved_conf.write(
                "[Resolve]\nDNS=127.0.0.1:8600\nDNSSEC=false\nDomains=~consul"
            )
            consul_resolved_config = files.put(
                name="Configure systemd-resolved to resolve .consul domains locally",
                dest="/etc/systemd/resolved.conf.d/consul.conf",
                src=resolved_conf.name,
                create_remote_dir=True,
            )
        systemd.service(
            name="Enable systemd-resolved",
            service="systemd-resolved",
            enabled=True,
            running=True,
            restarted=consul_resolved_config.changed,
        )
    else:
        apt.packages(
            name="Install Unbound for DNS proxying",
            packages=["unbound"],
            present=True,
            update=True,
        )
        files.put(
            name="Configure Unbound to resolve .consul domains locally",
            dest="/etc/unbound/unbound.conf.d/consul.conf",
            src=str(
                Path(__file__).resolve().parent.joinpath("files", "unbound_config.conf")
            ),
            create_remote_dir=True,
        )
        systemd.service(
            name="Enable Unbound DNS proxy",
            service="unbound",
            enabled=True,
            running=True,
        )
