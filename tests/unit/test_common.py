"""Unit tests for reflexo.common primitives."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from reflexo.common.errors import ErrorCode, ReflexoError
from reflexo.common.ids import id_timestamp_ms, new_id
from reflexo.common.logging import mask_phone_numbers
from reflexo.common.money import format_inr, group_indian, rupees_to_paise
from reflexo.common.ports import PortMissing, Registry
from reflexo.common.timeutil import FixedClock, scaled


def test_uuid7_version_variant_and_order() -> None:
    ids = [new_id() for _ in range(5000)]
    assert all(i.version == 7 and i.variant == uuid.RFC_4122 for i in ids)
    assert ids == sorted(ids, key=lambda u: u.int), "UUIDv7 must be strictly increasing within a process"
    assert len(set(ids)) == len(ids)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    assert abs(id_timestamp_ms(ids[-1]) - now_ms) < 5000


@pytest.mark.parametrize(
    ("rupees", "expected"),
    [(0, "0"), (950, "950"), (1499, "1,499"), (110000, "1,10,000"), (1234567, "12,34,567"), (-2500, "-2,500")],
)
def test_indian_grouping(rupees: int, expected: str) -> None:
    assert group_indian(rupees) == expected


@pytest.mark.parametrize(
    ("paise", "expected"),
    [(95000, "₹950"), (149900, "₹1,499"), (11000000, "₹1,10,000"), (9950, "₹99.50"), (5, "₹0.05")],
)
def test_format_inr(paise: int, expected: str) -> None:
    assert format_inr(paise) == expected


def test_rupees_to_paise() -> None:
    assert rupees_to_paise(950) == 95000
    assert rupees_to_paise(99.5) == 9950


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("call +91 98123 45678 now", "call ***5678 now"),
        ("wa 919812345678", "wa ***5678"),
        ("order MB-1042 total 950", "order MB-1042 total 950"),
        ("date 2026-10-15", "date 2026-10-15"),
    ],
)
def test_phone_masking(raw: str, expected: str) -> None:
    assert mask_phone_numbers(raw) == expected


def test_ports_registry() -> None:
    class SomePort:  # stands in for a Protocol
        pass

    reg = Registry()
    reg.declare_required(SomePort)
    with pytest.raises(PortMissing):
        reg.verify()
    impl = SomePort()
    reg.provide(SomePort, impl)
    reg.verify()
    assert reg.require(SomePort) is impl
    with pytest.raises(RuntimeError):
        reg.provide(SomePort, impl)


def test_error_envelope() -> None:
    err = ReflexoError(ErrorCode.AMBIGUOUS_MATCH, "2 products match", guidance="Ask which one")
    assert err.to_envelope() == {
        "ok": False,
        "error": {
            "code": "AMBIGUOUS_MATCH",
            "message": "2 products match",
            "retryable": False,
            "guidance": "Ask which one",
        },
    }


def test_scaled_and_fixed_clock() -> None:
    assert scaled(timedelta(days=1), scale=1 / 1440) == timedelta(seconds=60)
    clock = FixedClock(datetime(2026, 10, 14, 10, 0, tzinfo=UTC))
    clock.advance(timedelta(minutes=5))
    assert clock.now().minute == 5
    with pytest.raises(ValueError, match="aware"):
        FixedClock(datetime(2026, 10, 14))


def test_demo_env_refuses_placeholder_secrets() -> None:
    import pytest
    from pydantic import ValidationError

    from reflexo.common.config import Settings

    with pytest.raises(ValidationError, match="REFLEXO_MEDIA_URL_SECRET"):
        Settings(
            env="demo",
            media_url_secret="dev-media-secret-change-me",
            simulator_secret="x" * 32,
            admin_password="p" * 20,
            admin_session_secret="a" * 32,
        )
    deployed = {"customer_notice_enabled": True, "operator_totp_required": True, "operator_totp_secret": "J" * 32}
    ok = Settings(
        env="demo",
        media_url_secret="m" * 32,
        simulator_secret="s" * 32,
        admin_password="p" * 20,
        admin_session_secret="a" * 32,
        **deployed,
    )
    strong = {"media_url_secret": "m" * 32, "simulator_secret": "s" * 32, "admin_password": "p" * 20,
              "admin_session_secret": "a" * 32}  # fmt: skip
    with pytest.raises(ValidationError, match="may be off only in dev"):  # BETA_10 §6, §13
        Settings(env="demo", **strong, **(deployed | {"customer_notice_enabled": False}))
    with pytest.raises(ValidationError, match="REFLEXO_OPERATOR_TOTP_SECRET"):
        Settings(env="demo", **strong, **(deployed | {"operator_totp_secret": ""}))
    with pytest.raises(ValidationError, match="REFLEXO_ADMIN_PASSWORD"):
        Settings(
            env="demo",
            media_url_secret="m" * 32,
            simulator_secret="s" * 32,
            admin_password="change-me",
            admin_session_secret="a" * 32,
        )
    assert ok.env == "demo"
    assert Settings(env="dev").media_url_secret.get_secret_value()  # dev keeps working defaults


def test_plain_dsn_for_drivers_used_directly() -> None:
    from reflexo.common.db import plain_dsn

    local = "postgresql+asyncpg://reflexo_service:pw@localhost:5442/reflexo"
    assert plain_dsn(local) == "postgresql://reflexo_service:pw@localhost:5442/reflexo"
    managed = "postgresql+asyncpg://u:p@ep-x.ap-southeast-1.aws.neon.tech/neondb?ssl=require"
    assert plain_dsn(managed) == "postgresql://u:p@ep-x.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
