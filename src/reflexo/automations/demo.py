"""Demo definitions (POC_06 §9): worked examples for the builder prompt and the seeded demo automations."""

from __future__ import annotations

from typing import Any

DAILY_CAKE_LIMIT: dict[str, Any] = {
    "schema_version": 1, "name": "Daily cake limit", "kind": "guard", "mode": "observe",
    "trigger": {"type": "order_confirming", "cel": "order.has_category('cakes')"},
    "instance_key": "{{ order.id }}",
    "steps": [
        {"id": "cap", "type": "counter_limit", "key": "cakes:{{ order.delivery_date }}", "limit": 8,
         "amount": "order.cake_count()", "on_exhausted": "deny",
         "release_on": ["order_cancelled", "payment_link_expired"], "next": "ok"},
        {"id": "deny", "type": "deny",
         "reason": "Cake orders for {{ date_fmt(order.delivery_date, 'd MMM') }} are full.",
         "alternatives": "next_available_dates"},
        {"id": "ok", "type": "end"},
    ],
}  # fmt: skip

COD_CONFIRMATION: dict[str, Any] = {
    "schema_version": 1, "name": "COD confirmation", "mode": "observe",
    "trigger": {"type": "order_confirmed", "cel": "order.payment_method == 'cod'"},
    "instance_key": "{{ order.id }}",
    "steps": [
        {"id": "ask", "type": "send", "to": "customer",
         "text": "Please reply YES to confirm order {{ order.number }} "
                 "({{ money(order.total_paise) }}, cash on delivery) 🙏",
         "next": "wait"},
        {"id": "wait", "type": "wait_for_event", "event": "message_received",
         "match": "contains_any(lower(event.text), ['yes', 'haan', 'ha', 'confirm', 'ok'])", "consume": True,
         "timeout": "PT2H", "next": "thanks", "on_timeout": "tell_owner"},
        {"id": "thanks", "type": "send", "to": "customer", "text": "Thank you! Order confirmed ✅", "next": "end"},
        {"id": "tell_owner", "type": "notify_owner",
         "text": "COD order {{ order.number }} ({{ customer.name }}) was not confirmed within 2 hours.",
         "priority": "p1", "next": "end"},
        {"id": "end", "type": "end"},
    ],
}  # fmt: skip

DELIVERY_REMINDER: dict[str, Any] = {
    "schema_version": 1, "name": "Delivery reminder", "mode": "observe",
    "trigger": {"type": "order_confirmed"},
    "instance_key": "{{ order.id }}",
    "steps": [
        {"id": "w", "type": "wait", "until": "order.delivery_date.at('10:00') - duration('24h')", "next": "s"},
        {"id": "s", "type": "send", "to": "customer",
         "text": "Reminder: your order {{ order.number }} will be delivered tomorrow ({{ order.delivery_slot }}). 🎂",
         "template_fallback": {"name": "order_update",
                               "vars": ["Your order {{ order.number }} will be delivered tomorrow "
                                        "({{ order.delivery_slot }})."]},
         "next": "e"},
        {"id": "e", "type": "end"},
    ],
}  # fmt: skip

UNPAID_LINK_REMINDER: dict[str, Any] = {
    "schema_version": 1, "name": "Unpaid link reminder", "mode": "observe",
    "trigger": {"type": "payment_link_created"},
    "instance_key": "{{ event.payload.order_id }}",
    "steps": [
        {"id": "w1", "type": "wait_for_event", "event": "payment_captured",
         "match": "event.payload.order_id == order.id", "consume": False, "timeout": "PT2H",
         "next": "end", "on_timeout": "remind"},
        {"id": "remind", "type": "send", "to": "customer",
         "text": "Your order {{ order.number }} is waiting for payment. The link is above 🙂", "next": "w2"},
        {"id": "w2", "type": "wait_for_event", "event": "payment_captured",
         "match": "event.payload.order_id == order.id", "consume": False, "timeout": "PT22H",
         "next": "end", "on_timeout": "owner"},
        {"id": "owner", "type": "notify_owner",
         "text": "{{ customer.name }}'s order {{ order.number }} has been unpaid for 24 hours.", "priority": "p2",
         "next": "end"},
        {"id": "end", "type": "end"},
    ],
}  # fmt: skip

COMMENT_PRICE_DM: dict[str, Any] = {
    "schema_version": 1, "name": "Comment price → DM", "mode": "consume",
    "trigger": {"type": "comment_received", "keywords_any": ["price", "kitne", "rate", "cost", "pp"]},
    "instance_key": "{{ event.customer_id }}",
    "steps": [
        {"id": "once", "type": "claim_once", "key": "{{ event.customer_id }}:{{ event.payload.post_id }}",
         "on_claimed": "skip", "next": "dm"},
        {"id": "dm", "type": "send", "to": "customer",
         "blocks": [{"type": "text", "text": "Hi {{ customer.name }}! I've sent you the price details in a DM 💌"},
                    {"type": "product", "offering_id": "{{ event.payload.post_offering_id }}"}],
         "next": "end"},
        {"id": "end", "type": "end"},
    ],
}  # fmt: skip

OUT_FOR_DELIVERY: dict[str, Any] = {
    "schema_version": 1, "name": "Out for delivery update",
    "trigger": {"type": "order_status_changed", "to_status": ["out_for_delivery"]},
    "instance_key": "{{ order.id }}",
    "steps": [
        {"id": "s", "type": "send", "to": "customer", "text": "Your order {{ order.number }} is out for delivery 🚚",
         "next": "e"},
        {"id": "e", "type": "end"},
    ],
}  # fmt: skip

ALL = [DAILY_CAKE_LIMIT, COD_CONFIRMATION, DELIVERY_REMINDER, UNPAID_LINK_REMINDER, COMMENT_PRICE_DM, OUT_FOR_DELIVERY]
