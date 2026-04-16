# app.py
from flask import Flask, request, jsonify, send_from_directory, make_response
from datetime import datetime, timedelta
import copy
import csv
import difflib
import os
import re
import win32con # type: ignore
import win32print # type: ignore
import win32ui # type: ignore
import tempfile
import time
import json
import logging
import threading

import doordash
import ubereats
import print_capture

app = Flask(__name__)

# Incoming orders queue (in-memory)
INCOMING_ORDERS = []
INCOMING_NEXT_ID = 1

# Accepted orders (in-memory)
ACCEPTED_ORDERS = []

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')

# Printer configuration
PRINTER_NAME = "Brother MFC-L5850DW series (Copy 1)"
SUSHI_PRINTER_NAME = "Brother MFC-L5850DW series (Copy 1)"
KITCHEN_PRINTER_NAME = "Brother MFC-L5850DW series (Copy 1)"

# CSV and Menu File Configuration
CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
MENU_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data/menu.json')
PRINT_JOBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'print_jobs')
VIRTUAL_PRINTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'virtual_prints')
LOCAL_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'local_settings.json')

UBER_WEBHOOK_LOCK = threading.Lock()
UBER_WEBHOOK_EVENT_IDS = set()
DOORDASH_WEBHOOK_LOCK = threading.Lock()
DOORDASH_WEBHOOK_EVENT_IDS = set()

# Menu cache for category lookup
_MENU_CACHE = None
_MENU_CACHE_MTIME = None
_SPECIAL_NAME_ZH = {
    'tempura shrimps': '天妇罗虾',
    'fried potato': '炸红薯',
    'asparagus': '芦笋',
    'crab': '蟹',
    'fried calamari': '炸鱿鱼',
}

# --- ESC/POS Commands (Updated to match app DUMMY.py for more formatting options) ---
ESC = b'\x1B'
GS = b'\x1D'

InitializePrinter = ESC + b'@'
BoldOn = ESC + b'E\x01'
BoldOff = ESC + b'E\x00'
DoubleHeightWidth = GS + b'!\x11'  # Double Height and Double Width
DoubleHeight = GS + b'!\x01'       # Double Height only
DoubleWidth = GS + b'!\x10'        # Double Width only
NormalText = GS + b'!\x00'
AlignLeft = ESC + b'a\x00'
AlignCenter = ESC + b'a\x01'
AlignRight = ESC + b'a\x02'
SelectFontA = ESC + b'M\x00' # Standard Font A
SelectFontB = ESC + b'M\x01' # Smaller Font B
FullCut = GS + b'V\x00'


def to_bytes(s, encoding='cp437'):
    if isinstance(s, bytes):
        return s
    return s.encode(encoding, errors='replace')

def normalize_print_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\x00', '')
    return text.strip()

def first_value(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return ""

def normalize_order_data(order_data):
    if not isinstance(order_data, dict):
        return {}
    items_in = order_data.get('items', [])
    items_out = []
    if isinstance(items_in, list):
        for it in items_in:
            if not isinstance(it, dict):
                continue
            name = normalize_print_text(it.get('name', ''))
            if not name:
                continue
            qty_raw = first_value(it.get('quantity'), 1)
            try:
                qty = int(qty_raw)
            except Exception:
                qty = 1
            if qty <= 0:
                qty = 1
            price_raw = first_value(it.get('price'), 0)
            try:
                price = float(price_raw)
            except Exception:
                price = 0.0
            items_out.append({
                'name': name,
                'nameZh': normalize_print_text(first_value(it.get('nameZh'), _name_zh_for_item_name(name))),
                'category': normalize_print_text(first_value(it.get('category'), it.get('_category'))),
                'quantity': qty,
                'price': price,
                'selectedOptions': it.get('selectedOptions', []) if isinstance(it.get('selectedOptions', []), list) else [],
                'merchantSuppliedId': normalize_print_text(first_value(it.get('merchantSuppliedId'), it.get('merchant_supplied_id'))),
                'doorDashLineItemId': normalize_print_text(first_value(it.get('doorDashLineItemId'), it.get('door_dash_line_item_id'), it.get('line_item_id'))),
                'comment': normalize_print_text(first_value(
                    it.get('comment'),
                    it.get('note'),
                    it.get('notes'),
                    it.get('special_request'),
                    it.get('specialRequest'),
                    it.get('customer_note'),
                    it.get('customerNote'),
                    it.get('item_note'),
                    it.get('itemNote')
                ))
            })
    normalized = dict(order_data)
    normalized['items'] = items_out
    normalized['kitchenPrinter'] = normalize_print_text(first_value(
        order_data.get('kitchenPrinter'),
        order_data.get('kitchen_printer')
    ))
    normalized['sushiPrinter'] = normalize_print_text(first_value(
        order_data.get('sushiPrinter'),
        order_data.get('sushi_printer')
    ))
    normalized['packerPrinter'] = normalize_print_text(first_value(
        order_data.get('packerPrinter'),
        order_data.get('packer_printer')
    ))
    normalized['universalComment'] = normalize_print_text(first_value(
        order_data.get('universalComment'),
        order_data.get('note'),
        order_data.get('notes'),
        order_data.get('special_request'),
        order_data.get('specialRequest'),
        order_data.get('customer_note'),
        order_data.get('customerNote')
    ))
    return normalized

def _uber_env_bool(key, default=False):
    v = os.environ.get(key, "")
    if v == "":
        return default
    return v.lower() in ("1", "true", "yes", "on")

def _load_uber_store_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ubereats_store_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _load_doordash_store_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "doordash_store_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_doordash_store_config(data):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "doordash_store_config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data or {}, f, ensure_ascii=False, indent=2)

def _doordash_env_bool(key, default=False):
    v = os.environ.get(key, "")
    if v == "":
        return default
    return v.lower() in ("1", "true", "yes", "on")

def _doordash_is_duplicate_event(event_id):
    if not event_id:
        return False
    with DOORDASH_WEBHOOK_LOCK:
        if event_id in DOORDASH_WEBHOOK_EVENT_IDS:
            return True
        DOORDASH_WEBHOOK_EVENT_IDS.add(event_id)
        if len(DOORDASH_WEBHOOK_EVENT_IDS) > 6000:
            DOORDASH_WEBHOOK_EVENT_IDS.clear()
        return False

def _get_doordash_access_key(cfg=None):
    cfg = cfg if isinstance(cfg, dict) else _load_doordash_store_config()
    access_key = cfg.get("accessKey") if isinstance(cfg.get("accessKey"), dict) else {}
    return {
        "developer_id": str(first_value(access_key.get("developer_id"), os.environ.get("DOORDASH_DEVELOPER_ID", ""))).strip(),
        "key_id": str(first_value(access_key.get("key_id"), os.environ.get("DOORDASH_KEY_ID", ""))).strip(),
        "signing_secret": str(first_value(access_key.get("signing_secret"), os.environ.get("DOORDASH_SIGNING_SECRET", ""))).strip(),
    }

def _doordash_access_key_ready(access_key):
    return bool(access_key.get("developer_id") and access_key.get("key_id") and access_key.get("signing_secret"))

def _doordash_apply_store_config(order_data, cfg):
    if isinstance(cfg.get("categoryPrinters"), dict):
        order_data["categoryPrinters"] = cfg["categoryPrinters"]
    if isinstance(cfg.get("specialPrinters"), dict):
        order_data["specialPrinters"] = cfg["specialPrinters"]
    kitchen_printer = str(first_value(cfg.get("kitchenPrinter"), cfg.get("kitchen_printer"))).strip()
    if kitchen_printer:
        order_data["kitchenPrinter"] = kitchen_printer
    sushi_printer = str(first_value(cfg.get("sushiPrinter"), cfg.get("sushi_printer"))).strip()
    if sushi_printer:
        order_data["sushiPrinter"] = sushi_printer
    packer_printer = str(first_value(cfg.get("packerPrinter"), cfg.get("packer_printer"))).strip()
    if packer_printer:
        order_data["packerPrinter"] = packer_printer
    printer_override = str(first_value(cfg.get("printer"), cfg.get("defaultPrinter"))).strip()
    if printer_override:
        order_data["printer"] = printer_override

def _doordash_fail_confirmation(order_data, access_key, failure_reason, error_code="OTHER", error_message=""):
    order_id = str(order_data.get("doorDashOrderId", "")).strip()
    if not order_id or not _doordash_access_key_ready(access_key):
        return False
    error_payload = []
    merchant_item_id = ""
    for item in order_data.get("items", []):
        merchant_item_id = str(first_value(item.get("merchantSuppliedId"), item.get("doorDashLineItemId"))).strip()
        if merchant_item_id:
            break
    if error_code:
        error_payload.append({
            "code": error_code,
            "merchant_supplied_id": merchant_item_id or "unknown",
            "message": error_message or failure_reason or error_code,
        })
    return doordash.confirm_order(
        order_id,
        access_key,
        merchant_supplied_id=str(order_data.get("number", "")),
        success=False,
        failure_reason=failure_reason,
        errors=error_payload,
    )

