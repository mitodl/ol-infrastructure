from pyinfra.api import FactBase


class HasSystemd(FactBase):
    command = "/bin/which systemd"

    def process(self, output):
        return bool(output)
