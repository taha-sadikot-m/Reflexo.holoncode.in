"""Owner-side lookups (POC_04 §3.3): resolve "truffle 1kg" to an offering/variant, including hidden items, and
patch business settings (COD rule) with the previous value kept for the read-back.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text

from reflexo.common.db import get_db
from reflexo.knowledge.query import canonical_size

_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|gm|gms|grams?|kilo|pcs?|pieces?|ml|l|litre)\b", re.IGNORECASE)


@dataclass
class VariantRef:
    variant_id: UUID
    name: str
    price_paise: int | None


@dataclass
class OfferingRef:
    offering_id: UUID
    name: str
    status: str
    base_price_paise: int | None
    score: float
    variants: list[VariantRef] = field(default_factory=list)

    def pick_variant(self, target: str) -> VariantRef | None:
        """The variant named in `target` ("1 kg"), or the only one."""
        if len(self.variants) == 1:
            return self.variants[0]
        size = _SIZE.search(target)
        if size:
            wanted = canonical_size(size.group(0))
            for v in self.variants:
                if canonical_size(v.name) == wanted:
                    return v
        lowered = target.lower()
        named = [v for v in self.variants if v.name.lower() in lowered]
        return named[0] if len(named) == 1 else None


async def resolve_offerings(business_id: UUID, target: str, limit: int = 5) -> list[OfferingRef]:
    """Active and hidden offerings ranked by name/alias similarity to `target` (sizes stripped)."""
    name_part = _SIZE.sub(" ", target).strip() or target
    async with get_db().tenant_session(business_id) as s:
        rows = (
            await s.execute(
                text("""SELECT o.id, o.name, o.status, o.base_price_paise,
                               GREATEST(word_similarity(lower(:t), lower(o.name)),
                                        COALESCE((SELECT max(word_similarity(lower(:t), lower(a)))
                                                  FROM unnest(o.aliases) a), 0)) AS score
                        FROM offerings o WHERE o.status IN ('active', 'hidden')
                        ORDER BY score DESC, o.name LIMIT :n"""),
                {"t": name_part, "n": limit},
            )
        ).all()
        out = []
        for r in rows:
            if r.score < 0.3:
                continue
            variants = (
                await s.execute(
                    text("""SELECT id, name, price_paise FROM offering_variants
                            WHERE offering_id = :o AND status = 'active' ORDER BY price_paise NULLS LAST, name"""),
                    {"o": r.id},
                )
            ).all()
            out.append(
                OfferingRef(
                    r.id,
                    r.name,
                    str(r.status),
                    r.base_price_paise,
                    float(r.score),
                    [VariantRef(v.id, v.name, v.price_paise) for v in variants],
                )
            )
    return out


async def patch_settings(business_id: UUID, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge top-level keys into `businesses.settings`; returns the previous values of those keys."""
    async with get_db().tenant_session(business_id) as s:
        current = (
            await s.execute(text("SELECT settings FROM businesses WHERE id = :b FOR UPDATE"), {"b": business_id})
        ).scalar_one()
        before = {k: (current or {}).get(k) for k in patch}
        await s.execute(
            text("UPDATE businesses SET settings = settings || CAST(:p AS jsonb) WHERE id = :b"),
            {"p": json.dumps(patch, ensure_ascii=False), "b": business_id},
        )
    return before
