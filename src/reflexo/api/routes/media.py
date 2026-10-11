"""Signed media URLs for providers fetching outbound media (POC_03 §3.2): /media/{id}?exp=&sig=."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import reflexo.gateway.api as gateway

router = APIRouter()


@router.get("/media/{media_id}", include_in_schema=False)
async def media(media_id: UUID, exp: int, sig: str) -> FileResponse:
    if not gateway.media_signer().valid(media_id, exp, sig):
        raise HTTPException(status_code=403, detail="invalid or expired link")
    found = await gateway.media_path(media_id)
    if found is None or not found[0].exists():
        raise HTTPException(status_code=404, detail="not found")
    path, mime = found
    return FileResponse(path, media_type=mime)
