from pyinfra.api import FactBase

from ol_configuration_management.lib.linux_helpers import normalize_cpu_arch


class DebianCpuArch(FactBase):
    command = "dpkg --print-architecture"

    def process(self, output):
        return normalize_cpu_arch(output)


class RedhatCpuArch(FactBase):
    command = "rpm --eval %{_host_cpu}"

    def process(self, output):
        return normalize_cpu_arch(output)
