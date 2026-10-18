"""The static legal site is the same pages the API serves, filled from the settings (BETA_10 §14)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from reflexo.api.routes.legal import page_html
from reflexo.workflows import privacy

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import render_legal


def test_every_page_is_rendered_as_served_and_listed_on_the_index(tmp_path: Path) -> None:
    written = render_legal.render(tmp_path / "site")
    assert {p.name for p in written} == {f"{page}.html" for page in privacy.PAGES} | {"index.html"}
    for page in privacy.PAGES:
        text = (tmp_path / "site" / f"{page}.html").read_text(encoding="utf-8")
        assert text == page_html(privacy.page_markdown(page) or ""), page
        assert not re.search(r"\{[a-z_]+\}", text), page  # every placeholder filled
    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    for page in privacy.PAGES:
        assert f'href="{page}"' in index, page
    assert privacy.legal_values()["provider_name"] in index
