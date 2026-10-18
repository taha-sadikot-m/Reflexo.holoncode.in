"""Is this the same name, written in another script? (BETA_13 §6.1: names appear as written, in the recipient's
script when the message is in another one.)

A name's consonant skeleton is compared across Latin and Devanagari: "Vikram Mehta" and "विक्रम मेहता" both read
v-k-r-m m-t. Vowels and the letters transliteration spells loosely (h for aspiration, y as a glide) are ignored;
the other consonants, which carry the name, must match in order, over the same span of words. Deterministic, and
only used for name facts: codes, numbers and read-backs are always compared exactly.
"""

from __future__ import annotations

import re
import unicodedata

# Devanagari consonants (and nukta forms) to Latin consonant classes; vowels and signs read as nothing.
_DEVANAGARI = {
    "क": "k", "ख": "k", "ग": "g", "घ": "g", "ङ": "n", "च": "c", "छ": "c", "ज": "j", "झ": "j", "ञ": "n",
    "ट": "t", "ठ": "t", "ड": "d", "ढ": "d", "ण": "n", "त": "t", "थ": "t", "द": "d", "ध": "d", "न": "n",
    "प": "p", "फ": "f", "ब": "b", "भ": "b", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "s",
    "ष": "s", "स": "s", "ह": "h", "क़": "k", "ख़": "k", "ग़": "g", "ज़": "j", "ड़": "d", "ढ़": "d", "फ़": "f",
    "ं": "n", "ँ": "n",
}  # fmt: skip
# Latin spellings folded to the same classes: aspirates lose their h, w reads v, z reads j, ch reads c ...
_LATIN_FOLDS = [("ngh", "n"), ("ksh", "ks"), ("ch", "c"), ("sh", "s"), ("ph", "f"), ("kh", "k"), ("gh", "g"),
                ("jh", "j"), ("th", "t"), ("dh", "d"), ("bh", "b"), ("w", "v"), ("z", "j"), ("q", "k"),
                ("x", "ks"), ("ck", "k")]  # fmt: skip
_IGNORED = set("aeiouhy")
_REPEATS = re.compile(r"(.)\1+")


def _squeeze(letters: list[str] | str) -> str:
    return _REPEATS.sub(r"\1", "".join(ch for ch in letters if ch not in _IGNORED))


def _latin_skeleton(word: str) -> str:
    w = unicodedata.normalize("NFKD", word.lower())
    w = "".join(ch for ch in w if "a" <= ch <= "z")
    for a, b in _LATIN_FOLDS:
        w = w.replace(a, b)
    return _squeeze(re.sub(r"ng$", "n", w))


def _devanagari_skeleton(word: str) -> str:
    w = unicodedata.normalize("NFC", word)
    out = []
    i = 0
    while i < len(w):
        if w[i : i + 2] in _DEVANAGARI:
            out.append(_DEVANAGARI[w[i : i + 2]])
            i += 2
            continue
        out.append(_DEVANAGARI.get(w[i], ""))
        i += 1
    return _squeeze(out)


def _devanagari(word: str) -> bool:
    return any("ऀ" <= ch <= "ॿ" for ch in word)


def skeleton(word: str) -> str:
    return _devanagari_skeleton(word) if _devanagari(word) else _latin_skeleton(word)


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÀ-ɏ]+|[ऀ-ॿ]+", text)


def appears(name: str, text: str) -> bool:
    """The name appears in the text as written, or in the other script with the same consonants in order."""
    if " ".join(name.split()).casefold() in " ".join(text.split()).casefold():
        return True
    name_words = _words(name)
    wanted = "".join(skeleton(w) for w in name_words)
    if len(wanted) < 2:  # too little to tell names apart
        return False
    script = _devanagari(" ".join(name_words))
    words = _words(text)
    # only words in the other script: within one script a name must match exactly (Ritu is not Rita)
    have = [skeleton(w) if _devanagari(w) != script else "" for w in words]
    span = len(name_words) + 1
    return any(
        all(have[i : i + n]) and "".join(have[i : i + n]) == wanted
        for n in range(1, span + 1)
        for i in range(len(have) - n + 1)
    )
