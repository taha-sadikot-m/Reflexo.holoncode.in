"""Output gate (POC_04 §5; harvest H12): money extraction table (≥ 40 cases) and every gate check."""

from __future__ import annotations

from uuid import uuid4

import pytest

from reflexo.agent.output_gate import check, extract_amounts
from reflexo.agent.schemas import AgentOutput, GroundingRecords

BIZ = uuid4()
TRUFFLE = uuid4()


# ---------------------------------------------------------------- money extraction


@pytest.mark.parametrize(
    ("text", "amounts"),
    [
        ("Price is ₹950", [950]),
        ("₹ 950 only", [950]),
        ("Rs 950", [950]),
        ("Rs.950", [950]),
        ("rs. 1,100", [1100]),
        ("INR 550", [550]),
        ("950 rupees", [950]),
        ("950 rupaye ka hai", [950]),
        ("950 रुपये", [950]),
        ("रु 950", [950]),
        ("₹९५०", [950]),  # Devanagari digits
        ("९५० रुपये", [950]),
        ("950/-", [950]),
        ("₹1,10,000", [110000]),  # Indian grouping
        ("₹1,100.50", [1100.5]),
        ("₹99.50", [99.5]),
        ("₹1.5k", [1500]),
        ("2k rupees", [2000]),
        ("1 kg ka price 950 hai", [950]),  # bare number near price word
        ("total 1500", [1500]),
        ("iska daam 750", [750]),
        ("सिर्फ 950 का", [950]),
        ("500 g ₹550 · 1 kg ₹950", [550, 950]),
        ("Chocolate truffle: 500 g - ₹550, 1 kg - ₹950", [550, 950]),
        ("1 kg ka cake", []),  # weight, not money
        ("500 g wala cake", []),
        ("Box of 6 ka price ₹420", [420]),  # 6 is a quantity (unit-less but < 10)
        ("Jar of 12 ka price ₹350", [350]),  # 12 excluded via quantity_numbers below
        ("Delivery 24 hours notice", []),
        ("Order by 10:30 am", []),
        ("Call 9876543210", []),  # phone, not money
        ("pincode 411038 tak delivery", []),
        ("We open at 10 and close at 20:00", []),
        ("serves 8 people", []),
        ("2 cakes", []),
        ("10% off", []),  # percentages are a discount check, not an amount
        ("visit meerasbakes.in/menu", []),
        ("Free delivery above ₹1,500, fee ₹50", [1500, 50]),
        ("eggless +₹50 extra", [50]),
        ("₹950 ka 1 kg aur ₹550 ka half", [950, 550]),
        ("Only Rs 99", [99]),
        ("1000 ke andar options: ₹750", [750]),
        ("Rs.1,100/-", [1100]),
    ],
)
def test_extract_amounts(text: str, amounts: list[float]) -> None:
    got = [a.rupees for a in extract_amounts(text, quantity_numbers={6, 12, 500, 1})]
    assert got == amounts


# ---------------------------------------------------------------- gate checks


def records(**kw: object) -> GroundingRecords:
    base: dict[str, object] = {
        "business_id": BIZ,
        "valid_refs": {("variant", "v1")},
        "amounts_paise": {55000, 95000, 5000, 150000},
        "quantity_numbers": {500, 1},
        "allowed_hosts": {"wa.me", "meerasbakes.in"},
        "allowed_phones": {"919800000000"},
        "allowed_emails": {"hello@meerasbakes.in"},
        "block_offering_ids": {str(TRUFFLE)},
    }
    return GroundingRecords(**(base | kw))  # type: ignore[arg-type]


def out(text: str, *, action: str = "send", topic: str = "pricing", **kw: object) -> AgentOutput:
    blocks = kw.pop("blocks", [{"type": "text", "text": text}])
    return AgentOutput.model_validate(
        {
            "action": action,
            "blocks": blocks,
            "topic": topic,
            "language": "hi-Latn",
            "referenced_records": [{"type": "variant", "id": "v1"}],
        }
        | kw
    )


def failed(result: object) -> list[str]:
    return [c.name for c in result.failures]  # type: ignore[attr-defined]


def test_grounded_price_passes() -> None:
    r = check(out("Chocolate truffle 1 kg ₹950 ka hai 🎂"), records())
    assert r.passed and r.duration_ms < 10