def _maybe_confirm_doordash_success(order_data, access_key):
    order_id = str(order_data.get("doorDashOrderId", "")).strip()
    if not order_id or not order_data.get("doorDashPendingConfirm") or not _doordash_access_key_ready(access_key):
        return False
    ok = doordash.confirm_order(
        order_id,
        access_key,
        merchant_supplied_id=str(order_data.get("number", "")),
        success=True,
    )
    if ok:
        order_data["doorDashPendingConfirm"] = False
    return ok

def _uber_is_duplicate_event(event_id):
    if not event_id:
        return False
    with UBER_WEBHOOK_LOCK:
        if event_id in UBER_WEBHOOK_EVENT_IDS:
            return True
        UBER_WEBHOOK_EVENT_IDS.add(event_id)
        if len(UBER_WEBHOOK_EVENT_IDS) > 6000:
            UBER_WEBHOOK_EVENT_IDS.clear()
        return False

def _uber_webhook_worker(payload):
    if not isinstance(payload, dict):
        return
    if _uber_is_duplicate_event(payload.get("event_id")):
        return
    if payload.get("event_type") != "orders.notification":
        return
    meta = payload.get("meta") or {}
    order_id = meta.get("resource_id")
    href = payload.get("resource_href")
    if not order_id or not href:
        app.logger.error("Uber webhook: missing resource_id or resource_href")
        return
    token = os.environ.get("UBEREATS_ACCESS_TOKEN", "").strip()
    if not token:
        app.logger.error("Uber webhook: set UBEREATS_ACCESS_TOKEN to fetch and accept orders")
        return
    detail = ubereats.fetch_order_details(href, token)
    if not detail:
        if _uber_env_bool("UBEREATS_DENY_ON_FETCH_FAIL"):
            ubereats.deny_pos_order(order_id, token, reason="system_error")
        return
    raw = ubereats.uber_order_response_to_internal(detail, order_id)
    order_data = normalize_order_data(raw)
    if not order_data.get("items"):
        app.logger.error("Uber webhook: no line items mapped for order %s", order_id)
        if _uber_env_bool("UBEREATS_DENY_ON_FAILURE"):
            ubereats.deny_pos_order(order_id, token, reason="invalid_cart")
        return
    cfg = _load_uber_store_config()
    if isinstance(cfg.get("categoryPrinters"), dict):
        order_data["categoryPrinters"] = cfg["categoryPrinters"]
    if isinstance(cfg.get("specialPrinters"), dict):
        order_data["specialPrinters"] = cfg["specialPrinters"]
    kitchen_printer = str(first_value(cfg.get("kitchenPrinter"), cfg.get("kitchen_printer"))).strip()
    if kitchen_printer:
        order_data["kitchenPrinter"] = kitchen_printer
    sushi_printer = str(first_value(cfg.get("sushiPrinter"), cfg.get("sushi_printer"))).strip()
    if sushi_printer:
        order_data["sushiPrinter"] = sushi_printer
    packer_printer = str(first_value(cfg.get("packerPrinter"), cfg.get("packer_printer"))).strip()
    if packer_printer:
        order_data["packerPrinter"] = packer_printer
    printer_override = (cfg.get("printer") or cfg.get("defaultPrinter") or "").strip()
    if printer_override:
        order_data["printer"] = printer_override
    if "number" not in order_data or order_data.get("number") is None:
        order_data["number"] = int(datetime.now().timestamp() % 10000)

    if _uber_env_bool("UBEREATS_AUTO_ACCEPT", default=False):
        try:
            ok = handle_order_internal(order_data)
        except Exception as e:
            app.logger.exception("Uber AUTO_ACCEPT handle_order_internal: %s", e)
            ok = False
        if ok:
            if ubereats.accept_pos_order(order_id, token):
                app.logger.info("Uber accept_pos_order success %s", order_id)
            else:
                app.logger.error("Uber accept_pos_order failed after print %s", order_id)
        else:
            app.logger.error("Uber order print/log failed %s", order_id)
            if _uber_env_bool("UBEREATS_DENY_ON_FAILURE"):
                ubereats.deny_pos_order(order_id, token, reason="system_error")
        return
    enqueue_incoming(order_data)
    app.logger.info("Uber order queued for POS accept order_id=%s", order_id)

def _doordash_webhook_worker(payload):
    if not isinstance(payload, dict):
        return
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    event_type = str(event.get("type", "")).strip()
    event_status = str(event.get("status", "")).strip().upper()
    order = payload.get("order") if isinstance(payload.get("order"), dict) else {}
    order_id = str(order.get("id", "")).strip()
    event_id = str(first_value(payload.get("id"), payload.get("event_id"), event.get("event_timestamp"), order_id)).strip()
    if _doordash_is_duplicate_event(event_id):
        return
    if event_type != "OrderCreate" or event_status != "NEW" or not order_id:
        return

    raw = doordash.order_to_internal(order)
    order_data = normalize_order_data(raw)
    if not order_data.get("items"):
        cfg = _load_doordash_store_config()
        access_key = _get_doordash_access_key(cfg)
        _doordash_fail_confirmation(
            order_data,
            access_key,
            "Invalid Order - No items mapped",
            error_code="INVALID_ORDER",
            error_message="Order contained no supported items",
        )
        app.logger.error("DoorDash webhook: no line items mapped for order %s", order_id)
        return

    cfg = _load_doordash_store_config()
    _doordash_apply_store_config(order_data, cfg)
    if "number" not in order_data or order_data.get("number") is None:
        order_data["number"] = int(datetime.now().timestamp() % 10000)
    access_key = _get_doordash_access_key(cfg)

    if _doordash_env_bool("DOORDASH_AUTO_CONFIRM", default=False):
        try:
            ok = handle_order_internal(order_data)
        except Exception as e:
            app.logger.exception("DoorDash AUTO_CONFIRM handle_order_internal: %s", e)
            ok = False
        if ok:
            if _maybe_confirm_doordash_success(order_data, access_key):
                app.logger.info("DoorDash confirm_order ok for %s", order_id)
            else:
                app.logger.error("DoorDash confirm_order failed after print %s", order_id)
            return
        _doordash_fail_confirmation(
            order_data,
            access_key,
            "Store Unavailable - Connectivity Issue",
            error_code="INTERNAL_ERROR",
            error_message="Print/log processing failed",
        )
        app.logger.error("DoorDash order print/log failed %s", order_id)
        return

    enqueue_incoming(order_data)
    app.logger.info("DoorDash order queued for POS accept order_id=%s", order_id)

# --- Word Wrap Helper Function (Updated to match app DUMMY.py for better wrapping) ---
def word_wrap_text(text, max_width, initial_indent="", subsequent_indent=""):
    lines = []
    if not text: return lines
    
    paragraphs = text.split('\n')
    
    for i, paragraph_text in enumerate(paragraphs):
        if not paragraph_text.strip() and i < len(paragraphs) -1 : 
            lines.append(initial_indent if not lines else subsequent_indent) 
            continue

        current_line = []
        current_length = 0
        words = paragraph_text.split(' ')
        
        current_indent = initial_indent if not lines and not any(lines) else subsequent_indent
        
        for word_idx, word in enumerate(words):
            if not word: 
                if current_line: current_line.append("") 
                continue

            available_width_for_word = max_width - len(current_indent) - current_length - (1 if current_line else 0)
            if len(word) > available_width_for_word and not current_line : 
                part_fits = word[:available_width_for_word]
                remaining_part = word[available_width_for_word:]
                lines.append(current_indent + part_fits)
                
                while remaining_part:
                    available_width_for_remaining = max_width - len(subsequent_indent)
                    part_fits = remaining_part[:available_width_for_remaining]
                    remaining_part = remaining_part[available_width_for_remaining:]
                    lines.append(subsequent_indent + part_fits)
                current_line = []
                current_length = 0
                current_indent = subsequent_indent 
                continue

            if current_length + len(word) + (1 if current_line else 0) <= (max_width - len(current_indent)):
                current_line.append(word)
                current_length += len(word) + (1 if len(current_line) > 1 else 0) 
            else:
                if current_line: 
                    lines.append(current_indent + " ".join(current_line))
                
                current_line = [word]
                current_length = len(word)
                current_indent = subsequent_indent 
        
        if current_line: 
            lines.append(current_indent + " ".join(current_line))
            
    return lines if lines else [initial_indent]

def list_windows_printers():
    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        printers = win32print.EnumPrinters(flags)
        names = []
        for p in printers:
            # (flags, description, name, comment)
            if len(p) >= 3 and p[2]:
                names.append(p[2])
        return sorted(set(names))
    except Exception as e:
        app.logger.error(f"Error listing printers: {e}")
        return []

