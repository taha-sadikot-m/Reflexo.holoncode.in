"""Language of a customer message: English, Hindi (Devanagari) or Hinglish (Roman Hindi). Deterministic.

Roman script is shared by English and Hinglish, and customers mix them freely ("mera order cancel kar do please"),
so a message is weighed both ways: Hindi function words (pronouns, postpositions, auxiliaries, common verb forms)
against English ones. Nouns carry no signal — "order", "cake", "delivery" are the same in both. When neither side
clearly wins, the message does not change the language: the customer's established language (`default`) stands,
so one short or mixed message never flips a Hinglish conversation to English.
"""

from __future__ import annotations

import re

from reflexo.common.language import default_language as default_language
from reflexo.common.language import fixed as fixed

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Hindi words in Roman script that are not also English words ("do", "to", "me", "us", "main" are left out)
_HINDI = frozenset(
    """
    hai hain ho hoga hogi honge hua hui hue tha thi raha rahi rahe rha rhi
    kya kaise kitna kitne kitni kab kahan kaun kyun kyu konsa kaunsa
    ka ki ke ko mein mai se par pe tak liye wala wali wale
    mera meri mere mujhe muje tera teri tere apna apni apne aap aapka aapki aapke hum hamara hamari humara humko
    tum tumhara yeh ye woh wo vo isko usko inko unko iska uska unka inka
    nahi nahin haan ji jee bhai didi bhaiya accha acha achha theek thik sahi
    chahiye chahte chahta chahti chaiye
    kar karo karna karke karunga karungi karenge kariye kijiye karwa karwana kiya kiye
    dena dijiye diya diye dedo lena lijiye liya
    gaya gayi gaye jayega jayegi jaye jaana jana aana aaya aayi aaye
    bata batao bataiye batana bhejo bhejna bhejiye milega milegi milenge sakte sakta sakti
    abhi kal aaj parso bahut thoda zyada jyada sab koi kuch kuchh aur bhi toh agar lekin phir fir
    """.split()
)
_ENGLISH = frozenset(
    """
    the a an is are was were be been am will would can could should shall may might must
    i you he she we they it my your our their me him her them this that these those
    what when where which who why how please thanks thank want wanted need have has had
    for of with from at by about into than then there here and but or not no yes
    any some much many more most very just only also get got send sent give tell know
    """.split()
)
_MARGIN = 1  # one function word more on one side is enough to call it


def reply_language(text: str | None, default: str = "hi-Latn") -> str:
    """The language to answer `text` in: the platform's fixed language, or (policy "auto") the one detected."""
    from reflexo.common.config import get_settings

    policy = get_settings().language_policy
    return policy if policy != "auto" else detect(text, default)


_RULE_MIRROR = {
    "customer": "Reply in the language AND script of the customer's LATEST message (see \"language of the latest "
    'message"), not of earlier messages, your own earlier replies or the examples below: English → English only (no '
    "Hindi words); Hindi in Devanagari → Devanagari; Hinglish (Hindi in Roman letters) → Hinglish. Set `language` to "
    "what you actually wrote (en / hi / hi-Latn).",
    "owner": "Reply in the owner's language and script (English, Hindi, or Hinglish in Roman letters), matching "
    "their latest message.",
    "caller": "Mirror the caller's language: Hinglish → Hinglish (Roman Hindi), Hindi → Hindi, English → English.",
}
_RULE_ENGLISH = {
    "customer": "Always reply in clear, simple English, even when the customer writes in Hindi or Hinglish (you "
    "understand them; you answer in English). No Hindi words. Set `language` to en.",
    "owner": "Always reply in clear, simple English, even when the owner writes in Hindi or Hinglish. No Hindi words.",
    "caller": "Always speak simple, clear English, even if the caller speaks Hindi or Hinglish. No Hindi words.",
}


# house style for everything the AI writes or says (outgoing text is also normalised: gateway.plain_punctuation)
_STYLE = " Never use the long em-dash; write a comma, colon or full stop instead."


def language_rule(audience: str) -> str:
    """The prompt's LANGUAGE rule for `audience` (customer | owner | caller) under the platform's policy, with the
    house writing style."""
    from reflexo.common.config import get_settings

    return (_RULE_MIRROR if get_settings().language_policy == "auto" else _RULE_ENGLISH)[audience] + _STYLE


def detect(text: str | None, default: str = "hi-Latn") -> str:
    if not text or not text.strip():
        return default
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if _DEVANAGARI.match(c)) / len(letters) > 0.3:
        return "hi"
    words = re.findall(r"[a-zA-Z]+", text.lower())
    hindi = sum(1 for w in words if w in _HINDI)
    english = sum(1 for w in words if w in _ENGLISH)
    if hindi >= english + _MARGIN:
        return "hi-Latn"
    if english >= hindi + _MARGIN:
        return "en"
    return default  # no clear signal: the established language stands
