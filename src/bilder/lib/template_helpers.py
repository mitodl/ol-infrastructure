from pathlib import Path

from pyinfra.operations import files

from bilder.components.hashicorp.consul_template.models import ConsulTemplateTemplate

CONSUL_TEMPLATE_DIRECTORY = "/etc/consul-template"


def place_jinja_template_file(
    name: str,
    repo_path: Path,
    destination_path: Path,
    context: dict,
    watched_files: list[Path],
    mode: str = "0644",
):
    """Interpolate and place a file on the destination at AMI bake time.

    Predicate function that side effects multiple arguments and has no return.

    :param name: The name the file will ultimately have on the destination system.
    :type name: str

    :param repo_path: The path within this code repository that contains the source template.
    :type repo_path: Path

    :param destination_path: The path on the destination system for the file to be placed at.
    This does NOT include the filename, just the directory.
    :type destination_path: Path

    :param context: A dictionary containing the values to interpolate into the tempalte.
    :type context: dict

    :param watched_files: A list of files that will be watched on the system and used to
    trigger service restarts when changed. This function will append the combined
    destination_path + name to the list.
    :type watched_files: list[Path]

    :param mode: The permissions specifier for the file on the destination system. Numerical form.
    :type mode: str

    :returns: Nothing
    :rtype: None

    """
    files.template(
        name=f"Place and interpolate {name} jinja template file",
        src=str(repo_path.joinpath(name + ".j2")),
        dest=str(destination_path.joinpath(name)),
        context=context,
        mode="0664",
    )
    watched_files.append(destination_path.joinpath(name))


def place_consul_template_file(
    name: str,
    repo_path: Path,
    template_path: Path,
    destination_path: Path,
    consul_templates: list[ConsulTemplateTemplate],
    watched_files: list[Path],
    mode: str = "0664",
):
    """Places a consul template file on a destination system. Interpolation
    happens at startup based on values from consul + vault. A list of
    ConsulTemplateTemplate objects will be updated as well as a watch list for
    restarting services.

    :param name: The name the file will ultimately have on the destination system.
    :type name: str

    :param repo_path: The path within this code repository that contains the source template.
    :type repo_path: Path

    :param template_path: The path on the destination system where the template will be placed.
    :type template_path: Path

    :param destination_path: The path on the destination system for the file to be placed at.
    This does NOT include the filename, just the directory.
    :type destination_path: Path

    :param consul_templates: A list of ConsulTemplateTemplate objects for tracking / creating the consul
    configuration. This function will sideffect this list and appaned a new ConsulTemplateTemplate
    object to the list.
    :type consul_templates: List[ConsulTemplateTemplate]

    :param watched_files: A list of files that will be watched on the system and used to
    trigger service restarts when changed. This function will append the combined
    destination_path + name to the list.
    :type watched_files: list[Path]

    :param mode: The permissions specifier for the file on the destination system. Numerical form.
    both the unrendered template AND the rendered file will have the same permissions.
    :type mode: str

    :returns: Nothing
    :rtype: None
    """
    files.put(
        name=f"Place {name} template file.",
        src=str(repo_path.joinpath(name + ".tmpl")),
        dest=str(template_path.joinpath(name + ".tmpl")),
        mode=mode,
    )
    consul_templates.append(
        ConsulTemplateTemplate(
            source=template_path.joinpath(name + ".tmpl"),
            destination=destination_path.joinpath(name),
            perms=mode,
        )
    )
    watched_files.append(destination_path.joinpath(name))
