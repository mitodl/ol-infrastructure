from pathlib import Path
from typing import Any

from pyinfra.operations import files

from bilder.components.hashicorp.consul_template.models import ConsulTemplateTemplate

CONSUL_TEMPLATE_DIRECTORY = "/etc/consul-template"


def place_jinja_template_file(  # noqa: PLR0913
    name: str,
    repo_path: Path,
    destination_path: Path,
    context: dict[str, Any],
    watched_files: list[Path],  # noqa: ARG001
    mode: str = "0644",  # noqa: ARG001
    user: str = "root",
    group: str = "root",
):
    """Interpolate and place a file on the destination at AMI bake time.
    Predicate function that side effects multiple arguments and has no return.

    :param name: The name the file will ultimately have on the destination system.
    :type name: str

    :param repo_path: The path within this code repository containing the
        source template.
    :type repo_path: Path

    :param destination_path: The path on the destination system for the file to be
        placed at. This does NOT include the filename, just the directory.
    :type destination_path: Path

    :param context: A dictionary containing the values to interpolate into the tempalte.
    :type context: dict

    :param watched_files: A list of files that will be watched on the system and used to
        trigger service restarts when changed. This function will append the combined
        destination_path + name to the list.
    :type watched_files: list[Path]

    :param mode: The permissions specifier for the file on the destination system.
        Numerical form.
    :type mode: str

    :param user: The file user of the rendered template on the system.
    :type user: str

    :param group: The group of the rendered template on the system.
    :type group: str

    :rtype: Path
    :returns: A Path object representing the path of the ultimate file
        (not the template file).
    """
    dest = destination_path.joinpath(name)
    files.template(
        name=f"Place and interpolate {name} jinja template file",
        src=str(repo_path.joinpath(name + ".j2")),
        dest=dest,
        context=context,
        mode="0664",
        user=user,
        group=group,
    )
    return dest


def place_consul_template_file(  # noqa: PLR0913
    name: str,
    repo_path: Path,
    template_path: Path,
    destination_path: Path,
    mode: str = "0664",
    user: str = "consul-template",
    group: str = "consul-template",
):
    """Places a consul template file on a destination system. Interpolation
    happens at startup based on values from consul + vault. A list of
    ConsulTemplateTemplate objects will be updated as well as a watch list for
    restarting services.

    :param name: The name the file will ultimately have on the destination system.
    :type name: str

    :param repo_path: The path within this code repository that contains the
        source template.
    :type repo_path: Path

    :param template_path: The path on the destination system where the template
        will be placed.
    :type template_path: Path

    :param destination_path: The path on the destination system for the file
        to be placed at. This does NOT include the filename, just the directory.
    :type destination_path: Path

    :param mode: The permissions specifier for the file on the destination system.
        Numerical form. both the unrendered template AND the rendered file will have
        the same permissions.
    :type mode: str

    :param user: The file user for the the unrendered template only. The rendered
        file will always have consul-template:consul-template usership.
        Set the mode accordingly.
    :type user: str

    :param group: The group for the unrendered template only. The rendered file will
        always have consul-template:consul-template usership. Set the mode accordingly.
    :type group: str

    :rtype: ConsulTemplateTemplate
    :returns: A ConsulTemplateTemplate object that can be appended to the master list
        of consul templates.
    """
    files.put(
        name=f"Place {name} template file.",
        src=str(repo_path.joinpath(name + ".tmpl")),
        dest=str(template_path.joinpath(name + ".tmpl")),
        mode=mode,
        user=user,
        group=group,
    )
    return ConsulTemplateTemplate(
        source=template_path.joinpath(name + ".tmpl"),
        destination=destination_path.joinpath(name),
        perms=mode,
    )