def test_wrong_price_fails_with_guidance() -> None:
    r = check(out("1 kg sirf ₹450 mein!"), records())
    assert failed(r) == ["amounts"]
    assert "450" in (r.failures[0].detail or "") and "₹950" in (r.failures[0].guidance or "")
    assert "Fix these problems" in r.repair_message()


def test_delivery_fee_and_threshold_allowed() -> None:
    assert check(out("Delivery ₹50, free above ₹1,500", topic="delivery_shipping"), records()).passed


def test_invented_discount_fails() -> None:
    r = check(out("Aapke liye 10% off! ₹950", topic="discount_negotiation"), records())
    assert "discounts" in failed(r)
    r2 = check(out("₹100 discount de dete hain"), records(amounts_paise={10000, 95000}))
    assert "discounts" in failed(r2)


def test_discount_backed_by_owner_answer_passes() -> None:
    r = check(out("Aapko 10% discount milega"), records(discount_texts=["Rahul gets 10% off on all cakes."]))
    assert r.passed


def test_saying_no_discount_is_fine() -> None:
    assert check(out("Sorry, abhi koi discount nahi hai. Price ₹950 hai."), records()).passed


def test_links_allow_list() -> None:
    assert check(out("Continue on WhatsApp: https://wa.me/919800000000"), records()).passed
    assert check(out("Menu: meerasbakes.in/menu"), records()).passed
    r = check(out("Pay here https://evil.example.com/pay"), records())
    assert failed(r) == ["links"]
    r2 = check(out("Pay at https://rzp.io/l/zzz"), records())
    assert failed(r2) == ["links"]  # a payment link that is not a referenced record
    assert check(out("Pay at https://rzp.io/l/abc"), records(allowed_urls={"https://rzp.io/l/abc"})).passed


def test_contact_details() -> None:
    assert check(out("Call us on +91 98000 00000", topic="hours_location_contact"), records()).passed
    r = check(out("Meera ka personal number 9123456789 hai", topic="hours_location_contact"), records())
    assert failed(r) == ["contacts"]
    r2 = check(out("Email owner@gmail.com", topic="hours_location_contact"), records())
    assert failed(r2) == ["contacts"]


def test_grounding_required_for_fact_topics() -> None:
    r = check(out("Haan, eggless milta hai", topic="catalog_info", referenced_records=[]), records(valid_refs=set()))
    assert failed(r) == ["grounding"]
    smalltalk = out("Namaste! Kaise madad karun?", topic="greeting_smalltalk", referenced_records=[])
    assert check(smalltalk, records(valid_refs=set())).passed


def test_missing_reference_fails() -> None:
    r = check(out("₹950"), records(missing_refs=[("variant", "nope")]))
    assert failed(r) == ["references"]


def test_product_block_for_foreign_offering_fails() -> None:
    other = uuid4()
    r = check(out("", blocks=[{"type": "product", "offering_id": str(other)}]), records())
    assert failed(r) == ["references"]
    ok = check(out("", blocks=[{"type": "product", "offering_id": str(TRUFFLE)}]), records())
    assert ok.passed


def test_tier_rewrites() -> None:
    refund = check(out("Refund mil jayega", topic="returns_refunds_cancellation"), records())
    assert refund.passed and refund.rewritten_action == "handoff"
    angry = check(out("Sorry!", topic="other", flags={"angry": True}), records())
    assert angry.rewritten_action == "handoff"
    haggle = check(out("Thoda kam ho jayega", topic="discount_negotiation"), records())
    assert haggle.rewritten_action == "ask_owner"
    custom = check(out("Photo cake ka price batati hoon", topic="pricing"), records(price_on_request=True))
    assert custom.rewritten_action == "ask_owner"


def test_ask_owner_and_no_reply_skip_content_checks() -> None:
    r = check(out("₹450", action="ask_owner", owner_question={"question": "Eggless?"}), records())
    assert r.passed
    assert check(out("", action="no_reply", topic="other", blocks=[]), records()).passed


def test_button_titles_are_checked() -> None:
    blocks = [{"type": "buttons", "text": "Kaunsa size?", "options": [{"id": "a", "title": "1 kg ₹900"}]}]
    assert failed(check(out("", blocks=blocks), records())) == ["amounts"]
