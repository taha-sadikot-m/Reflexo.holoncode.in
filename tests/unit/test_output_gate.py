"""Output gate (POC_04 §5; harvest H12): money extraction table (≥ 40 cases) and every gate check."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

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
        ("Kothrud 411038 mein. Iska total ₹1,100 hai", [1100]),  # live: the PIN code was read as a price
        ("Pune 411 038, total ₹1,100", [1100]),
        ("total ₹411038", [411038]),  # with a currency marker it is an amount and still checked
        ("Aapka order MB-1012 draft ho gaya hai. Total ₹1,100", [1100]),  # live: order number read as a price
        ("Total Rs500 only", [500]),
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


def test_a_tool_answer_grounds_a_fact_without_a_record() -> None:
    """order_status found no order: "no order yet" is the tool's answer, there is nothing to cite."""
    none_yet = out("Aapka abhi koi order nahi hai.", topic="order_status", referenced_records=[])
    assert failed(check(none_yet, records(valid_refs=set()))) == ["grounding"]
    assert check(none_yet, records(valid_refs=set(), tool_backed=True)).passed
    # numbers are still checked on their own
    priced = out("Aapka order ₹999 ka hai.", topic="order_status", referenced_records=[])
    assert failed(check(priced, records(valid_refs=set(), tool_backed=True))) == ["amounts"]


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
    assert refund.passed and refund.rewritten_action == "handoff"  # an unbacked refund/cancel claim → a human
    backed = check(
        out("Aapka order cancel ho gaya", topic="returns_refunds_cancellation"), records(capability_decided=True)
    )
    assert backed.passed and backed.rewritten_action is None  # an action tool decided it by the owner's rules
    # stating the shop's own policy is information, not a decision
    policy = check(
        out("24 ghante pehle tak cancellation free hai", topic="returns_refunds_cancellation"),
        records(valid_refs={("profile", "policies")}),
    )
    assert policy.passed and policy.rewritten_action is None
    # ...but an outcome for an order still needs a capability
    order_claim = check(
        out("Aapka order cancel ho gaya", topic="returns_refunds_cancellation"),
        records(valid_refs={("profile", "policies"), ("order", "o1")}),
    )
    assert order_claim.rewritten_action == "handoff"
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


@pytest.mark.parametrize(
    "text",
    [
        "The earliest we can deliver your eggless 1 kg Chocolate Truffle Cake is Mon 12 Oct.",  # live false positive
        "We need no less than 24 hours notice.",
        "Orders placed less than 24 hours ahead can't be delivered.",
        "I can offer 2 options: 500 g or 1 kg.",
        "We're off on 15 Oct, closed for Diwali.",
        "Order MB-1001 is confirmed.",
    ],
)
def test_ordinary_sentences_are_not_discounts(text: str) -> None:
    assert "discounts" not in failed(check(out(text, topic="order_status"), records()))


@pytest.mark.parametrize(
    "text", ["₹100 less de dete hain", "50 rupees kam kar denge", "Flat ₹200 off today", "discount of 15"]
)
def test_discount_phrasings_are_caught(text: str) -> None:
    assert "discounts" in failed(check(out(text), records(amounts_paise={10000, 5000, 20000, 1500})))


def test_numbers_the_system_showed_are_not_new_prices() -> None:
    text = "Order 1012 ka total ₹950 hai"  # the order number sits next to "ka"
    assert check(out(text), records(known_numbers={"1012"})).passed
    assert failed(check(out(text), records())) == ["amounts"]


def test_amount_from_a_tool_result_is_backed_without_a_reference() -> None:
    # live: order_confirm declined with "COD only for orders up to ₹1,500"; the reply quoted it and was refused
    text = "COD sirf ₹1,500 tak ke orders pe hai, aapka total ₹1,800 hai. Payment link bhej doon?"
    assert failed(check(out(text, topic="payments"), records())) == ["amounts"]
    assert check(out(text, topic="payments"), records(amounts_paise={150000, 180000})).passed


def test_buttons_without_text_take_the_text_block_before_them() -> None:
    # live: [text, buttons(no text)] was rejected twice and the customer got a fallback
    options = [{"id": "yes", "title": "Yes"}, {"id": "change", "title": "Change"}]
    merged = out(
        "x", blocks=[{"type": "text", "text": "Total ₹950. Confirm?"}, {"type": "buttons", "options": options}]
    )
    assert [b.type for b in merged.blocks] == ["buttons"] and merged.blocks[0].text == "Total ₹950. Confirm?"
    with pytest.raises(ValueError):  # nothing to take: still invalid
        out("x", blocks=[{"type": "buttons", "options": options}])


def test_reply_without_action_takes_it_from_its_content() -> None:
    """Live: a correct price answer was refused twice for leaving out `action`. The content says it."""
    from reflexo.agent.schemas import AgentOutput

    base = {"topic": "pricing", "language": "hi-Latn"}
    sent = AgentOutput.model_validate({**base, "blocks": [{"type": "text", "text": "Gulab Jamun 1 kg ₹400"}]})
    assert sent.action == "send"
    asked = AgentOutput.model_validate(
        {
            **base,
            "blocks": [{"type": "text", "text": "Ek minute, main check karti hoon"}],
            "owner_question": {"question": "Can we do eggless laddoo?", "options": []},
        }
    )
    assert asked.action == "ask_owner"
    with pytest.raises(ValidationError):  # nothing to go on: still invalid
        AgentOutput.model_validate(base)
    assert AgentOutput.model_validate({**base, "action": "handoff"}).action == "handoff"  # explicit wins
