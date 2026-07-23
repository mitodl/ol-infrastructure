"""Render short pipeline description text as an SVG for use as a
``display.background_image``.

Concourse has no native pipeline description/documentation field. The only
pipeline-level visual customization hook is ``display.background_image``
(see ``ol_concourse.lib.models.pipeline.DisplayConfig``), which Concourse
blends into the pipeline tile at 30% opacity, grayscaled, by default. This
module renders a description string into a minimal SVG that reads reasonably
under that blend, meant to be committed alongside each pipeline's
``pipeline.py`` and served via a raw.githubusercontent.com URL.
"""

import textwrap

_WIDTH = 800
_HEIGHT = 200
_FONT_SIZE = 22
_LINE_HEIGHT = 30
_WRAP_CHARS = 46
_PADDING_TOP = 40


def render_description_svg(text: str) -> str:
    """Render ``text`` as a wrapped, centered SVG suitable for
    ``display.background_image``.
    """
    lines = textwrap.wrap(text, width=_WRAP_CHARS)
    tspans = "\n    ".join(
        f'<tspan x="50%" dy="{0 if i == 0 else _LINE_HEIGHT}">{_escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )
    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" \
viewBox="0 0 {_WIDTH} {_HEIGHT}">
  <text x="50%" y="{_PADDING_TOP}" text-anchor="middle" \
font-family="sans-serif" font-size="{_FONT_SIZE}" fill="white">
    {tspans}
  </text>
</svg>
"""


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_description_svg(text: str, path: str) -> None:
    """Render ``text`` and write it to ``path`` (typically
    ``description.svg`` next to a pipeline's ``pipeline.py``).
    """
    with open(path, "w") as description_file:  # noqa: PTH123
        description_file.write(render_description_svg(text))
