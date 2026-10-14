"""The platform's language policy for fixed system text (REFLEXO_LANGUAGE_POLICY): English only ("en", the
default) or mirror each person ("auto", Hinglish for messages whose reader's language is not known).

Detecting a person's language and the prompts' language rules live in `agent.language`.
"""

from __future__ import annotations

from reflexo.common.config import get_settings


def fixed(english: str, hinglish: str) -> str:
    """A fixed system message (welcome, onboarding questions, fail-safes) in the platform's language: English, or
    under policy "auto" the Hinglish original (the person's language is not known for these)."""
    return hinglish if get_settings().language_policy == "auto" else english


def default_language(configured: str | None = None) -> str:
    """A new business's or customer's starting language under the policy."""
    return (configured or "hi-Latn") if get_settings().language_policy == "auto" else "en"
