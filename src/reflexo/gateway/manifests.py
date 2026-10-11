"""Capability manifests per provider (POC_03 §8.2). Values marked (verify) in the spec are kept conservative."""

from __future__ import annotations

from reflexo.gateway.adapters.base import Manifest

MANIFESTS: dict[str, Manifest] = {
    "meta_whatsapp": Manifest(
        provider="meta_whatsapp",
        max_text=4096,
        max_buttons=3,
        button_title_max=20,
        list_supported=True,
        list_max_rows=10,
        row_title_max=24,
        carousel_supported=False,
        carousel_max=0,
        caption_max=1024,
        link_button=False,
        session_window_hours=24,
    ),
    "meta_pages": Manifest(
        provider="meta_pages",
        max_text=2000,
        max_buttons=3,
        button_title_max=20,
        list_supported=False,
        list_max_rows=0,
        row_title_max=0,
        carousel_supported=True,
        carousel_max=10,
        caption_max=0,
        link_button=True,
        session_window_hours=24,
        quick_replies_max=13,
    ),
    "meta_instagram": Manifest(
        provider="meta_instagram",
        max_text=1000,
        max_buttons=0,
        button_title_max=20,
        list_supported=False,
        list_max_rows=0,
        row_title_max=0,
        carousel_supported=False,
        carousel_max=0,
        caption_max=0,
        link_button=False,
        session_window_hours=24,
        quick_replies_max=13,
    ),
    "telegram": Manifest(
        provider="telegram",
        max_text=4096,
        max_buttons=8,
        button_title_max=64,
        list_supported=True,
        list_max_rows=20,
        row_title_max=64,
        carousel_supported=False,
        carousel_max=0,
        caption_max=1024,
        link_button=True,
        session_window_hours=None,
    ),
    "simulator": Manifest(
        provider="simulator",
        max_text=4096,
        max_buttons=10,
        button_title_max=80,
        list_supported=True,
        list_max_rows=30,
        row_title_max=80,
        carousel_supported=True,
        carousel_max=10,
        caption_max=1024,
        link_button=True,
        session_window_hours=None,
    ),
}


def manifest_for(provider: str) -> Manifest:
    return MANIFESTS[provider]