@app.route('/api/printers', methods=['GET'])
def api_printers():
    return jsonify(list_windows_printers())

def _load_menu_cached():
    global _MENU_CACHE, _MENU_CACHE_MTIME
    try:
        mtime = os.path.getmtime(MENU_FILE)
        if _MENU_CACHE is not None and _MENU_CACHE_MTIME == mtime:
            return _MENU_CACHE
        with open(MENU_FILE, 'r', encoding='utf-8') as f:
            _MENU_CACHE = json.load(f)
        _MENU_CACHE_MTIME = mtime
        return _MENU_CACHE
    except Exception:
        return {}

def _load_local_settings():
    try:
        with open(LOCAL_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_local_settings(data):
    try:
        os.makedirs(os.path.dirname(LOCAL_SETTINGS_FILE), exist_ok=True)
        with open(LOCAL_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data or {}, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

@app.route('/api/local_settings', methods=['GET', 'POST'])
def api_local_settings():
    if request.method == 'GET':
        cfg = _load_local_settings()
        return jsonify({
            "autoAccept": bool(cfg.get("autoAccept")),
            "kitchenPrinter": normalize_print_text(cfg.get("kitchenPrinter", "")),
            "sushiPrinter": normalize_print_text(cfg.get("sushiPrinter", "")),
            "packerPrinter": normalize_print_text(cfg.get("packerPrinter", "")),
        })
    cfg = _load_local_settings()
    data = request.json or {}
    if "autoAccept" in data:
        cfg["autoAccept"] = bool(data.get("autoAccept"))
    if "kitchenPrinter" in data:
        cfg["kitchenPrinter"] = normalize_print_text(data.get("kitchenPrinter", ""))
    if "sushiPrinter" in data:
        cfg["sushiPrinter"] = normalize_print_text(data.get("sushiPrinter", ""))
    if "packerPrinter" in data:
        cfg["packerPrinter"] = normalize_print_text(data.get("packerPrinter", ""))
    ok = _save_local_settings(cfg)
    return jsonify({"status": "success" if ok else "error"}), (200 if ok else 500)

def _category_for_item_name(item_name):
    name_l = str(item_name or '').strip().lower()
    if not name_l:
        return None
    menu_item = _menu_item_for_name(item_name)
    if menu_item:
        return str(menu_item.get('_category', '')) or None
    return None

def _menu_item_for_name(item_name):
    raw_name = str(item_name or '').strip()
    if not raw_name:
        return None
    menu = _load_menu_cached()
    if not isinstance(menu, dict):
        return None
    name_l = raw_name.lower()
    normalized_name = _normalize_menu_lookup_name(raw_name)
    exact_normalized = None
    fuzzy_candidates = []
    for cat, items in menu.items():
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            menu_name = str(it.get('name', '')).strip()
            if not menu_name:
                continue
            menu_name_l = menu_name.lower()
            if menu_name_l == name_l:
                menu_item = dict(it)
                menu_item['_category'] = str(cat)
                return menu_item
            menu_norm = _normalize_menu_lookup_name(menu_name)
            if normalized_name and menu_norm == normalized_name and exact_normalized is None:
                menu_item = dict(it)
                menu_item['_category'] = str(cat)
                exact_normalized = menu_item
            fuzzy_candidates.append((menu_norm, str(cat), dict(it)))
    if exact_normalized:
        return exact_normalized
    if normalized_name and fuzzy_candidates:
        names = [candidate[0] for candidate in fuzzy_candidates if candidate[0]]
        matches = difflib.get_close_matches(normalized_name, names, n=1, cutoff=0.84)
        if matches:
            best = matches[0]
            for candidate_norm, candidate_cat, candidate_item in fuzzy_candidates:
                if candidate_norm == best:
                    candidate_item['_category'] = candidate_cat
                    return candidate_item
    return None

def _name_zh_for_item_name(item_name):
    name_l = str(item_name or '').strip().lower()
    if not name_l:
        return ""
    if name_l in _SPECIAL_NAME_ZH:
        return _SPECIAL_NAME_ZH[name_l]
    menu_item = _menu_item_for_name(item_name)
    if isinstance(menu_item, dict):
        return normalize_print_text(menu_item.get('nameZh', ''))
    return ""

def _normalize_menu_lookup_name(value):
    s = str(value or '').strip().lower()
    if not s:
        return ""
    s = s.replace('&', ' and ')
    s = re.sub(r'\([^)]*\)', ' ', s)
    s = re.sub(r'\b(combo|meal|size|regular|large|medium|small)\b', ' ', s)
    s = re.sub(r'\bpcs?\b', ' ', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

@app.route('/')
def serve_index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'Sakura.html')

@app.route('/Sakura.html')
def serve_sakura_alias():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'Sakura.html')

@app.route('/sushaki.html')
def serve_old_sushaki_alias():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'Sakura.html')

