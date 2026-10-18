"""Render the public legal pages to static HTML, for a host that serves them without the app (BETA_10 §14).

uv run python scripts/render_legal.py OUT_DIR

The pages are filled from this machine's settings (REFLEXO_PRODUCT_NAME, REFLEXO_PROVIDER_NAME, ...), exactly as the
API serves them. OUT_DIR gets one `<page>.html` per page and an `index.html` listing them; Caddy serves the folder
(`REFLEXO_LEGAL_DOMAIN`, deploy/Caddyfile). The output names the provider's address: never commit it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reflexo.api.routes.legal import page_html, page_title
from reflexo.workflows import privacy


def render(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written, links = [], []
    for page in privacy.PAGES:
        markdown = privacy.page_markdown(page) or ""
        path = out / f"{page}.html"
        path.write_text(page_html(markdown), encoding="utf-8")
        written.append(path)
        links.append(f"- [{page_title(markdown)}]({page})")
    values = privacy.legal_values()
    index = out / "index.html"
    index.write_text(
        page_html(f"# {values['product_name']}\n\n{values['provider_name']}\n\n" + "\n".join(links)), encoding="utf-8"
    )
    return [*written, index]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    for p in render(Path(sys.argv[1])):
        print(p)
