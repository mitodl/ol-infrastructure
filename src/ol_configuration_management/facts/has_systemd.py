from pyinfra.api import FactBase


class HasSystemd(FactBase):
    command = "/bin/which systemd || echo 'false'"

    def process(self, output):
        return "false" not in ",".join(output)
