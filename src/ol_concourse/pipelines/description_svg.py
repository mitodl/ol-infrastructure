"""Render short pipeline description text as an SVG for use as a
``display.background_image``.

Concourse has no native pipeline description/documentation field. The only
pipeline-level visual customization hook is ``display.background_image``
(see ``ol_concourse.lib.models.pipeline.DisplayConfig``), which Concourse
blends into the pipeline tile at 30% opacity, grayscaled, by default, and
draws the job graph on top of it starting from the top-left. This module
renders a description string into a minimal SVG that reads reasonably under
that blend, meant to be committed alongside each pipeline's ``pipeline.py``
and served via a raw.githubusercontent.com URL.

Concourse applies the image via CSS ``background-size: cover;
background-position: center`` (see ``web/elm/src/Pipeline/Styles.elm`` in
concourse/concourse) — there is no user-facing zoom/rescale control. ``cover``
scales the image up until it fills the container in *both* dimensions, then
crops whatever overflows, centered. A source image with an extreme aspect
ratio (very wide and short, or very tall and narrow) gets scaled up hard on
its short axis to satisfy ``cover``, blowing its long axis far past the
viewport. The mitigations are in the source image, not in Concourse: pick an
aspect ratio closer to a typical browser viewport, and keep content within a
smaller, centered fraction of the canvas — the crop is always centered, so
margin from the canvas center (not from a particular edge) is what
determines whether content survives.
"""

import textwrap
from dataclasses import dataclass
from typing import Literal

# 16:9 reads much closer to a typical viewport than a short, wide banner, which
# keeps the "cover" scale-up factor (and thus the risk of content running off
# the visible crop) far smaller than an extreme aspect ratio would.
_DEFAULT_WIDTH = 1200
_DEFAULT_HEIGHT = 675
_DEFAULT_FONT_SIZE = 30
_DEFAULT_PADDING = 20
_LINE_HEIGHT_RATIO = 1.35
# Rough average glyph width for a sans-serif font, as a fraction of font_size.
_AVG_CHAR_WIDTH_RATIO = 0.55
# Cap the text block to this fraction of canvas width by default, centered,
# so there's margin on both sides before a "cover" crop reaches it.
_DEFAULT_TEXT_WIDTH_FRACTION = 0.6

Anchor = Literal["top", "center", "bottom"]


@dataclass(frozen=True)
class DescriptionStyle:
    """Sizing/scaling/contrast/positioning knobs for :func:`render_description_svg`.

    ``anchor`` controls where the text block sits vertically (top/center/
    bottom) so it can be steered clear of a given pipeline's job graph, which
    Concourse draws starting from the top-left of the canvas — a small
    pipeline is least likely to occlude a bottom-anchored block. Panning by
    click-and-drag in the Concourse UI is always available as a fallback for
    larger graphs.

    ``outline_color``/``outline_width`` add a stroke around the text itself
    for contrast against a busy graph; ``panel_opacity``/``panel_color`` (0
    by default, i.e. no panel) additionally draw a solid backdrop rectangle
    behind the whole text block. Both are independent, combinable contrast
    controls. ``width``/``height``/``font_size`` control overall scaling;
    ``wrap_chars`` overrides the automatic line-wrap width entirely.
    ``text_width_fraction`` instead caps the auto-derived wrap width to a
    fraction of ``width`` (default 0.6, i.e. text stays centered within the
    middle 60%) — since Concourse renders this image with
    ``background-size: cover`` and no rescale control, keeping the text
    block narrower than the full canvas and centered is what makes it
    survive being cropped on viewports with a different aspect ratio than
    ``width``/``height`` (see module docstring).
    """

    width: int = _DEFAULT_WIDTH
    height: int = _DEFAULT_HEIGHT
    font_size: int = _DEFAULT_FONT_SIZE
    padding: int = _DEFAULT_PADDING
    anchor: Anchor = "bottom"
    wrap_chars: int | None = None
    text_width_fraction: float = _DEFAULT_TEXT_WIDTH_FRACTION
    text_color: str = "white"
    outline_color: str | None = "black"
    outline_width: float = 3
    panel_opacity: float = 0.0
    panel_color: str = "black"


def render_description_svg(text: str, style: DescriptionStyle | None = None) -> str:
    """Render ``text`` as a wrapped SVG suitable for ``display.background_image``,
    styled per ``style`` (see :class:`DescriptionStyle`; defaults if omitted).
    """
    style = style or DescriptionStyle()
    line_height = round(style.font_size * _LINE_HEIGHT_RATIO)
    wrap_chars = style.wrap_chars
    if wrap_chars is None:
        text_width = min(
            style.width - 2 * style.padding,
            style.width * style.text_width_fraction,
        )
        avg_char_width = style.font_size * _AVG_CHAR_WIDTH_RATIO
        wrap_chars = max(10, int(text_width / avg_char_width))
    lines = textwrap.wrap(text, width=wrap_chars)
    block_height = len(lines) * line_height

    if style.anchor == "top":
        first_line_y = float(style.padding + style.font_size)
    elif style.anchor == "bottom":
        first_line_y = float(
            style.height - style.padding - block_height + style.font_size
        )
    else:
        first_line_y = (style.height - block_height) / 2 + style.font_size

    text_attrs = f'fill="{style.text_color}"'
    if style.outline_color:
        text_attrs += (
            f' stroke="{style.outline_color}" stroke-width="{style.outline_width}"'
            ' paint-order="stroke"'
        )

    tspans = "\n    ".join(
        f'<tspan x="50%" dy="{0 if i == 0 else line_height}">{_escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )

    panel = ""
    if style.panel_opacity > 0:
        panel_y = max(
            0.0, first_line_y - style.font_size - (line_height - style.font_size)
        )
        panel_height = min(style.height - panel_y, block_height + line_height)
        panel = (
            f'  <rect x="0" y="{panel_y:.1f}" width="{style.width}" '
            f'height="{panel_height:.1f}" fill="{style.panel_color}" '
            f'fill-opacity="{style.panel_opacity}" />\n'
        )

    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" width="{style.width}" height="{style.height}" \
viewBox="0 0 {style.width} {style.height}">
{panel}  <text x="50%" y="{first_line_y:.1f}" text-anchor="middle" \
font-family="sans-serif" font-size="{style.font_size}" {text_attrs}>
    {tspans}
  </text>
</svg>
"""


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_description_svg(
    text: str, path: str, style: DescriptionStyle | None = None
) -> None:
    """Render ``text`` and write it to ``path`` (typically
    ``description.svg`` next to a pipeline's ``pipeline.py``).
    """
    with open(path, "w") as description_file:  # noqa: PTH123
        description_file.write(render_description_svg(text, style))
