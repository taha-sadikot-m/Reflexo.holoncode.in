"""CEL for automations (POC_06 §1.4): a restricted environment with shop functions, `{{ }}` templates, and
sample variables per trigger type so the validator can type-check every expression before activation.

No Python eval anywhere: expressions run in cel-python's interpreter over plain data.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any, cast
from zoneinfo import ZoneInfo

import celpy
from celpy import celtypes
from celpy.adapter import json_to_cel
from celpy.evaluation import CELEvalError

MAX_EXPR_CHARS = 500
_TEMPLATE = re.compile(r"\{\{\s*(.+?)\s*\}\}")
_env = celpy.Environment()


class CelError(ValueError):
    pass


def _s(v: Any) -> str:
    return str(v)


def _to_dt(v: Any, tz: ZoneInfo) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=tz)
    text_ = str(v)
    if len(text_) == 10:  # YYYY-MM-DD
        return datetime.combine(date.fromisoformat(text_), time(0), tzinfo=tz)
    return datetime.fromisoformat(text_.replace("Z", "+00:00"))


def functions(tz_name: str = "Asia/Kolkata", now: datetime | None = None) -> dict[str, Any]:
    tz = ZoneInfo(tz_name)
    current = now or datetime.now(UTC)

    def contains_any(text_: Any, words: Any) -> celtypes.BoolType:
        low = _s(text_).lower()
        return celtypes.BoolType(any(re.search(rf"(?<!\w){re.escape(_s(w).lower())}(?!\w)", low) for w in words))

    def lower(s: Any) -> celtypes.StringType:
        return celtypes.StringType(_s(s).lower())

    def money(paise: Any) -> celtypes.StringType:
        rupees = int(paise) // 100
        s = str(rupees)
        if len(s) > 3:  # Indian grouping: 1,10,000
            head, tail = s[:-3], s[-3:]
            head = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head))
            s = f"{head},{tail}"
        return celtypes.StringType(f"₹{s}")

    def date_fmt(v: Any, fmt: Any) -> celtypes.StringType:
        """Small subset of CLDR patterns: d, dd, MMM, yyyy, HH, mm ("d MMM" → "15 Oct")."""
        dt = _to_dt(v, tz).astimezone(tz)
        tokens = {
            "yyyy": f"{dt.year}",
            "MMM": dt.strftime("%b"),
            "dd": f"{dt.day:02d}",
            "d": f"{dt.day}",
            "HH": f"{dt.hour:02d}",
            "mm": f"{dt.minute:02d}",
        }
        out = re.sub(r"yyyy|MMM|dd|d|HH|mm", lambda m: tokens[m.group(0)], _s(fmt))
        return celtypes.StringType(out)

    def hours_since(v: Any) -> celtypes.DoubleType:
        return celtypes.DoubleType((current - _to_dt(v, tz)).total_seconds() / 3600)

    def at(v: Any, hhmm: Any) -> celtypes.TimestampType:
        """`order.delivery_date.at("10:00")`: that local time on that date, in the business timezone."""
        hour, minute = (int(x) for x in _s(hhmm).split(":"))
        local = datetime.combine(_to_dt(v, tz).astimezone(tz).date(), time(hour, minute), tzinfo=tz)
        return celtypes.TimestampType(local.astimezone(UTC))

    def has_category(order: Any, name: Any) -> celtypes.BoolType:
        cats = [_s(c).lower() for c in order.get(celtypes.StringType("categories"), [])]
        return celtypes.BoolType(_s(name).lower() in cats)

    def cake_count(order: Any) -> celtypes.IntType:
        return celtypes.IntType(int(order.get(celtypes.StringType("cake_count"), 0)))

    return {
        "contains_any": contains_any,
        "lower": lower,
        "money": money,
        "date_fmt": date_fmt,
        "hours_since": hours_since,
        "at": at,
        "has_category": has_category,
        "cake_count": cake_count,
    }


@lru_cache(maxsize=512)
def _compiled(expr: str) -> Any:
    if len(expr) > MAX_EXPR_CHARS:
        raise CelError(f"expression longer than {MAX_EXPR_CHARS} characters")
    try:
        return _env.compile(expr)
    except Exception as exc:
        raise CelError(f"cannot parse `{expr}`: {str(exc).splitlines()[0][:160]}") from exc


def _py(v: Any) -> Any:
    if isinstance(v, celtypes.BoolType):
        return bool(v)
    if isinstance(v, celtypes.IntType | celtypes.UintType):
        return int(v)
    if isinstance(v, celtypes.DoubleType):
        return float(v)
    if isinstance(v, celtypes.TimestampType):
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if isinstance(v, celtypes.DurationType):
        return timedelta(seconds=float(v.total_seconds()))
    if isinstance(v, celtypes.StringType):
        return str(v)
    if isinstance(v, celtypes.ListType):
        return [_py(x) for x in v]
    if isinstance(v, celtypes.MapType):
        return {str(k): _py(x) for k, x in v.items()}
    return v


def evaluate(expr: str, variables: dict[str, Any], *, tz: str = "Asia/Kolkata", now: datetime | None = None) -> Any:
    program = _env.program(_compiled(expr), functions=functions(tz, now))
    activation: dict[str, Any] = dict(cast(Any, json_to_cel({k: _jsonable(v) for k, v in variables.items()})))
    if now is not None:
        activation["now"] = celtypes.TimestampType(now)
    try:
        result = program.evaluate(cast(Any, activation))
    except Exception as exc:
        raise CelError(f"`{expr}`: {str(exc).splitlines()[0][:200]}") from exc
    if isinstance(result, CELEvalError):
        raise CelError(f"`{expr}`: {str(result)[:200]}")
    return _py(result)


def render(template: str, variables: dict[str, Any], *, tz: str = "Asia/Kolkata", now: datetime | None = None) -> str:
    def one(m: re.Match[str]) -> str:
        value = evaluate(m.group(1), variables, tz=tz, now=now)
        if isinstance(value, datetime):
            return value.astimezone(ZoneInfo(tz)).strftime("%d %b %H:%M")
        return str(value)

    return _TEMPLATE.sub(one, template)


def expressions_in(template: str) -> list[str]:
    return [m.group(1) for m in _TEMPLATE.finditer(template)]


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    return v


# ---------------------------------------------------------------- sample variables (type checking)

_ORDER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "number": "MB-1001",
    "items": [{"name": "Chocolate Truffle Cake 1 kg", "quantity": 1, "category": "cakes"}],
    "categories": ["cakes"],
    "cake_count": 1,
    "total_paise": 105000,
    "delivery_date": "2026-10-15",
    "delivery_slot": "4pm-8pm",
    "payment_method": "cod",
    "payment_status": "cod_pending",
    "status": "confirmed",
}
_CUSTOMER = {"id": "00000000-0000-0000-0000-000000000002", "name": "Rahul", "tags": [], "phone_masked": "+91•••••2233"}
_ORDER_EVENTS = {
    "order_created",
    "order_confirmed",
    "order_cancelled",
    "order_status_changed",
    "order_confirming",
    "payment_link_created",
    "payment_captured",
    "payment_failed",
}


def sample_variables(trigger_type: str) -> dict[str, Any]:
    """The variables a definition may use for this trigger (the validator evaluates every expression on them)."""
    event: dict[str, Any] = {
        "type": trigger_type,
        "customer_id": _CUSTOMER["id"],
        "channel": "whatsapp",
        "text": "haan confirm",
        "occurred_at": "2026-10-11T10:00:00+05:30",
        "payload": {
            "order_id": _ORDER["id"],
            "post_id": "post-1",
            "post_offering_id": _ORDER["id"],
            "to_status": "out_for_delivery",
        },
    }
    out: dict[str, Any] = {
        "event": event,
        "customer": _CUSTOMER,
        "business": {"name": "Meera's Bakes"},
        "vars": {},
        "steps": {},
    }
    if trigger_type in _ORDER_EVENTS:
        out["order"] = _ORDER
    return out
