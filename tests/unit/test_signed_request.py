"""Meta's signed_request is refused unless every part checks out (BETA_10 §8.3; CHK-SEC-13)."""

from __future__ import annotations

import pytest

from reflexo.workflows.privacy import BadSignedRequest, parse_signed_request
from tests.integration.test_privacy import signed

GOOD = {"algorithm": "HMAC-SHA256", "user_id": "fb-1"}


@pytest.mark.check("CHK-SEC-13")
@pytest.mark.parametrize(
    ("raw", "secret", "why"),
    [
        (signed(GOOD, "s3cret"), "", "no app secret"),
        ("no-dot-at-all", "s3cret", "malformed"),
        (signed(GOOD, "other"), "s3cret", "bad signature"),
        (signed({"algorithm": "none", "user_id": "fb-1"}, "s3cret"), "s3cret", "unexpected algorithm"),
        (signed({"algorithm": "HMAC-SHA256"}, "s3cret"), "s3cret", "no user_id"),
    ],
)
def test_a_signed_request_that_does_not_check_out_is_refused(raw: str, secret: str, why: str) -> None:
    with pytest.raises(BadSignedRequest, match=why):
        parse_signed_request(raw, secret)


def test_a_good_signed_request_gives_its_payload() -> None:
    assert parse_signed_request(signed(GOOD, "s3cret"), "s3cret")["user_id"] == "fb-1"
