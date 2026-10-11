"""Business profile: loading (cached by version), deterministic helpers, and the prompt block (POC_05 §1.1).

The model never computes opening hours, delivery fees or earliest dates; these helpers do.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reflexo.common.db import get_db
from reflexo.common.money import format_inr
from reflexo.common.redis import RedisUnavailable, get_redis
from reflexo.common.text import normalize
from reflexo.common.timeutil import to_business_tz, utcnow
from reflexo.knowledge.models import (
    DAYS,
    BusinessProfile,
    DeliveryQuote,
    Hours,
    Interval,
    RenderedProfile,
)

_PINCODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CACHE_TTL_S = 3600

# Profile sections indexed as search documents; ids are stable per business (uuid5) so upserts are idempotent.
SECTIONS: dict[str, str] = {
    "about": "about us who are you what do you make shop bakery kaun ho",
    "hours": "opening hours timings timing open close closed today sunday kab khulte kab tak khula band samay",
    "delivery": "delivery area deliver pincode zone charges fee pickup kahan tak deliver ghar tak",
    "policies": "policy returns refund cancellation custom order cancel wapas",
    "payments": "payment pay upi card cod cash on delivery payment kaise karu online",
    "contact": "contact phone number address location maps website kahan ho pata",
}


def section_entity_id(business_id: UUID, section: str) -> UUID:
    return uuid5(business_id, f"profile:{section}")


@dataclass(frozen=True)
class LoadedProfile:
    business_id: UUID
    version: int
    timezone: str
    profile: BusinessProfile


_local: dict[tuple[UUID, int], RenderedProfile] = {}


async def load(business_id: UUID, s: AsyncSession | None = None) -> LoadedProfile:
    sql = text("""SELECT p.version, p.profile, b.timezone FROM business_profiles p
                  JOIN businesses b ON b.id = p.business_id WHERE p.business_id = :b""")
    if s is not None:
        row = (await s.execute(sql, {"b": business_id})).one()
    else:
        async with get_db().tenant_session(business_id) as session:
            row = (await session.execute(sql, {"b": business_id})).one()
    return LoadedProfile(business_id, int(row.version), str(row.timezone), BusinessProfile.model_validate(row.profile))


# ---------------------------------------------------------------- hours


def _intervals_for(hours: Hours, d: date) -> list[Interval]:
    for exc in hours.exceptions:
        if exc.date == d:
            return [] if exc.closed else exc.intervals
    day = DAYS[d.weekday()]
    for dh in hours.weekly:
        if dh.day == day:
            return dh.intervals
    return []


def _t(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def is_open(lp: LoadedProfile, at: datetime | None = None) -> bool:
    local = to_business_tz(at or utcnow(), lp.timezone)
    now_t = local.time()
    return any(_t(i.open) <= now_t < _t(i.close) for i in _intervals_for(lp.profile.hours, local.date()))


def next_open(lp: LoadedProfile, at: datetime | None = None) -> datetime | None:
    """The next opening moment (business-local, tz-aware) strictly after `at`; None if closed for 14 days."""
    local = to_business_tz(at or utcnow(), lp.timezone)
    for offset in range(15):
        d = local.date() + timedelta(days=offset)
        for i in sorted(_intervals_for(lp.profile.hours, d), key=lambda x: x.open):
            start = datetime.combine(d, _t(i.open), tzinfo=local.tzinfo)
            if start > local:
                return start
    return None


def earliest_delivery_date(lp: LoadedProfile, at: datetime | None = None, notice_hours: int | None = None) -> date:
    """now + notice hours, moved to the next day the business is open (POC_05 §1.1)."""
    local = to_business_tz(at or utcnow(), lp.timezone)
    ready = local + timedelta(hours=lp.profile.delivery.notice_hours if notice_hours is None else notice_hours)
    for offset in range(15):
        d = ready.date() + timedelta(days=offset)
        intervals = _intervals_for(lp.profile.hours, d)
        if not intervals:
            continue
        if offset == 0 and all(_t(i.close) <= ready.time() for i in intervals):
            continue  # ready only after closing time on that day
        return d
    return ready.date()


# ---------------------------------------------------------------- delivery


def _trigram_sim(a: str, b: str) -> float:
    def grams(s: str) -> set[str]:
        s = f"  {s} "
        return {s[i : i + 3] for i in range(len(s) - 2)}

    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def delivery_quote(lp: LoadedProfile, area_or_pincode: str) -> DeliveryQuote:
    delivery = lp.profile.delivery
    base = {
        "notice_hours": delivery.notice_hours,
        "pickup_available": delivery.pickup_available,
        "pickup_address": delivery.pickup_address,
    }

    def quote(zone_index: int, matched_by: str) -> DeliveryQuote:
        z = delivery.zones[zone_index]
        return DeliveryQuote(
            serviceable=True,
            zone_id=z.id,
            zone_label=z.label,
            fee_paise=z.fee_paise,
            free_above_paise=z.free_above_paise,
            eta=z.eta,
            matched_by=matched_by,
            **base,
        )

    pin = _PINCODE.search(area_or_pincode)
    if pin:
        for i, z in enumerate(delivery.zones):
            if pin.group(1) in z.pincodes:
                return quote(i, "pincode")
        return DeliveryQuote(serviceable=False, **base)  # a pincode is exact: no fuzzy fallback
    q = normalize(area_or_pincode)
    best, best_score = -1, 0.0
    for i, z in enumerate(delivery.zones):
        for area in z.areas:
            a = normalize(area)
            score = 1.0 if f" {a} " in f" {q} " else _trigram_sim(q, a)
            if score > best_score:
                best, best_score = i, score
    if best >= 0 and best_score >= 0.6:
        return quote(best, "area")
    return DeliveryQuote(serviceable=False, **base)


# ---------------------------------------------------------------- rendering


def _fmt_intervals(intervals: list[Interval]) -> str:
    return ", ".join(f"{i.open}–{i.close}" for i in intervals) or "closed"


def section_text(lp: LoadedProfile, section: str) -> str:
    p = lp.profile
    if section == "about":
        parts = [p.identity.name, p.identity.description or "", p.identity.category or ""]
        if p.identity.owner_name:
            parts.append(f"Run by {p.identity.owner_name}.")
        return " ".join(x for x in parts if x).strip()
    if section == "hours":
        lines = [f"{dh.day.capitalize()}: {_fmt_intervals(dh.intervals)}" for dh in p.hours.weekly]
        lines += [
            f"{e.date.isoformat()}: {'closed' if e.closed else _fmt_intervals(e.intervals)}"
            + (f" ({e.note})" if e.note else "")
            for e in p.hours.exceptions
        ]
        return "Opening hours — " + "; ".join(lines)
    if section == "delivery":
        d = p.delivery
        zones = []
        for z in d.zones:
            free = f", free above {format_inr(z.free_above_paise)}" if z.free_above_paise else ""
            areas = ", ".join(z.areas[:12])
            zones.append(f"{z.label}: fee {format_inr(z.fee_paise)}{free}; areas {areas}; ETA {z.eta}".strip())
        pickup = f" Pickup available at {d.pickup_address}." if d.pickup_available else " No pickup."
        return f"Delivery needs {d.notice_hours} h notice. " + " | ".join(zones) + pickup
    if section == "policies":
        pol = p.policies
        return " ".join(
            f"{label}: {value}"
            for label, value in (
                ("Returns", pol.returns),
                ("Cancellation", pol.cancellation),
                ("Custom orders", pol.custom_orders),
            )
            if value
        )
    if section == "payments":
        names = {"upi_link": "UPI (payment link)", "card_link": "card (payment link)", "cod": "cash on delivery"}
        return "Payment methods: " + ", ".join(names.get(m, m) for m in p.payments.methods)
    if section == "contact":
        c = p.contact
        return " ".join(
            f"{label}: {value}"
            for label, value in (("Phone", c.phone), ("Address", c.address), ("Maps", c.maps_url), ("Web", c.website))
            if value
        )
    raise KeyError(section)


def render(lp: LoadedProfile) -> RenderedProfile:
    p = lp.profile
    persona = p.persona
    body = "\n".join(
        [
            f"# Business: {p.identity.name}",
            section_text(lp, "about"),
            f"Persona: speak as {persona.name or p.identity.name}; tone {persona.tone}; formality {persona.formality};"
            f" emoji {persona.emoji}; languages {', '.join(persona.languages)}.",
            *(f"## {name.capitalize()}\n{section_text(lp, name)}" for name in SECTIONS if name != "about"),
        ]
    )
    return RenderedProfile(business_id=lp.business_id, version=lp.version, text=body)


async def profile_block(business_id: UUID) -> RenderedProfile:
    """Prompt block, cached per (business, version) in Redis with an in-process fallback."""
    lp = await load(business_id)
    key = (business_id, lp.version)
    if key in _local:
        return _local[key]
    redis_key = f"cache:profile:{business_id}:{lp.version}"
    try:
        cached = await get_redis().call("get", redis_key)
        if cached:
            rendered = RenderedProfile.model_validate_json(cached)
            _local[key] = rendered
            return rendered
    except RedisUnavailable:
        pass
    rendered = render(lp)
    for old in [k for k in _local if k[0] == business_id]:
        del _local[old]  # keep only the current version per business
    _local[key] = rendered
    try:
        await get_redis().call("set", redis_key, rendered.model_dump_json(), ex=_CACHE_TTL_S)
    except RedisUnavailable:
        pass
    return rendered


def profile_json(lp: LoadedProfile) -> str:
    return json.dumps(lp.profile.model_dump(mode="json"), ensure_ascii=False)
