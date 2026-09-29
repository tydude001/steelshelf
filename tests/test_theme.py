"""Every colour comes from the :root tokens, so the dark scheme can redefine it."""

import re
from pathlib import Path

CSS = Path("app/static/steelshelf.css").read_text()
COLOUR = re.compile(r"#[0-9A-Fa-f]{3,8}\b|rgba?\(|hsla?\(")


def _outside_tokens(css: str) -> str:
    """The sheet minus the light :root block and the dark scheme's block."""
    css = re.sub(r"^:root \{.*?^\}", "", css, count=1, flags=re.S | re.M)
    return re.sub(r"^@media \(prefers-color-scheme: dark\) \{.*?^\}", "", css, count=1,
                  flags=re.S | re.M)


def test_no_colour_outside_the_tokens():
    rest = _outside_tokens(CSS)
    assert rest != CSS
    assert COLOUR.findall(rest) == []


def test_the_dark_scheme_redefines_the_shelf_tokens():
    dark = CSS.split("@media (prefers-color-scheme: dark)")[1].split("\n}\n")[0]
    for token in ("--bg", "--card", "--wall", "--brass-hi", "--brass-lo", "--art-sheen",
                  "--title-shadow", "--steel", "--shadow", "--track", "--teal-soft"):
        assert f"{token}:" in dark, token


def test_templates_paint_no_colour_of_their_own():
    for page in Path("app/templates").glob("*.html"):
        text = page.read_text()
        assert not re.search(r'fill="#|stroke="#|style="[^"]*#[0-9A-Fa-f]{3}', text), page.name