@app.route('/api/menu', methods=['GET'])
def get_menu():
    try:
        with open(MENU_FILE, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({})

@app.route('/api/menu', methods=['POST'])
def save_menu():
    new_menu_data = request.json
    os.makedirs(os.path.dirname(MENU_FILE), exist_ok=True)
    with open(MENU_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_menu_data, f, indent=2)
    return jsonify({"status": "success"})

def enqueue_incoming(order_data):
    global INCOMING_NEXT_ID
    order_data = normalize_order_data(order_data)
    local_cfg = _load_local_settings()
    if bool(local_cfg.get("autoAccept")):
        if not order_data.get('kitchenPrinter') and local_cfg.get('kitchenPrinter'):
            order_data['kitchenPrinter'] = normalize_print_text(local_cfg.get('kitchenPrinter'))
        if not order_data.get('sushiPrinter') and local_cfg.get('sushiPrinter'):
            order_data['sushiPrinter'] = normalize_print_text(local_cfg.get('sushiPrinter'))
        if not order_data.get('packerPrinter') and local_cfg.get('packerPrinter'):
            order_data['packerPrinter'] = normalize_print_text(local_cfg.get('packerPrinter'))
        ok = False
        try:
            ok = bool(handle_order_internal(order_data))
        except Exception as e:
            app.logger.error(f"Auto accept error: {e}")
            ok = False
        accepted_id = INCOMING_NEXT_ID
        INCOMING_NEXT_ID += 1
        ACCEPTED_ORDERS.append({
            'id': accepted_id,
            'accepted_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'finished_at': None,
            'status': 'accepted' if ok else 'error',
            'order': order_data
        })
        return {
            'id': accepted_id,
            'received_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'order': order_data
        }
    oid = INCOMING_NEXT_ID
    INCOMING_NEXT_ID += 1
    entry = {
        'id': oid,
        'received_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'order': order_data
    }
    INCOMING_ORDERS.append(entry)
    return entry

@app.route('/api/incoming', methods=['GET'])
def api_incoming_list():
    return jsonify(INCOMING_ORDERS)

@app.route('/api/incoming/<int:incoming_id>', methods=['DELETE'])
def api_incoming_delete(incoming_id):
    idx = next((i for i, o in enumerate(INCOMING_ORDERS) if o.get('id') == incoming_id), None)
    if idx is None:
        return jsonify({"status": "error", "message": "Not found"}), 404
    INCOMING_ORDERS.pop(idx)
    return jsonify({"status": "success"}), 200

@app.route('/api/incoming', methods=['POST'])
def api_incoming_create():
    data = request.json
    if not data or 'items' not in data:
        return jsonify({"status": "error", "message": "Invalid order data"}), 400
    entry = enqueue_incoming(data)
    return jsonify({"status": "success", "id": entry['id']}), 200

@app.route('/api/incoming/<int:incoming_id>/accept', methods=['POST'])
def api_incoming_accept(incoming_id):
    idx = next((i for i, o in enumerate(INCOMING_ORDERS) if o.get('id') == incoming_id), None)
    if idx is None:
        return jsonify({"status": "error", "message": "Not found"}), 404
    entry = INCOMING_ORDERS.pop(idx)
    order_data = entry.get('order') or {}
    req_data = request.json or {}
    if isinstance(req_data.get('categoryPrinters'), dict):
        order_data['categoryPrinters'] = req_data.get('categoryPrinters')
    if isinstance(req_data.get('specialPrinters'), dict):
        order_data['specialPrinters'] = req_data.get('specialPrinters')
    kitchen_printer = normalize_print_text(first_value(req_data.get('kitchenPrinter'), req_data.get('kitchen_printer')))
    if kitchen_printer:
        order_data['kitchenPrinter'] = kitchen_printer
    sushi_printer = normalize_print_text(first_value(req_data.get('sushiPrinter'), req_data.get('sushi_printer')))
    if sushi_printer:
        order_data['sushiPrinter'] = sushi_printer
    packer_printer = normalize_print_text(first_value(req_data.get('packerPrinter'), req_data.get('packer_printer')))
    if packer_printer:
        order_data['packerPrinter'] = packer_printer
    if 'number' not in order_data:
        order_data['number'] = int(datetime.now().timestamp() % 10000)
    ok = None
    try:
        ok = handle_order_internal(order_data)
    except Exception as e:
        app.logger.error(f"Accept error: {e}")
        ok = False
    ACCEPTED_ORDERS.append({
        'id': entry.get('id'),
        'accepted_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'finished_at': None,
        'status': 'accepted' if ok else 'error',
        'order': order_data
    })
    if ok:
        uber_oid = order_data.get('uberOrderId')
        if uber_oid and order_data.get('uberPendingAccept'):
            utok = os.environ.get("UBEREATS_ACCESS_TOKEN", "").strip()
            if utok:
                if ubereats.accept_pos_order(uber_oid, utok):
                    order_data['uberPendingAccept'] = False
                    app.logger.info("Uber accept_pos_order ok for %s", uber_oid)
                else:
                    app.logger.error("Uber accept_pos_order failed for %s", uber_oid)
        dd_access_key = _get_doordash_access_key()
        dd_oid = str(order_data.get('doorDashOrderId', '')).strip()
        if dd_oid and order_data.get('doorDashPendingConfirm'):
            if _maybe_confirm_doordash_success(order_data, dd_access_key):
                app.logger.info("DoorDash confirm_order ok for %s", dd_oid)
            else:
                app.logger.error("DoorDash confirm_order failed for %s", dd_oid)
        return jsonify({"status": "success"}), 200
    dd_access_key = _get_doordash_access_key()
    if str(order_data.get('doorDashOrderId', '')).strip() and order_data.get('doorDashPendingConfirm'):
        _doordash_fail_confirmation(
            order_data,
            dd_access_key,
            "Store Unavailable - Connectivity Issue",
            error_code="INTERNAL_ERROR",
            error_message="Failed to print or process order",
        )
    return jsonify({"status": "error", "message": "Failed to process order"}), 500

@app.route('/api/accepted', methods=['GET'])
def api_accepted_list():
    return jsonify(ACCEPTED_ORDERS)

@app.route('/api/accepted/<int:accepted_id>/finish', methods=['POST'])
def api_accepted_finish(accepted_id):
    for o in ACCEPTED_ORDERS:
        if o.get('id') == accepted_id:
            o['status'] = 'finished'
            o['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            return jsonify({"status": "success"}), 200
    return jsonify({"status": "error", "message": "Not found"}), 404

@app.route('/api/accepted/<int:accepted_id>/doordash/item_86', methods=['POST'])
def api_accepted_doordash_item_86(accepted_id):
    entry = next((o for o in ACCEPTED_ORDERS if o.get('id') == accepted_id), None)
    if not entry:
        return jsonify({"status": "error", "message": "Accepted order not found"}), 404
    order_data = entry.get('order') if isinstance(entry.get('order'), dict) else {}
    order_id = str(order_data.get('doorDashOrderId', '')).strip()
    if not order_id:
        return jsonify({"status": "error", "message": "Not a DoorDash order"}), 400
    data = request.json or {}
    line_item_id = str(data.get('line_item_id', '')).strip()
    if not line_item_id and data.get('item_index') is not None:
        try:
            item_index = int(data.get('item_index'))
        except Exception:
            item_index = -1
        items = order_data.get('items', [])
        if 0 <= item_index < len(items):
            line_item_id = str(items[item_index].get('doorDashLineItemId', '')).strip()
    if not line_item_id:
        return jsonify({"status": "error", "message": "line_item_id is required"}), 400
    access_key = _get_doordash_access_key()
    if not _doordash_access_key_ready(access_key):
        return jsonify({"status": "error", "message": "DoorDash access key not configured"}), 400
    ok = doordash.remove_item(order_id, access_key, line_item_id)
    if not ok:
        return jsonify({"status": "error", "message": "DoorDash item remove failed"}), 502
    order_data['items'] = [
        item for item in order_data.get('items', [])
        if str(item.get('doorDashLineItemId', '')).strip() != line_item_id
    ]
    entry['order'] = order_data
    return jsonify({"status": "success"}), 200

@app.route('/api/doordash/store_hours', methods=['GET', 'POST'])
def api_doordash_store_hours():
    cfg = _load_doordash_store_config()
    if request.method == 'GET':
        return jsonify(cfg.get('storeHours') or {})
    data = request.json or {}
    store_hours = cfg.get('storeHours') if isinstance(cfg.get('storeHours'), dict) else {}
    merchant_store_id = str(first_value(
        data.get('merchant_supplied_store_id'),
        store_hours.get('merchant_supplied_store_id'),
        cfg.get('storeLocationId'),
    )).strip()
    store_payload = {
        'merchant_supplied_store_id': merchant_store_id,
        'open_hours': data.get('open_hours', store_hours.get('open_hours', [])),
        'special_hours': data.get('special_hours', store_hours.get('special_hours', [])),
    }
    cfg['storeHours'] = store_payload
    if data.get('store_location_id'):
        cfg['storeLocationId'] = str(data.get('store_location_id')).strip()
    _save_doordash_store_config(cfg)
    push_now = bool(data.get('push'))
    if not push_now:
        return jsonify({"status": "success", "storeHours": store_payload}), 200
    access_key = _get_doordash_access_key(cfg)
    store_location_id = str(first_value(data.get('store_location_id'), cfg.get('storeLocationId'))).strip()
    if not store_location_id:
        return jsonify({"status": "error", "message": "store_location_id is required for push"}), 400
    if not _doordash_access_key_ready(access_key):
        return jsonify({"status": "error", "message": "DoorDash access key not configured"}), 400
    ok = doordash.update_store_hours(access_key, store_location_id, store_payload)
    if not ok:
        return jsonify({"status": "error", "message": "DoorDash store hours update failed"}), 502
    return jsonify({"status": "success", "storeHours": store_payload}), 200

@app.route('/api/doordash/store_hours/<store_location_id>', methods=['GET'])
def api_doordash_store_hours_pull(store_location_id):
    cfg = _load_doordash_store_config()
    expected_token = str(first_value(cfg.get("webhookToken"), os.environ.get("DOORDASH_WEBHOOK_TOKEN", ""))).strip()
    if expected_token:
        auth_header = str(request.headers.get("Authorization", "")).strip()
        raw_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else auth_header
        if raw_token != expected_token:
            return jsonify({"status": "error", "message": "Unauthorized"}), 401
    store_hours = cfg.get('storeHours') if isinstance(cfg.get('storeHours'), dict) else {}
    payload = {
        'merchant_supplied_store_id': str(first_value(store_hours.get('merchant_supplied_store_id'), store_location_id)),
        'open_hours': store_hours.get('open_hours', []),
        'special_hours': store_hours.get('special_hours', []),
    }
    return jsonify(payload)

def handle_order_internal(order_data):
    category_printers = order_data.get('categoryPrinters') or {}
    special_printers = order_data.get('specialPrinters') or {}
    def _is_sushi_category_name(category_name):
        cl = str(category_name or '').strip().lower()
        if not cl:
            return False
        return ('roll' in cl) or cl.startswith('sushi') or ('sashimi' in cl) or ('nigiri' in cl) or ('maki' in cl)

    def _is_sushi_item(item):
        if not isinstance(item, dict):
            return False
        cat = str(item.get('category', '')).strip()
        if not cat:
            found = _category_for_item_name(item.get('name', ''))
            if found:
                item['category'] = found
                cat = found
        if _is_sushi_category_name(cat):
            return True
        name_l = str(item.get('name', '')).strip().lower()
        return any(token in name_l for token in (
            ' roll',
            'sushi',
            'sashimi',
            'nigiri',
            'maki',
            'chirashi',
            'naruto',
            'unagi don',
            'poke roll',
        ))

    if not category_printers and (SUSHI_PRINTER_NAME or KITCHEN_PRINTER_NAME):
        menu = _load_menu_cached()
        if isinstance(menu, dict):
            auto = {}
            for cat in menu.keys():
                c = str(cat)
                cl = c.lower()
                if cl == 'entree':
                    if KITCHEN_PRINTER_NAME:
                        auto[c] = KITCHEN_PRINTER_NAME
                    continue
                if ('roll' in cl) or cl.startswith('sushi') or ('sashimi' in cl) or ('nigiri' in cl) or ('maki' in cl):
                    if SUSHI_PRINTER_NAME:
                        auto[c] = SUSHI_PRINTER_NAME
            if auto:
                category_printers = auto
    explicit_station_printers = bool(
        str(first_value(
            order_data.get('kitchenPrinter'),
            order_data.get('kitchen_printer'),
            order_data.get('sushiPrinter'),
            order_data.get('sushi_printer'),
        )).strip()
    )
    if (isinstance(category_printers, dict) and category_printers) or explicit_station_printers:
        items = order_data.get('items', [])
        printed_any = False
        printed_all = True
        original_order = copy.deepcopy(order_data)
        base_comment = normalize_print_text(order_data.get('universalComment', ''))
        sushi_printer = str(first_value(
            order_data.get('sushiPrinter'),
            order_data.get('sushi_printer')
        )).strip() or None
        kitchen_printer = str(first_value(
            order_data.get('kitchenPrinter'),
            order_data.get('kitchen_printer'),
            KITCHEN_PRINTER_NAME,
            PRINTER_NAME,
        )).strip() or None
        if not sushi_printer and isinstance(category_printers, dict):
            for cat, printer in category_printers.items():
                printer_name = str(printer or '').strip()
                if printer_name and _is_sushi_category_name(cat):
                    sushi_printer = printer_name
                    break
        sushi_printer = str(first_value(sushi_printer, SUSHI_PRINTER_NAME)).strip() or None
        sushi_items = []
        kitchen_items = []

        packer_printer = str(first_value(
            order_data.get('packerPrinter'),
            order_data.get('packer_printer'),
            special_printers.get('packer_original'),
        )).strip()
        if packer_printer:
            packer_ok = print_kitchen_ticket(original_order, copy_info='Original', printer_name=packer_printer)
            printed_any = printed_any or packer_ok
            printed_all = printed_all and packer_ok
            time.sleep(1)
        categories = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            cat = str(it.get('category', '')).strip()
            if not cat:
                found = _category_for_item_name(it.get('name', ''))
                if found:
                    it['category'] = found
                    cat = found
            cat = cat or 'Uncategorized'
            categories.setdefault(cat, []).append(it)
            if _is_sushi_category_name(cat) or _is_sushi_item(it):
                sushi_items.append(copy.deepcopy(it))
            else:
                kitchen_items.append(copy.deepcopy(it))

        # Sushi -> Kitchen add-ons
        addon_quantities = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            cat = str(it.get('category', '')).strip()
            if not cat:
                found = _category_for_item_name(it.get('name', ''))
                if found:
                    it['category'] = found
                    cat = found
            if not cat:
                continue
            cat_l = cat.lower()
            if not (('roll' in cat_l) or cat_l.startswith('sushi')):
                continue
            name_l = str(it.get('name', '')).lower()
            try:
                qty = int(it.get('quantity', 1) or 1)
            except Exception:
                qty = 1
            if qty <= 0:
                continue
            if any(token in name_l for token in (
                'shrimp tempura roll',
                'new york roll',
                'tiger roll',
                'godzilla roll',
                'caterpillar',
                'maki combo',
                'dragon roll',
            )):
                addon_quantities['Tempura Shrimps'] = addon_quantities.get('Tempura Shrimps', 0) + (2 * qty)
            if 'sweet potato roll' in name_l:
                addon_quantities['Fried Potato'] = addon_quantities.get('Fried Potato', 0) + qty
            if 'asparagus roll' in name_l:
                addon_quantities['Asparagus'] = addon_quantities.get('Asparagus', 0) + qty
            if 'spider roll' in name_l:
                addon_quantities['Crab'] = addon_quantities.get('Crab', 0) + qty
            if 'caterpillar' in name_l:
                addon_quantities['Crab'] = addon_quantities.get('Crab', 0) + qty
            if 'hot sexy mama' in name_l:
                addon_quantities['Sushi Fried Calamari'] = addon_quantities.get('Sushi Fried Calamari', 0) + qty

        if addon_quantities:
            if kitchen_printer:
                kitchen_order = {
                    'number': order_data.get('number'),
                    'tableNumber': order_data.get('tableNumber', 'N/A'),
                    'items': [
                        {
                            'name': addon_name,
                            'nameZh': _name_zh_for_item_name(addon_name),
                            'quantity': addon_qty,
                            'price': 0.0,
                            'selectedOptions': [],
                            'comment': ''
                        }
                        for addon_name, addon_qty in addon_quantities.items()
                        if addon_qty > 0
                    ],
                    'universalComment': 'SUSHI ADD-ON - NOT APPETIZER',
                }
                kitchen_items.extend(copy.deepcopy(kitchen_order['items']))
                kitchen_comment = kitchen_order['universalComment']
            else:
                kitchen_comment = ""
        else:
            kitchen_comment = ""

        if sushi_items and sushi_printer:
            sushi_order = {
                'number': order_data.get('number'),
                'tableNumber': order_data.get('tableNumber', 'N/A'),
                'items': sushi_items,
                'universalComment': "",
            }
            ok = print_kitchen_ticket(sushi_order, copy_info='Sushi', printer_name=sushi_printer)
            printed_any = printed_any or ok
            printed_all = printed_all and ok
            time.sleep(1)

        if kitchen_items and kitchen_printer:
            kitchen_order = {
                'number': order_data.get('number'),
                'tableNumber': order_data.get('tableNumber', 'N/A'),
                'items': kitchen_items,
                'universalComment': kitchen_comment,
            }
            ok = print_kitchen_ticket(kitchen_order, copy_info='Kitchen', printer_name=kitchen_printer)
            printed_any = printed_any or ok
            printed_all = printed_all and ok
            time.sleep(1)

        if not printed_any:
            printed_all = False
        status = 'Yes (routed)' if printed_any and printed_all else ('Partial (routed)' if printed_any else 'No (routed)')
        return log_order_to_csv(order_data, skip_print=True, printed_status_override=status)
    else:
        printer_name = order_data.get('printer') or order_data.get('printer_name')
        return log_order_to_csv(order_data, printer_name=printer_name)

# --- THIS IS THE MAIN MODIFIED FUNCTION ---
def _print_raw_job_to_printer(job_path, printer_name):
    if not job_path or not printer_name:
        return False
    ext = os.path.splitext(str(job_path))[1].lower()
    if ext != ".bin":
        return False
    try:
        with open(job_path, "rb") as f:
            raw_data = f.read()
    except Exception:
        return False
    if not raw_data:
        return False
    hprinter = None
    try:
        hprinter = win32print.OpenPrinter(printer_name)
        try:
            doc_name = f"OriginalRaw_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            win32print.StartDocPrinter(hprinter, 1, (doc_name, None, "RAW"))
            win32print.StartPagePrinter(hprinter)
            win32print.WritePrinter(hprinter, raw_data)
            win32print.EndPagePrinter(hprinter)
            win32print.EndDocPrinter(hprinter)
        finally:
            win32print.ClosePrinter(hprinter)
        return True
    except Exception as e:
        app.logger.error(f"Raw passthrough print error: {str(e)}")
        if hprinter:
            try:
                win32print.ClosePrinter(hprinter)
            except Exception:
                pass
        return False

def print_kitchen_ticket(order_data, copy_info="", original_timestamp_str=None, printer_name=None):
    hprinter = None
    try:
        if str(copy_info or "").strip().lower() == "original":
            raw_job_path = normalize_print_text(order_data.get('_captured_job_path', ''))
            target_printer = printer_name or PRINTER_NAME
            if raw_job_path and _print_raw_job_to_printer(raw_job_path, target_printer):
                return True
        ticket_content = bytearray()
        ticket_lines = []
        ticket_content += InitializePrinter
        
        NORMAL_FONT_LINE_WIDTH = 42
        SMALL_FONT_LINE_WIDTH = 56 

        # --- Header Section (As per app DUMMY.py) ---
        ticket_content += AlignCenter + SelectFontA + DoubleHeightWidth + BoldOn
        restaurant_name = "Sakura" 
        ticket_content += to_bytes(restaurant_name + "\n")
        ticket_lines.append(restaurant_name)
        ticket_content += BoldOff 
        
        ticket_content += AlignCenter + SelectFontA + NormalText
        header_text = "Kitchen Order"
        if copy_info:
             header_text += f" - {copy_info.upper()}"
        ticket_content += to_bytes(header_text + "\n")
        ticket_lines.append(header_text)
        
        ticket_content += AlignLeft 
        
        ticket_content += SelectFontA + DoubleHeightWidth + BoldOn
        order_num_text = f"Order #: {order_data.get('number', 'N/A')}"
        ticket_content += to_bytes(order_num_text + "\n")
        ticket_lines.append(order_num_text)
        ticket_content += BoldOff

        ticket_content += SelectFontA + NormalText
        time_to_display = original_timestamp_str if original_timestamp_str else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ticket_content += to_bytes(f"Time: {time_to_display}\n")
        ticket_lines.append(f"Time: {time_to_display}")

        if str(copy_info or "").strip().lower() == "original":
            platform = normalize_print_text(first_value(
                order_data.get('orderSource'),
                order_data.get('platform'),
                order_data.get('marketplace'),
                order_data.get('tableNumber'),
            ))
            customer_name = normalize_print_text(first_value(
                order_data.get('customerName'),
                order_data.get('customer_name'),
                order_data.get('consumer', {}).get('name') if isinstance(order_data.get('consumer'), dict) else None,
            ))
            external_id = normalize_print_text(first_value(
                order_data.get('uberOrderId'),
                order_data.get('doorDashOrderId'),
                order_data.get('grubhubOrderId'),
                order_data.get('marketplaceOrderId'),
            ))
            ticket_content += SelectFontA + DoubleHeight + BoldOn
            if platform:
                line = f"Platform: {platform}"
                ticket_content += to_bytes(line + "\n")
                ticket_lines.append(line)
            if customer_name:
                line = f"Customer: {customer_name}"
                ticket_content += to_bytes(line + "\n")
                ticket_lines.append(line)
            if external_id:
                line = f"Order ID: {external_id}"
                ticket_content += to_bytes(line + "\n")
                ticket_lines.append(line)
            ticket_content += NormalText + BoldOff
        
        ticket_content += to_bytes("-" * NORMAL_FONT_LINE_WIDTH + "\n")
        ticket_lines.append("-" * NORMAL_FONT_LINE_WIDTH)
        
        # --- Items Section (Logic from app DUMMY.py) ---
        for item_idx, item in enumerate(order_data.get('items', [])):
            item_quantity = item.get('quantity', 0)
            item_name_orig = item.get('name', 'Unknown Item')
            item_name_zh = normalize_print_text(first_value(item.get('nameZh'), _name_zh_for_item_name(item_name_orig)))
            selected_options = item.get('selectedOptions', [])

            left_side = f"{item_quantity}x {item_name_orig}"
            ticket_content += SelectFontA + DoubleHeightWidth + BoldOn
            DOUBLE_WIDTH_LINE_CHARS = NORMAL_FONT_LINE_WIDTH // 2
            wrapped_name_lines = word_wrap_text(left_side, DOUBLE_WIDTH_LINE_CHARS)
            for line in wrapped_name_lines:
                ticket_content += to_bytes(line + "\n")
                ticket_lines.append(line)
            ticket_content += NormalText + BoldOff
            if item_name_zh:
                wrapped_zh_lines = word_wrap_text(f"  {item_name_zh}", NORMAL_FONT_LINE_WIDTH, initial_indent="  ", subsequent_indent="  ")
                for zh_line in wrapped_zh_lines:
                    ticket_lines.append(zh_line)

            # Print selected options (indented)
            if selected_options and isinstance(selected_options, list):
                for option in selected_options:
                    option_name = option.get('name', 'N/A')
                    option_line = f"  -> {option_name}"
                    wrapped_option_lines = word_wrap_text(option_line, NORMAL_FONT_LINE_WIDTH, initial_indent="  ", subsequent_indent="    ") 
                    for opt_line_part in wrapped_option_lines:
                        ticket_content += to_bytes(opt_line_part + "\n")
                        ticket_lines.append(opt_line_part)

            # Print item comment (indented)
            item_comment = normalize_print_text(item.get('comment', ''))
            if item_comment:
                ticket_content += SelectFontA + DoubleHeight + BoldOn
                wrapped_comments = word_wrap_text(f"NOTE: {item_comment}", NORMAL_FONT_LINE_WIDTH, initial_indent="    ", subsequent_indent="    ")
                for comment_line in wrapped_comments:
                     ticket_content += to_bytes(comment_line + "\n")
                     ticket_lines.append(comment_line)
                ticket_content += NormalText + BoldOff
            
            # Add a separator between items
            if item_idx < len(order_data.get('items', [])) - 1:
                ticket_content += to_bytes("." * NORMAL_FONT_LINE_WIDTH + "\n")
                ticket_lines.append("." * NORMAL_FONT_LINE_WIDTH)

        # --- Footer Section (As per app DUMMY.py) ---
        ticket_content += SelectFontA + NormalText
        ticket_content += to_bytes("-" * NORMAL_FONT_LINE_WIDTH + "\n\n") 
        ticket_lines.append("-" * NORMAL_FONT_LINE_WIDTH)
        
        universal_comment = normalize_print_text(order_data.get('universalComment', ''))
        if universal_comment:
            ticket_content += to_bytes("=" * NORMAL_FONT_LINE_WIDTH + "\n")
            ticket_lines.append("=" * NORMAL_FONT_LINE_WIDTH)
            ticket_content += SelectFontB + BoldOn
            ticket_content += to_bytes("CUSTOMER NOTE\n")
            ticket_lines.append("ORDER NOTES")
            ticket_content += BoldOff
            wrapped_universal_comment_lines = word_wrap_text(universal_comment, SMALL_FONT_LINE_WIDTH, initial_indent="", subsequent_indent="") 
            for line in wrapped_universal_comment_lines:
                ticket_content += to_bytes(line + "\n")
                ticket_lines.append(line)
            ticket_content += SelectFontA + NormalText
            ticket_content += to_bytes("\n")
            ticket_lines.append("")
        
        ticket_content += to_bytes("\n")
        ticket_lines.append("")
        ticket_content += AlignCenter + SelectFontB
        disclaimer_text = "This is not a legal receipt and is for informational purposes only."
        wrapped_disclaimer_lines = word_wrap_text(disclaimer_text, SMALL_FONT_LINE_WIDTH)
        for line in wrapped_disclaimer_lines:
            ticket_content += to_bytes(line + "\n")
            ticket_lines.append(line)

        ticket_content += SelectFontA + AlignLeft
            
        ticket_content += to_bytes("\n\n\n\n") 
        ticket_content += FullCut

        target_printer = printer_name or PRINTER_NAME
        doc_name = f"Order_{order_data.get('number', 'N/A')}_Ticket_{copy_info.replace(' ','_')}"
        if str(os.environ.get("VIRTUAL_PRINT", "")).strip().lower() in ("1", "true", "yes", "on"):
            os.makedirs(VIRTUAL_PRINTS_DIR, exist_ok=True)
            safe_printer = re.sub(r'[^A-Za-z0-9._-]+', '_', str(target_printer or 'default')).strip('_') or 'default'
            safe_doc = re.sub(r'[^A-Za-z0-9._-]+', '_', str(doc_name or 'ticket')).strip('_') or 'ticket'
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            preview_path = os.path.join(VIRTUAL_PRINTS_DIR, f"{stamp}__{safe_printer}__{safe_doc}.txt")
            with open(preview_path, 'w', encoding='utf-8') as f:
                f.write(f"TARGET_PRINTER: {target_printer}\n")
                f.write(f"DOC_NAME: {doc_name}\n")
                f.write(f"COPY_INFO: {copy_info}\n")
                f.write("=" * 48 + "\n")
                f.write("\n".join(ticket_lines))
                f.write("\n")
            app.logger.info("Virtual print saved to %s", preview_path)
            return True
        if any(any(ord(ch) > 127 for ch in line) for line in ticket_lines):
            hdc = win32ui.CreateDC()
            try:
                hdc.CreatePrinterDC(target_printer)
                dpi_y = hdc.GetDeviceCaps(win32con.LOGPIXELSY)
                page_height = hdc.GetDeviceCaps(win32con.VERTRES)
                margin = max(int(dpi_y * 0.3), 60)
                line_height = max(int(dpi_y * 0.22), 28)
                font = win32ui.CreateFont({
                    'name': 'Microsoft YaHei UI',
                    'height': -max(int(dpi_y * 0.16), 22),
                    'weight': 700,
                })
                hdc.StartDoc(doc_name)
                hdc.StartPage()
                hdc.SelectObject(font)
                y = margin
                for line in ticket_lines:
                    if y + line_height > page_height - margin:
                        hdc.EndPage()
                        hdc.StartPage()
                        hdc.SelectObject(font)
                        y = margin
                    hdc.TextOut(margin, y, line)
                    y += line_height
                hdc.EndPage()
                hdc.EndDoc()
                return True
            finally:
                try:
                    hdc.DeleteDC()
                except Exception:
                    pass
        hprinter = win32print.OpenPrinter(target_printer)
        try:
            win32print.StartDocPrinter(hprinter, 1, (doc_name, None, "RAW"))
            win32print.StartPagePrinter(hprinter)
            win32print.WritePrinter(hprinter, bytes(ticket_content))
            win32print.EndPagePrinter(hprinter)
            win32print.EndDocPrinter(hprinter)
        finally:
            win32print.ClosePrinter(hprinter)
        
        return True

    except Exception as e:
        app.logger.error(f"Printing error (ESC/POS): {str(e)}")
        if hprinter:
             win32print.ClosePrinter(hprinter)
        return False
        
def log_order_to_csv(order_data, printer_name=None, skip_print=False, printed_status_override=None):
    try:
        os.makedirs(CSV_DIR, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = os.path.join(CSV_DIR, f"orders_{date_str}.csv")

        fieldnames = [
            'order_number', 'table_number', 'timestamp', 'items_summary', 
            'universal_comment', 'order_total', 'printed_status', 'items_json'
        ]

        printed_status = 'No'
        if printed_status_override is not None:
            printed_status = printed_status_override
        elif not skip_print:
            app.logger.info(f"Printing receipt for order #{order_data.get('number', 'N/A')}")
            print_success1 = print_kitchen_ticket(order_data, copy_info="Kitchen", printer_name=printer_name)
            time.sleep(1)  # Small delay for the printer queue
            print_success2 = print_kitchen_ticket(order_data, copy_info="Customer", printer_name=printer_name)

            if print_success1 and print_success2:
                printed_status = 'Yes (2 copies)'
            elif print_success1 or print_success2:
                printed_status = 'Partial (1 copy)'
            else:
                printed_status = 'No'

        existing_rows = []
        file_exists = os.path.exists(filename)
        if file_exists:
            with open(filename, 'r', newline='', encoding='utf-8') as f_read:
                reader = csv.DictReader(f_read)
                for row in reader:
                    if row.get('order_number', '').lower() != 'total':
                        existing_rows.append(row)
        
        items_summary_parts = []
        for item in order_data.get('items', []):
            option_summary_parts = []
            selected_options = item.get('selectedOptions', [])
            if selected_options and isinstance(selected_options, list):
                for option in selected_options:
                    option_name = option.get('name', '')
                    option_summary_parts.append(option_name)
            
            summary_part = f"{item.get('quantity', 0)}x {item.get('name', 'N/A')}"
            if option_summary_parts:
                summary_part += f" (Opts: {', '.join(option_summary_parts)})"

            item_comment = normalize_print_text(item.get('comment', ''))
            if item_comment:
                 summary_part += f" (Note: {item_comment})"
            items_summary_parts.append(summary_part)

        items_summary_str = " | ".join(items_summary_parts)

        new_row = {
            'order_number': order_data.get('number', 'N/A'),
            'table_number': order_data.get('tableNumber', 'N/A'),
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'items_summary': items_summary_str,
            'items_json': json.dumps(order_data.get('items', [])),
            'universal_comment': normalize_print_text(order_data.get('universalComment', '')),
            'order_total': '',
            'printed_status': printed_status
        }
        existing_rows.append(new_row)

        with open(filename, 'w', newline='', encoding='utf-8') as f_write:
            writer = csv.DictWriter(f_write, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(existing_rows)
            
        return True
    except Exception as e:
        app.logger.error(f"CSV logging error: {str(e)}")
        return False

@app.route('/api/orders', methods=['POST'])
def handle_order():
    order_data = normalize_order_data(request.json)
    if not order_data or 'items' not in order_data:
        return jsonify({"status": "error", "message": "Invalid order data"}), 400
    try:
        if 'number' not in order_data: 
            order_data['number'] = int(datetime.now().timestamp() % 10000) 
        success = handle_order_internal(order_data)
        
        if success:
            return jsonify({"status": "success", "order_number": order_data['number']})
        else: 
            return jsonify({"status": "error", "message": "Failed to process order (log/print)"}), 500
    except Exception as e: 
        app.logger.error(f"Error in handle_order: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webhooks/uber-eats', methods=['POST'])
def uber_eats_webhook():
    body = request.get_data(cache=False)
    skip_sig = _uber_env_bool("UBEREATS_SKIP_SIGNATURE_VERIFY")
    secret = os.environ.get("UBEREATS_CLIENT_SECRET", "").strip()
    sig = request.headers.get("X-Uber-Signature")
    if secret and not skip_sig:
        if not ubereats.verify_webhook_signature(body, sig, secret):
            app.logger.warning("Uber webhook: invalid X-Uber-Signature")
            return make_response("", 401)
    elif not secret and not skip_sig:
        app.logger.warning("Uber webhook: UBEREATS_CLIENT_SECRET missing; set UBEREATS_SKIP_SIGNATURE_VERIFY=1 for local dev only")
        return make_response("", 403)
    try:
        payload = json.loads(body.decode("utf-8") if body else "{}")
    except Exception:
        app.logger.warning("Uber webhook: invalid JSON body")
        return make_response("", 400)
    if isinstance(payload, dict) and payload.get("event_type") == "orders.notification":
        if not os.environ.get("UBEREATS_ACCESS_TOKEN", "").strip():
            app.logger.error("Uber webhook: UBEREATS_ACCESS_TOKEN not set (Uber will retry)")
            return make_response("", 503)
    threading.Thread(target=_uber_webhook_worker, args=(payload,), daemon=True).start()
    return make_response("", 200)

@app.route('/webhooks/doordash', methods=['POST'])
def doordash_webhook():
    body = request.get_data(cache=False)
    try:
        payload = json.loads(body.decode("utf-8") if body else "{}")
    except Exception:
        app.logger.warning("DoorDash webhook: invalid JSON body")
        return make_response("", 400)
    cfg = _load_doordash_store_config()
    expected_token = str(first_value(cfg.get("webhookToken"), os.environ.get("DOORDASH_WEBHOOK_TOKEN", ""))).strip()
    if expected_token:
        auth_header = str(request.headers.get("Authorization", "")).strip()
        raw_token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else auth_header
        if raw_token != expected_token:
            app.logger.warning("DoorDash webhook: invalid Authorization token")
            return make_response("", 401)
    threading.Thread(target=_doordash_webhook_worker, args=(payload,), daemon=True).start()
    return make_response("", 202)

# Uber Eats (test) ingest: accept external JSON, convert to internal order, print+log
@app.route('/api/ingest', methods=['POST'])
def ingest_external_order():
    payload = request.json
    if not payload:
        return jsonify({"status": "error", "message": "Invalid payload"}), 400
    order_data = normalize_order_data({
        'number': payload.get('number') or payload.get('order_id') or payload.get('id') or int(datetime.now().timestamp() % 100000),
        'tableNumber': payload.get('tableNumber', 'N/A'),
        'items': payload.get('items', []),
        'universalComment': first_value(
            payload.get('universalComment'),
            payload.get('note'),
            payload.get('notes'),
            payload.get('special_request'),
            payload.get('specialRequest'),
            payload.get('customer_note'),
            payload.get('customerNote')
        ),
    })
    if not order_data.get('items'):
        return jsonify({"status": "error", "message": "No valid items"}), 400
    entry = enqueue_incoming(order_data)
    return jsonify({"status": "success", "incoming_id": entry['id'], "order_number": order_data.get('number')}), 200

# --- NEW ENDPOINTS FOR REPRINT FUNCTIONALITY ---

@app.route('/api/todays_orders_for_reprint', methods=['GET'])
def get_todays_orders_for_reprint():
    try:
        today_date_str = datetime.now().strftime("%Y-%m-%d")
        filename = os.path.join(CSV_DIR, f"orders_{today_date_str}.csv")
        
        if not os.path.exists(filename):
            return jsonify([])

        orders_for_reprint = []
        with open(filename, 'r', newline='', encoding='utf-8') as f_read:
            reader = csv.DictReader(f_read)
            for row in reader:
                if row.get('order_number', '').lower() not in ['total', ''] and row.get('items_json'): 
                    orders_for_reprint.append({
                        'order_number': row.get('order_number'),
                        'table_number': row.get('table_number', 'N/A'),
                        'timestamp': row.get('timestamp')
                    })
        orders_for_reprint.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        return jsonify(orders_for_reprint)

    except Exception as e:
        app.logger.error(f"Error fetching today's orders for reprint: {str(e)}")
        return jsonify({"status": "error", "message": f"Could not fetch today's orders: {str(e)}"}), 500

@app.route('/api/history_orders', methods=['GET'])
def get_history_orders():
    try:
        days_raw = request.args.get('days', '7')
        selected_date = str(request.args.get('date', '')).strip()
        try:
            days = int(days_raw)
        except Exception:
            days = 7
        days = max(1, min(days, 30))

        today = datetime.now().date()
        available_dates = []
        dates_to_read = []
        for offset in range(days):
            day = today - timedelta(days=offset)
            day_str = day.strftime('%Y-%m-%d')
            available_dates.append(day_str)
            dates_to_read.append((day, day_str))

        if not selected_date or selected_date not in available_dates:
            selected_date = available_dates[0]

        history_orders = []
        for day, day_str in dates_to_read:
            if day_str != selected_date:
                continue
            filename = os.path.join(CSV_DIR, f"orders_{day.strftime('%Y-%m-%d')}.csv")
            if not os.path.exists(filename):
                continue
            with open(filename, 'r', newline='', encoding='utf-8') as f_read:
                reader = csv.DictReader(f_read)
                for row in reader:
                    order_number = str(row.get('order_number', '')).strip()
                    if not order_number or order_number.lower() == 'total':
                        continue
                    items_json = row.get('items_json', '[]')
                    try:
                        items = json.loads(items_json) if items_json else []
                    except Exception:
                        items = []
                    history_orders.append({
                        'order_number': order_number,
                        'table_number': row.get('table_number', 'N/A'),
                        'timestamp': row.get('timestamp', ''),
                        'items_summary': row.get('items_summary', ''),
                        'items': items if isinstance(items, list) else [],
                        'universal_comment': row.get('universal_comment', ''),
                        'printed_status': row.get('printed_status', ''),
                        'order_total': row.get('order_total', ''),
                    })

        history_orders.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        return jsonify({
            'selected_date': selected_date,
            'available_dates': available_dates,
            'orders': history_orders,
        })
    except Exception as e:
        app.logger.error(f"Error fetching history orders: {str(e)}")
        return jsonify({"status": "error", "message": f"Could not fetch history orders: {str(e)}"}), 500


@app.route('/api/reprint_order', methods=['POST'])
def reprint_order_endpoint():
    data = request.json
    order_number_to_reprint = data.get('order_number')

    if not order_number_to_reprint:
        return jsonify({"status": "error", "message": "Order number is required for reprint."}), 400

    try:
        today_date_str = datetime.now().strftime("%Y-%m-%d")
        filename = os.path.join(CSV_DIR, f"orders_{today_date_str}.csv")

        if not os.path.exists(filename):
            return jsonify({"status": "error", "message": f"No orders found for today."}), 404

        found_order_row = None
        with open(filename, 'r', newline='', encoding='utf-8') as f_read:
            reader = csv.DictReader(f_read)
            for row in reader:
                if row.get('order_number') == str(order_number_to_reprint):
                    found_order_row = row
                    break
        
        if not found_order_row:
            return jsonify({"status": "error", "message": f"Order #{order_number_to_reprint} not found in today's records."}), 404

        items_list_str = found_order_row.get('items_json', '[]')
        items_list = json.loads(items_list_str)
        
        reprint_order_data = {
            'number': found_order_row.get('order_number'),
            'tableNumber': found_order_row.get('table_number', 'N/A'),
            'items': items_list,
            'universalComment': found_order_row.get('universal_comment', '')
        }
        original_timestamp = found_order_row.get('timestamp')

        app.logger.info(f"Attempting to reprint order #{order_number_to_reprint}")

        # Reprint the receipt twice, with a simple "Reprint" header
        reprint_success1 = print_kitchen_ticket(reprint_order_data, 
                                               copy_info="Reprint", 
                                               original_timestamp_str=original_timestamp)
        time.sleep(1) # Small delay for the printer
        reprint_success2 = print_kitchen_ticket(reprint_order_data, 
                                               copy_info="Reprint", 
                                               original_timestamp_str=original_timestamp)
        
        reprint_success = reprint_success1 and reprint_success2
        
        if reprint_success:
            return jsonify({"status": "success", "message": f"Order #{order_number_to_reprint} REPRINTED successfully (2 copies)."}), 200
        else:
            return jsonify({"status": "error", "message": f"Failed to reprint Order #{order_number_to_reprint}. Check printer."}), 500

    except json.JSONDecodeError:
        app.logger.error(f"Error decoding item data for order #{order_number_to_reprint} during reprint.")
        return jsonify({"status": "error", "message": f"Corrupted item data for order #{order_number_to_reprint}. Cannot reprint."}), 500
    except Exception as e:
        app.logger.error(f"Error reprinting order #{order_number_to_reprint}: {str(e)}")
        return jsonify({"status": "error", "message": f"Could not reprint order #{order_number_to_reprint}: {str(e)}"}), 500


if __name__ == '__main__':
    try:
        import win32print
    except ImportError:
        app.logger.error("pywin32 not found. Please ensure it is installed (pip install pywin32).")
    
    app.logger.info(f"CSV files will be saved to: {CSV_DIR}")
    if PRINTER_NAME: 
        app.logger.info(f"Attempting to use printer: {PRINTER_NAME}")
    else:
        app.logger.warning("Warning: PRINTER_NAME is not set. Printing will likely fail.")

    os.makedirs(PRINT_JOBS_DIR, exist_ok=True)
    if str(os.environ.get("VIRTUAL_PRINT", "")).strip().lower() in ("1", "true", "yes", "on"):
        os.makedirs(VIRTUAL_PRINTS_DIR, exist_ok=True)
        app.logger.info("Virtual print preview enabled: %s", VIRTUAL_PRINTS_DIR)

    def _enqueue_from_raw_job(data: bytes, peer: str, job_path: str | None = None):
        text = print_capture._bytes_to_text(data)
        source = print_capture.detect_order_source(text, fallback=f"RAW9100 {peer}")
        order_data = print_capture.parse_receipt_text_to_order(
            text=text,
            source=source,
        )
        if job_path:
            order_data['_captured_job_path'] = job_path
        if not str(first_value(order_data.get('printer'), order_data.get('printer_name'))).strip() and PRINTER_NAME:
            order_data['printer'] = PRINTER_NAME
        enqueue_incoming(order_data)

    raw_port = int(os.environ.get("PRINT_CAPTURE_RAW_PORT", "9100") or "9100")
    raw_host = str(os.environ.get("PRINT_CAPTURE_RAW_HOST", "0.0.0.0") or "0.0.0.0")
    try:
        recv = print_capture.Raw9100Receiver(
            host=raw_host,
            port=raw_port,
            jobs_dir=PRINT_JOBS_DIR,
            on_job=_enqueue_from_raw_job,
        )
        recv.start()
        app.logger.info("RAW9100 receiver listening on %s:%s", raw_host, raw_port)
    except Exception as e:
        app.logger.error("RAW9100 receiver failed: %s", e)

    ipp_port = int(os.environ.get("PRINT_CAPTURE_IPP_PORT", "8631") or "8631")
    ipp_host = str(os.environ.get("PRINT_CAPTURE_IPP_HOST", "0.0.0.0") or "0.0.0.0")
    mdns_host = str(os.environ.get("PRINT_CAPTURE_MDNS_HOST", "") or "").strip() or print_capture.detect_lan_ip()
    ipp_enabled = print_capture.start_ipp_receiver_if_available(PRINT_JOBS_DIR, ipp_host, ipp_port, public_host=mdns_host)
    if ipp_enabled:
        app.logger.info("IPP receiver listening on %s:%s", ipp_host, ipp_port)
        svc_name = str(os.environ.get("PRINT_CAPTURE_AIRPRINT_NAME", "KitchenPrintPro") or "KitchenPrintPro")
        app.logger.info("AirPrint advertise IP %s interface_index=%s", mdns_host, print_capture.get_interface_index_for_ip(mdns_host))
        native_airprint = print_capture.start_native_airprint_mdns_if_available(svc_name, mdns_host, ipp_port)
        if native_airprint:
            app.logger.info("Native AirPrint mDNS advertised as %s on %s", svc_name, mdns_host)
        else:
            app.logger.warning("AirPrint mDNS not advertised")
    else:
        app.logger.warning("IPP receiver not started (install ippserver)")

    def _watch_ipp_pdfs():
        recent_hashes = {}
        while True:
            try:
                for name in os.listdir(PRINT_JOBS_DIR):
                    if not name.lower().endswith((".pdf", ".urf", ".jpg", ".jpeg", ".bin")):
                        continue
                    if name.lower().startswith("raw9100_") and name.lower().endswith(".bin"):
                        continue
                    path = os.path.join(PRINT_JOBS_DIR, name)
                    marker = path + ".queued"
                    if os.path.exists(marker):
                        continue
                    digest = ""
                    try:
                        digest = print_capture.sha1_file(path)
                    except Exception:
                        digest = ""
                    now_ts = time.time()
                    recent_hashes = {
                        k: v for k, v in recent_hashes.items()
                        if now_ts - v < 45
                    }
                    if digest and digest in recent_hashes:
                        with open(marker, "w", encoding="utf-8") as f:
                            f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                        continue
                    order_data, extracted_text = print_capture.build_order_from_saved_job(path)
                    text_path = path + ".txt"
                    if extracted_text and not os.path.exists(text_path):
                        with open(text_path, "w", encoding="utf-8", errors="replace") as f:
                            f.write(extracted_text)
                    enqueue_incoming(order_data)
                    if digest:
                        recent_hashes[digest] = now_ts
                    with open(marker, "w", encoding="utf-8") as f:
                        f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            except Exception:
                pass
            time.sleep(1.0)

    threading.Thread(target=_watch_ipp_pdfs, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
