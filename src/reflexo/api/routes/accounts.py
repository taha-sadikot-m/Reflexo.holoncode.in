"""Owner accounts API (`/api/accounts`): sign up (a new shop + its owner's login) and log in.

Both answer with the owner session (owner cookie + CSRF token); from there the owner's app uses the owner-scoped
admin API (`/admin/api`), locked to their business.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

import reflexo.admin.api as admin
from reflexo.api.routes.admin import problem
from reflexo.common.config import get_settings

router = APIRouter(prefix="/api/accounts")


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _sign_in(response: Response, account: admin.accounts.Account) -> dict[str, Any]:
    settings = get_settings()
    token, s = admin.issue_session(
        account.owner_name, business_id=str(account.business_id), hours=settings.owner_session_hours
    )
    response.set_cookie(
        admin.OWNER_COOKIE,
        token,
        max_age=settings.owner_session_hours * 3600,
        httponly=True,
        secure=settings.env != "dev",
        samesite="strict",
        path="/admin",
    )
    return {"business_id": str(account.business_id), "name": account.business_name, "csrf": s.csrf}


class SignupBody(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)
    business_name: str = Field(max_length=80)
    owner_name: str = Field(max_length=60)
    category: str = Field(default="", max_length=80)
    area: str = Field(default="", max_length=120)
    accept_terms: bool = False  # the Seller Terms and the Privacy Notice (BETA_10 §6)


@router.post("/signup")
async def signup(body: SignupBody, request: Request, response: Response) -> dict[str, Any]:
    try:
        account = await admin.accounts.signup(
            email=body.email,
            password=body.password,
            business_name=body.business_name,
            owner_name=body.owner_name,
            category=body.category,
            area=body.area,
            client=_client(request),
            accept_terms=body.accept_terms,
        )
    except admin.accounts.SignupRefused as exc:
        headers = {"Retry-After": str(exc.retry_after_s)} if exc.retry_after_s else None
        raise problem(exc.status, "SIGNUP_REFUSED", exc.reason, headers) from None
    return _sign_in(response, account)


class LoginBody(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    client = _client(request)
    if admin.login_throttled(client):
        raise problem(429, "RATE_LIMITED", "too many attempts; try again in a few minutes")
    account = await admin.accounts.login(body.email, body.password)
    if account is None:
        admin.record_login_failure(client)
        raise problem(401, "INVALID_CREDENTIALS", "wrong email or password")
    return _sign_in(response, account)
