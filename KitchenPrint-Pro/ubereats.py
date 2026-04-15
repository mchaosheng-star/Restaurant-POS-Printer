# Uber Eats POS integration helpers (OAuth Bearer + webhooks + accept_pos_order).
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

UBER_ACCEPT_URL = "https://api.uber.com/v1/eats/orders/{order_id}/accept_pos_order"
UBER_DENY_URL = "https://api.uber.com/v1/eats/orders/{order_id}/deny_pos_order"


def _first(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return ""


def verify_webhook_signature(body: bytes, signature_header: Optional[str], client_secret: str) -> bool:
    if not client_secret or not signature_header:
        return False
    dig = hmac.new(client_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(dig.lower(), signature_header.strip().lower())
    except Exception:
        return False


def fetch_order_details(resource_href: str, access_token: str, timeout: int = 60):
    req = urllib.request.Request(
        resource_href,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        logger.error("Uber Eats GET order HTTP %s: %s", e.code, err[:2000])
        return None
    except Exception as e:
        logger.exception("Uber Eats GET order failed: %s", e)
        return None


def _modifiers_to_options(item: dict):
    opts = []
    for grp in item.get("selected_modifier_groups") or []:
        if not isinstance(grp, dict):
            continue
        for si in grp.get("selected_items") or []:
            if not isinstance(si, dict):
                continue
            label = (si.get("title") or si.get("name") or "").strip()
            if label:
                opts.append({"name": label, "price": 0.0})
    return opts


def cart_to_internal_items(cart: dict):
    if not isinstance(cart, dict):
        return []
    raw = cart.get("items")
    if not isinstance(raw, list):
        raw = []
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or it.get("name") or "").strip()
        if not title:
            continue
        try:
            qty = int(it.get("quantity", 1) or 1)
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1
        note = _first(it.get("special_instructions"), it.get("specialRequests"))
        note = str(note).strip() if note else ""
        out.append({
            "name": title,
            "quantity": qty,
            "price": 0.0,
            "selectedOptions": _modifiers_to_options(it),
            "comment": note,
        })
    return out


def uber_order_response_to_internal(order_json: dict, order_id: str) -> dict:
    cart = order_json.get("cart") if isinstance(order_json.get("cart"), dict) else order_json
    items = cart_to_internal_items(cart if isinstance(cart, dict) else {})
    display = _first(
        order_json.get("display_id"),
        order_json.get("displayId"),
        order_json.get("external_reference_id"),
    )
    try:
        num = int(display) if str(display).isdigit() else int(str(order_id).replace("-", "")[:8], 16) % 90000 + 10000
    except Exception:
        num = int(__import__("time").time()) % 90000 + 10000
    uni = _first(
        order_json.get("special_instructions"),
        order_json.get("specialInstructions"),
        (order_json.get("cart") or {}).get("special_instructions") if isinstance(order_json.get("cart"), dict) else None,
    )
    return {
        "number": num,
        "tableNumber": "Uber Eats",
        "items": items,
        "universalComment": str(uni).strip() if uni else "",
        "uberOrderId": order_id,
        "uberPendingAccept": True,
    }


def accept_pos_order(order_id: str, access_token: str, reason: str = "accepted", timeout: int = 60) -> bool:
    url = UBER_ACCEPT_URL.format(order_id=order_id)
    body = json.dumps({"reason": reason}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 201, 204)
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        logger.error("Uber accept_pos_order HTTP %s: %s", e.code, err[:2000])
        return False
    except Exception as e:
        logger.exception("Uber accept_pos_order failed: %s", e)
        return False


def deny_pos_order(order_id: str, access_token: str, reason: str = "unavailable", timeout: int = 60) -> bool:
    url = UBER_DENY_URL.format(order_id=order_id)
    body = json.dumps({"reason": reason}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 201, 204)
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        logger.error("Uber deny_pos_order HTTP %s: %s", e.code, err[:2000])
        return False
    except Exception as e:
        logger.exception("Uber deny_pos_order failed: %s", e)
        return False
