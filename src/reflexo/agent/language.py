"""Language of a customer message: English, Hindi (Devanagari) or Hinglish (Roman Hindi). Deterministic.

Roman script is shared by English and Hinglish, and customers mix them freely ("mera order cancel kar do please"),
so a message is weighed both ways: Hindi function words (pronouns, postpositions, auxiliaries, common verb forms)
against English ones. Nouns carry no signal — "order", "cake", "delivery" are the same in both. When neither side
clearly wins, the message does not change the language: the customer's established language (`default`) stands,
so one short or mixed message never flips a Hinglish conversation to English.
"""

from __future__ import annotations

import re

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
