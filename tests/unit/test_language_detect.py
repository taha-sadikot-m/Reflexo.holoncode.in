"""Customer language detection (agent.language): evidence both ways; an unclear message keeps the established one."""

from __future__ import annotations

import pytest

from reflexo.agent.language import detect


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mera order cancel kar do please, plans change ho gaye", "hi-Latn"),  # mixed, Hindi-led
        ("Eggless cakes milte hain? Price kya hai?", "hi-Latn"),
        ("1 kg chocolate truffle cake chahiye kal shaam 6 baje, Kothrud delivery", "hi-Latn"),
        ("Do you deliver to Baner? What is the delivery charge?", "en"),
        ("Can I get it by tomorrow evening please", "en"),
        ("मुझे केक चाहिए", "hi"),
    ],
)
def test_clear_messages(text: str, expected: str) -> None:
    assert detect(text, default="en" if expected == "hi-Latn" else "hi-Latn") == expected


@pytest.mark.parametrize("text", ["ok", "MB-1042", "Kothrud 411038", "chocolate truffle 1 kg", "👍"])
def test_unclear_messages_keep_the_established_language(text: str) -> None:
    assert detect(text, default="hi-Latn") == "hi-Latn"
    assert detect(text, default="en") == "en"


def test_english_policy_answers_everyone_in_english(monkeypatch: pytest.MonkeyPatch) -> None:
    from reflexo.agent.language import language_rule, reply_language
    from reflexo.common.config import get_settings
    from reflexo.common.language import default_language, fixed

    monkeypatch.setattr(get_settings(), "language_policy", "en")
    assert reply_language("Mera order cancel kar do", "hi-Latn") == "en"
    assert reply_language("मुझे केक चाहिए", "hi") == "en"
    assert "English" in language_rule("customer") and "English" in language_rule("caller")
    assert fixed("Hello", "Namaste") == "Hello" and default_language("hi-Latn") == "en"

    monkeypatch.setattr(get_settings(), "language_policy", "auto")  # mirror mode is still there
    assert reply_language("Mera order cancel kar do", "en") == "hi-Latn"
    assert fixed("Hello", "Namaste") == "Namaste" and default_language("hi-Latn") == "hi-Latn"
