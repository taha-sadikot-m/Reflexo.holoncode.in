"""Public legal pages and Meta's privacy callbacks (BETA_10 §8.3, §14; BETA_03 §14.3).

The pages render the documents in `legal/` (Markdown, versioned with the code): the privacy notice, the terms,
the rights page, the data-deletion instructions, and each shop's customer notice at `/{slug}/privacy`. Meta
requires the privacy, terms and data-deletion URLs and calls the two callbacks with a `signed_request`.
"""

from __future__ import annotations

import html
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from markdown_it import MarkdownIt

import reflexo.workflows.api as workflows

privacy = workflows.privacy

router = APIRouter()
_md = MarkdownIt("commonmark", {"html": False}).enable("table")
_STYLE = (
    "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:46rem;margin:2rem auto;"
    "padding:0 1rem;line-height:1.55;color:#1a1a1a}table{border-collapse:collapse}td,th{border:1px solid #ccc;"
    "padding:.3rem .5rem;text-align:left}code{background:#f3f3f3;padding:0 .2rem}"
)


def page_title(markdown: str) -> str:
    return next((ln.lstrip("# ").strip() for ln in markdown.splitlines() if ln.startswith("# ")), "")


def page_html(markdown: str) -> str:
    """A whole HTML page for a legal document (served here, and rendered to files by `scripts/render_legal.py`)."""
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(page_title(markdown))}</title>"
        f"<style>{_STYLE}</style></head><body>{_md.render(markdown)}</body></html>"
    )


def _page(markdown: str) -> HTMLResponse:
    return HTMLResponse(page_html(markdown))


@router.get("/privacy", include_in_schema=False)
async def privacy_page() -> HTMLResponse:
    return _page(privacy.page_markdown("privacy") or "")


@router.get("/terms", include_in_schema=False)
async def terms_page() -> HTMLResponse:
    return _page(privacy.page_markdown("terms") or "")


@router.get("/rights", include_in_schema=False)
async def rights_page() -> HTMLResponse:
    return _page(privacy.page_markdown("rights") or "")


@router.get("/data-deletion", include_in_schema=False)
async def data_deletion_page() -> HTMLResponse:
    return _page(privacy.page_markdown("data-deletion") or "")


async def _signed_request(request: Request) -> str:
    """Meta posts `signed_request` form-encoded."""
    values = parse_qs((await request.body()).decode("utf-8", "replace")).get("signed_request") or [""]
    return values[0]


@router.post("/meta/data-deletion", include_in_schema=False)
async def meta_data_deletion(request: Request) -> JSONResponse:
    signed_request = await _signed_request(request)
    try:
        return JSONResponse(await privacy.meta_data_deletion(signed_request))
    except privacy.BadSignedRequest as exc:
        raise HTTPException(status_code=400, detail="invalid signed_request") from exc


@router.post("/meta/deauthorize", include_in_schema=False)
async def meta_deauthorize(request: Request) -> JSONResponse:
    signed_request = await _signed_request(request)
    try:
        stopped = await privacy.meta_deauthorize(signed_request)
    except privacy.BadSignedRequest as exc:
        raise HTTPException(status_code=400, detail="invalid signed_request") from exc
    return JSONResponse({"ok": True, "connections_disconnected": stopped})


@router.get("/meta/deletion-status/{code}", include_in_schema=False)
async def meta_deletion_status(code: str) -> HTMLResponse:
    status = await privacy.deletion_status(code)
    return _page(f"# Data deletion\n\nConfirmation code: `{code}`\n\n{privacy.status_words(status)}\n")


@router.get("/{slug}/privacy", include_in_schema=False)
async def shop_privacy(slug: str) -> HTMLResponse:
    markdown = await privacy.shop_notice_markdown(slug)
    if markdown is None:
        raise HTTPException(status_code=404, detail="not found")
    return _page(markdown)
