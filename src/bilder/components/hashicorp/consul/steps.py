import tempfile

from pyinfra.api import deploy
from pyinfra.operations import files, iptables, systemd

from bilder.lib.magic_numbers import CONSUL_DNS_PORT, DEFAULT_DNS_PORT


@deploy("Set up DNS proxy")
def proxy_consul_dns(state=None, host=None):
    with tempfile.NamedTemporaryFile(delete=False, mode="w") as source_file:
        source_file.write("[Resolve]\nDNS=127.0.0.1\nDomains=~consul")
        files.put(
            name="Configure systemd-resolved to resolve .consul domains locally",
            dest="/etc/systemd/resolved.conf.d/consul.conf",
            src=source_file.name,
            create_remote_dir=True,
            state=state,
            host=host,
        )
    systemd.service(
        name="Enable systemd-resolved",
        service="systemd-resolved",
        enabled=True,
        running=True,
        state=state,
        host=host,
    )
    for protocol in ("tcp", "udp"):
        iptables.rule(
            name=f"Route localhost {protocol} DNS queries to Consul port",
            present=True,
            table="nat",
            protocol=protocol,
            chain="OUTPUT",
            append=True,
            jump="REDIRECT",
            destination="localhost",
            destination_port=DEFAULT_DNS_PORT,
            to_ports=str(CONSUL_DNS_PORT),
            state=state,
            host=host,
        )
