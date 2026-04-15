from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DOORDASH_BASE_URL = "https://openapi.doordash.com/marketplace"
DOORDASH_CONFIRM_URL = DOORDASH_BASE_URL + "/api/v1/orders/{order_id}"
DOORDASH_ADJUST_URL = DOORDASH_BASE_URL + "/api/v1/orders/{order_id}/adjustment"
DOORDASH_STORE_URL = DOORDASH_BASE_URL + "/api/v2/stores/{store_location_id}"


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


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    raw = str(data or "").strip()
    padding = "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def create_jwt(access_key: dict, expires_in_sec: int = 300) -> str:
    developer_id = str(access_key.get("developer_id", "")).strip()
    key_id = str(access_key.get("key_id", "")).strip()
    signing_secret = str(access_key.get("signing_secret", "")).strip()
    if not developer_id or not key_id or not signing_secret:
        raise ValueError("DoorDash access key requires developer_id, key_id, signing_secret")

    now = int(time.time())
    header = {
        "alg": "HS256",
        "typ": "JWT",
        "dd-ver": "DD-JWT-V1",
    }
    payload = {
        "aud": "doordash",
        "iss": developer_id,
        "kid": key_id,
        "exp": now + int(expires_in_sec),
        "iat": now,
    }
    encoded_header = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(_b64url_decode(signing_secret), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_b64url_encode(signature)}"


def _request(url: str, method: str = "GET", access_key: dict | None = None, body: dict | None = None, timeout: int = 60):
    headers = {"Accept": "application/json"}
    data = None
    if access_key:
        headers["Authorization"] = f"Bearer {create_jwt(access_key)}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        logger.error("DoorDash %s %s HTTP %s: %s", method.upper(), url, e.code, err[:2000])
        return e.code, {"error": err}
    except Exception as e:
        logger.exception("DoorDash %s %s failed: %s", method.upper(), url, e)
        return None, None


def _walk_options(extras: list[Any]) -> list[dict]:
    out = []
    for extra in extras or []:
        if not isinstance(extra, dict):
            continue
        for option in extra.get("options") or []:
            if not isinstance(option, dict):
                continue
            name = str(option.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "price": float(option.get("price", 0) or 0) / 100.0,
                "doorDashLineOptionId": option.get("line_option_id"),
                "merchantSuppliedId": option.get("merchant_supplied_id"),
            })
            nested = option.get("extras") or []
            if nested:
                out.extend(_walk_options(nested))
    return out


def _iter_order_items(order_json: dict) -> list[dict]:
    direct = order_json.get("items")
    if isinstance(direct, list) and direct:
        return [it for it in direct if isinstance(it, dict)]
    out = []
    for category in order_json.get("categories") or []:
        if not isinstance(category, dict):
            continue
        for item in category.get("items") or []:
            if isinstance(item, dict):
                out.append(item)
    return out


def order_to_internal(order_json: dict) -> dict:
    items = []
    for item in _iter_order_items(order_json):
        title = str(item.get("name") or item.get("consumer_name") or "").strip()
        if not title:
            continue
        try:
            qty = int(item.get("quantity", 1) or 1)
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1
        items.append({
            "name": title,
            "quantity": qty,
            "price": float(item.get("price", 0) or 0) / 100.0,
            "selectedOptions": _walk_options(item.get("extras") or []),
            "comment": str(item.get("special_instructions") or "").strip(),
            "merchantSuppliedId": item.get("merchant_supplied_id"),
            "doorDashLineItemId": item.get("line_item_id"),
        })

    order_id = str(order_json.get("id") or "").strip()
    display = _first(
        order_json.get("store_order_cart_id"),
        order_json.get("merchant_order_reference"),
        order_json.get("merchant_supplied_id"),
    )
    try:
        number = int(display) if str(display).isdigit() else int(str(order_id).replace("-", "")[:8], 16) % 90000 + 10000
    except Exception:
        number = int(time.time()) % 90000 + 10000

    store = order_json.get("store") if isinstance(order_json.get("store"), dict) else {}
    return {
        "number": number,
        "tableNumber": "DoorDash",
        "items": items,
        "universalComment": str(_first(order_json.get("order_special_instructions"), order_json.get("special_instructions"))).strip(),
        "doorDashOrderId": order_id,
        "doorDashPendingConfirm": True,
        "doorDashStoreId": _first(store.get("id"), store.get("merchant_supplied_id")),
    }


def confirm_order(order_id: str, access_key: dict, merchant_supplied_id: str, success: bool, failure_reason: str = "", errors: list[dict] | None = None, prep_time: str = "") -> bool:
    payload: dict[str, Any] = {
        "merchant_supplied_id": str(merchant_supplied_id or ""),
        "order_status": "success" if success else "fail",
    }
    if not success and failure_reason:
        payload["failure_reason"] = failure_reason
    if errors:
        payload["errors"] = errors
    if success and prep_time:
        payload["prep_time"] = prep_time
    status, _resp = _request(DOORDASH_CONFIRM_URL.format(order_id=order_id), method="PATCH", access_key=access_key, body=payload)
    return status in (200, 202, 204)


def remove_item(order_id: str, access_key: dict, line_item_id: str) -> bool:
    payload = {
        "items": [
            {
                "line_item_id": str(line_item_id or "").strip(),
                "adjustment_type": "ITEM_REMOVE",
            }
        ]
    }
    status, _resp = _request(DOORDASH_ADJUST_URL.format(order_id=order_id), method="PATCH", access_key=access_key, body=payload)
    return status in (200, 202, 204)


def update_store_hours(access_key: dict, store_location_id: str, store_payload: dict) -> bool:
    status, _resp = _request(DOORDASH_STORE_URL.format(store_location_id=store_location_id), method="PATCH", access_key=access_key, body=store_payload)
    return status in (200, 202, 204)
