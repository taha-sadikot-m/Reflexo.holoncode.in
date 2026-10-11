"""Money in integer paise; Indian formatting (₹1,10,000) (POC_01 §6)."""

from __future__ import annotations


def group_indian(rupees: int) -> str:
    """Group digits the Indian way: 1,10,000 / 12,34,567."""
    s = str(abs(rupees))
    if len(s) <= 3:
        grouped = s
    else:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])
    return ("-" if rupees < 0 else "") + grouped


def format_inr(paise: int) -> str:
    """₹950 / ₹1,499 / ₹1,10,000 / ₹99.50 (paise shown only when non-zero)."""
    sign = "-" if paise < 0 else ""
    rupees, rem = divmod(abs(paise), 100)
    text = f"₹{group_indian(rupees)}"
    if rem:
        text += f".{rem:02d}"
    return sign + text


def rupees_to_paise(rupees: int | float) -> int:
    return round(rupees * 100)
