"""Knowledge pure logic: query understanding, sizes, opening hours, delivery quotes, profile rendering (POC_05 §1)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from reflexo.common.ids import new_id
from reflexo.knowledge import profile as profile_mod
from reflexo.knowledge.models import BusinessProfile
from reflexo.knowledge.query import canonical_size, parse_query

SEED = Path(__file__).resolve().parents[2] / "config" / "seeds" / "meeras_bakes.yaml"


@pytest.fixture(scope="module")
def meera() -> profile_mod.LoadedProfile:
    raw = yaml.safe_load(SEED.read_text(encoding="utf-8"))["profile"]
    return profile_mod.LoadedProfile(new_id(), 3, "Asia/Kolkata", BusinessProfile.model_validate(raw))


def ist(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    """IST wall time → UTC (IST = UTC+5:30)."""
    from zoneinfo import ZoneInfo

    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)


# ---------------------------------------------------------------- query parsing


@pytest.mark.parametrize(
    ("raw", "text", "max_price", "size"),
    [
        ("chocolate truffle 1kg price", "chocolate truffle", None, "1 kg"),
        ("choclate trufle", "choclate trufle", None, None),
        ("1,000 ke andar cake", "cake", 100000, None),
        ("1000 से कम केक", "केक", 100000, None),
        ("under rs 1.5k brownies", "brownies", 150000, None),
        ("₹1000 tak", "", 100000, None),  # filters only → browse
        ("budget 800", "", 80000, None),
        ("chocolateee cake aadha kilo kitne ka hai", "chocolatee cake", None, "500 g"),
        ("red velvet 2 kg", "red velvet", None, "2 kg"),
        ("do kilo cake", "cake", None, "2 kg"),
        ("cake 500gm", "cake", None, "500 g"),
        ("1.5 kg cake", "cake", None, "1.5 kg"),
        ("एक किलो चॉकलेट केक", "चॉकलेट केक", None, "1 kg"),
        ("kya aap deliver karte ho", "deliver", None, None),
        ("kya hai", "kya hai", None, None),  # only filler: keep the sentence
    ],
)
def test_parse_query(raw: str, text: str, max_price: int | None, size: str | None) -> None:
    pq = parse_query(raw)
    assert (pq.text, pq.max_price_paise, pq.size_hint) == (text, max_price, size)


def test_repeat_collapse_only_touches_letters() -> None:
    assert parse_query("chocolateeeee").normalized == "chocolatee"
    assert parse_query("10000 ke andar").max_price_paise == 1000000


def test_weight_is_not_a_price() -> None:
    pq = parse_query("cake under 2 kg")
    assert pq.max_price_paise is None
    assert pq.size_hint == "2 kg"


@pytest.mark.parametrize(
    ("name", "canonical"),
    [("1 kg", "1 kg"), ("1kg", "1 kg"), ("1000 g", "1 kg"), ("500 g", "500 g"), ("0.5 kg", "500 g"),
     ("Half kg", "500 g"), ("Box of 6", None), ("Jar of 12", None)],
)  # fmt: skip
def test_canonical_size(name: str, canonical: str | None) -> None:
    assert canonical_size(name) == canonical


# ---------------------------------------------------------------- hours


def test_is_open(meera: profile_mod.LoadedProfile) -> None:
    # 2026-10-12 is a Monday: 10:00–20:00
    assert profile_mod.is_open(meera, ist(2026, 10, 12, 10, 0))
    assert profile_mod.is_open(meera, ist(2026, 10, 12, 19, 59))
    assert not profile_mod.is_open(meera, ist(2026, 10, 12, 20, 0))
    assert not profile_mod.is_open(meera, ist(2026, 10, 12, 9, 30))
    # Sunday 11:00–18:00
    assert not profile_mod.is_open(meera, ist(2026, 10, 11, 10, 30))
    assert profile_mod.is_open(meera, ist(2026, 10, 11, 11, 30))


def test_next_open(meera: profile_mod.LoadedProfile) -> None:
    nxt = profile_mod.next_open(meera, ist(2026, 10, 12, 21, 0))  # Monday night
    assert nxt is not None and (nxt.date(), nxt.hour) == (date(2026, 10, 13), 10)
    nxt = profile_mod.next_open(meera, ist(2026, 10, 11, 8, 0))  # Sunday morning
    assert nxt is not None and (nxt.date(), nxt.hour) == (date(2026, 10, 11), 11)


def test_date_exception_closes_a_day(meera: profile_mod.LoadedProfile) -> None:
    from reflexo.knowledge.models import DateException

    hours = meera.profile.hours.model_copy(update={"exceptions": [DateException(date=date(2026, 10, 20))]})
    closed = profile_mod.LoadedProfile(
        meera.business_id, 4, meera.timezone, meera.profile.model_copy(update={"hours": hours})
    )
    assert not profile_mod.is_open(closed, ist(2026, 10, 20, 12, 0))
    nxt = profile_mod.next_open(closed, ist(2026, 10, 19, 21, 0))
    assert nxt is not None and nxt.date() == date(2026, 10, 21)


def test_earliest_delivery_date(meera: profile_mod.LoadedProfile) -> None:
    # Monday 15:00 + 24 h notice → Tuesday
    assert profile_mod.earliest_delivery_date(meera, ist(2026, 10, 12, 15, 0)) == date(2026, 10, 13)
    # Saturday 22:00 + 24 h = Sunday 22:00, after Sunday closing (18:00) → Monday
    assert profile_mod.earliest_delivery_date(meera, ist(2026, 10, 10, 22, 0)) == date(2026, 10, 12)
    # custom photo cakes: 48 h
    assert profile_mod.earliest_delivery_date(meera, ist(2026, 10, 12, 15, 0), 48) == date(2026, 10, 14)


# ---------------------------------------------------------------- delivery quotes


@pytest.mark.parametrize(
    ("where", "serviceable", "matched_by"),
    [
        ("411038", True, "pincode"),
        ("my pincode is 411052, Warje", True, "pincode"),
        ("400001", False, "none"),  # an unknown pincode is not fuzzy-matched
        ("Karve Nagar", True, "area"),
        ("karve nagr", True, "area"),  # misspelling
        ("near Deccan Gymkhana", True, "area"),
        ("Hinjewadi", False, "none"),
    ],
)
def test_delivery_quote(meera: profile_mod.LoadedProfile, where: str, serviceable: bool, matched_by: str) -> None:
    q = profile_mod.delivery_quote(meera, where)
    assert (q.serviceable, q.matched_by) == (serviceable, matched_by)
    assert q.notice_hours == 24 and q.pickup_available
    if serviceable:
        assert (q.zone_id, q.fee_paise, q.free_above_paise) == ("kothrud-10km", 5000, 150000)


# ---------------------------------------------------------------- rendering


def test_profile_render_contains_facts(meera: profile_mod.LoadedProfile) -> None:
    rendered = profile_mod.render(meera)
    assert rendered.version == 3
    assert "Meera's Bakes" in rendered.text
    assert "Mon: 10:00–20:00" in rendered.text
    assert "fee ₹50, free above ₹1,500" in rendered.text
    assert "UPI (payment link)" in rendered.text
    assert "formality aap" in rendered.text


def test_profile_validation_rejects_bad_times() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BusinessProfile.model_validate(
            {
                "identity": {"name": "X"},
                "hours": {"weekly": [{"day": "mon", "intervals": [{"open": "9", "close": "25:00"}]}]},
            }
        )
