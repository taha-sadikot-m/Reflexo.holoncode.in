"""Hydrate the records an `AgentOutput` references, for the output gate (POC_04 §5 check 1).

Everything is re-read under RLS at gate time: an id the model copied from another tenant, invented, or that was
archived a second ago simply does not resolve.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse
from uuid import UUID

import reflexo.knowledge.api as knowledge
from reflexo.agent.output_gate import extract_amounts
from reflexo.agent.schemas import AgentOutput, GroundingRecords
from reflexo.common.blocks import CarouselBlock, ProductBlock

_DISCOUNT_HINT = re.compile(r"%|discount|off\b|chhoot|छूट", re.IGNORECASE)
_NUMBERS = re.compile(r"\d+")
PROFILE_SECTIONS = {"about", "hours", "delivery", "policies", "payments", "contact", "profile"}


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def hydrate(
    output: AgentOutput,
    business_id: UUID,
    *,
    customer_id: UUID | None,
    customer_phone: str | None,
    business_phone: str | None = None,
    wa_display_number: str | None = None,
) -> GroundingRecords:
    rec = GroundingRecords(business_id=business_id)
    refs = [(r.type, r.id) for r in output.referenced_records]

    # ---- catalogue: offerings, variants, and offerings shown in product/carousel blocks
    block_ids: set[UUID] = set()
    for b in output.blocks:
        if isinstance(b, ProductBlock):
            block_ids.add(b.offering_id)
        elif isinstance(b, CarouselBlock):
            block_ids |= {i.offering_id for i in b.items}
    offering_refs = {u for t, i in refs if t == "offering" and (u := _uuid(i))}
    variant_refs = {u for t, i in refs if t == "variant" and (u := _uuid(i))}
    cards = await knowledge.cards_for(business_id, list(offering_refs | block_ids), list(variant_refs))
    card_ids = {c.offering_id for c in cards}
    variant_ids = {v.variant_id for c in cards for v in c.variants if v.variant_id}
    rec.block_offering_ids = {str(c.offering_id) for c in cards}
    for c in cards:
        # offerings shown as product blocks count as referenced: their prices are rendered from the rows
        if c.offering_id in offering_refs or c.offering_id in block_ids or variant_ids & variant_refs:
            for v in c.variants:
                if v.price_paise is not None:
                    rec.amounts_paise.add(v.price_paise)
                rec.quantity_numbers |= {int(n) for n in _NUMBERS.findall(v.name)}
            extra = c.attributes.get("eggless_extra_paise")
            if isinstance(extra, int) and extra > 0:
                rec.amounts_paise.add(extra)
            rec.quantity_numbers |= {int(n) for n in _NUMBERS.findall(c.name)}
            if c.price_on_request and (c.offering_id in offering_refs or c.offering_id in block_ids):
                rec.price_on_request = True

    # ---- Q&A
    qa_refs = {u for t, i in refs if t == "qa" and (u := _uuid(i))}
    qas = await knowledge.get_qa(business_id, list(qa_refs), customer_id) if qa_refs else []
    qa_ids = {q.entity_id for q in qas}
    for q in qas:
        rec.amounts_paise |= {a.paise for a in extract_amounts(q.text)}
        if _DISCOUNT_HINT.search(q.text):
            rec.discount_texts.append(q.text)

    # ---- profile: delivery zones, sections, contacts and own domains (always allowed)
    profile = await knowledge.get_profile(business_id)
    zone_ids = {z.id for z in profile.delivery.zones}
    if any(t in ("delivery_zone", "profile") for t, _ in refs) or output.topic == "delivery_shipping":
        for z in profile.delivery.zones:
            rec.amounts_paise.add(z.fee_paise)
            if z.free_above_paise:
                rec.amounts_paise.add(z.free_above_paise)
        rec.quantity_numbers.add(profile.delivery.notice_hours)
    rec.allowed_hosts.add("wa.me")
    if profile.contact.website:
        rec.allowed_hosts.add(
            (
                urlparse(
                    profile.contact.website
                    if "://" in profile.contact.website
                    else f"https://{profile.contact.website}"
                ).hostname
                or ""
            ).removeprefix("www.")
        )
    if profile.contact.maps_url:
        rec.allowed_hosts.add((urlparse(profile.contact.maps_url).hostname or "").removeprefix("www."))
    for phone in (profile.contact.phone, business_phone, wa_display_number, customer_phone):
        if phone:
            rec.allowed_phones.add(re.sub(r"\D", "", phone))
    email = getattr(profile.contact, "email", None)
    if isinstance(email, str) and email:
        rec.allowed_emails.add(email.lower())

    # ---- validity of every reference
    for t, i in refs:
        u = _uuid(i)
        ok = (
            (t == "offering" and u in card_ids)
            or (t == "variant" and u in variant_ids)
            or (t == "qa" and u in qa_ids)
            or (t == "delivery_zone" and i in zone_ids)
            or (t == "profile" and i in PROFILE_SECTIONS)
        )  # order/payment_link: resolved by commerce (WP-09); unknown until then
        if ok:
            rec.valid_refs.add((t, i))
        else:
            rec.missing_refs.append((t, i))
    rec.allowed_hosts.discard("")
    return rec
