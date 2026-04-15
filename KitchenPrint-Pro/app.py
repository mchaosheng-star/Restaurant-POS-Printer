# app.py
from flask import Flask, request, jsonify, send_from_directory, make_response
from datetime import datetime
import csv
import os
import win32print # type: ignore
import tempfile
import time
import json
import logging
import threading

import ubereats

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

UBER_WEBHOOK_LOCK = threading.Lock()
UBER_WEBHOOK_EVENT_IDS = set()

# Menu cache for category lookup
_MENU_CACHE = None
_MENU_CACHE_MTIME = None

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
                'quantity': qty,
                'price': price,
                'selectedOptions': it.get('selectedOptions', []) if isinstance(it.get('selectedOptions', []), list) else [],
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

def _category_for_item_name(item_name):
    name_l = str(item_name or '').strip().lower()
    if not name_l:
        return None
    menu = _load_menu_cached()
    if not isinstance(menu, dict):
        return None
    for cat, items in menu.items():
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            if str(it.get('name', '')).strip().lower() == name_l:
                return str(cat)
    return None

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
        return jsonify({"status": "success"}), 200
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

def handle_order_internal(order_data):
    category_printers = order_data.get('categoryPrinters') or {}
    special_printers = order_data.get('specialPrinters') or {}
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
    if isinstance(category_printers, dict) and category_printers:
        items = order_data.get('items', [])
        printed_any = False
        printed_all = True
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
            if 'shrimp' in name_l:
                addon_quantities['Tempura Shrimps'] = addon_quantities.get('Tempura Shrimps', 0) + (2 * qty)
            if 'spider roll' in name_l:
                addon_quantities['Crab'] = addon_quantities.get('Crab', 0) + qty
            if 'hot sexy mama' in name_l:
                addon_quantities['Fried Calamari'] = addon_quantities.get('Fried Calamari', 0) + qty

        if addon_quantities:
            kitchen_printer = (
                special_printers.get('sushi_addon')
                or category_printers.get('Entree')
                or category_printers.get('Teppanyaki')
                or category_printers.get('entree')
                or category_printers.get('teppanyaki')
                or None
            )
            if kitchen_printer:
                kitchen_order = {
                    'number': order_data.get('number'),
                    'tableNumber': order_data.get('tableNumber', 'N/A'),
                    'items': [
                        {
                            'name': addon_name,
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
                ok = print_kitchen_ticket(kitchen_order, copy_info='Kitchen Add-on', printer_name=kitchen_printer)
                printed_any = printed_any or ok
                printed_all = printed_all and ok
                time.sleep(1)

        for cat, cat_items in categories.items():
            printer = category_printers.get(cat) or category_printers.get(cat.lower()) or None
            if not printer:
                continue
            cat_order = {
                'number': order_data.get('number'),
                'tableNumber': order_data.get('tableNumber', 'N/A'),
                'items': cat_items,
                'universalComment': normalize_print_text(order_data.get('universalComment', '')),
            }
            ok = print_kitchen_ticket(cat_order, copy_info=cat, printer_name=printer)
            printed_any = printed_any or ok
            printed_all = printed_all and ok
            time.sleep(1)

        status = 'Yes (routed)' if printed_all else ('Partial (routed)' if printed_any else 'No (routed)')
        return log_order_to_csv(order_data, skip_print=True, printed_status_override=status)
    else:
        printer_name = order_data.get('printer') or order_data.get('printer_name')
        return log_order_to_csv(order_data, printer_name=printer_name)

# --- THIS IS THE MAIN MODIFIED FUNCTION ---
def print_kitchen_ticket(order_data, copy_info="", original_timestamp_str=None, printer_name=None):
    hprinter = None
    try:
        ticket_content = bytearray()
        ticket_content += InitializePrinter
        
        NORMAL_FONT_LINE_WIDTH = 42
        SMALL_FONT_LINE_WIDTH = 56 

        # --- Header Section (As per app DUMMY.py) ---
        ticket_content += AlignCenter + SelectFontA + DoubleHeightWidth + BoldOn
        restaurant_name = "Sakura" 
        ticket_content += to_bytes(restaurant_name + "\n")
        ticket_content += BoldOff 
        
        ticket_content += AlignCenter + SelectFontA + NormalText
        header_text = "Kitchen Order"
        if copy_info:
             header_text += f" - {copy_info.upper()}"
        ticket_content += to_bytes(header_text + "\n")
        
        ticket_content += AlignLeft 
        
        ticket_content += SelectFontA + DoubleHeightWidth + BoldOn
        order_num_text = f"Order #: {order_data.get('number', 'N/A')}"
        ticket_content += to_bytes(order_num_text + "\n")
        ticket_content += BoldOff

        ticket_content += SelectFontA + NormalText
        time_to_display = original_timestamp_str if original_timestamp_str else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ticket_content += to_bytes(f"Time: {time_to_display}\n")
        
        ticket_content += to_bytes("-" * NORMAL_FONT_LINE_WIDTH + "\n")
        
        # --- Items Section (Logic from app DUMMY.py) ---
        for item_idx, item in enumerate(order_data.get('items', [])):
            item_quantity = item.get('quantity', 0)
            item_name_orig = item.get('name', 'Unknown Item')
            selected_options = item.get('selectedOptions', [])

            left_side = f"{item_quantity}x {item_name_orig}"
            ticket_content += SelectFontA + DoubleHeightWidth + BoldOn
            DOUBLE_WIDTH_LINE_CHARS = NORMAL_FONT_LINE_WIDTH // 2
            wrapped_name_lines = word_wrap_text(left_side, DOUBLE_WIDTH_LINE_CHARS)
            for line in wrapped_name_lines:
                ticket_content += to_bytes(line + "\n")
            ticket_content += NormalText + BoldOff

            # Print selected options (indented)
            if selected_options and isinstance(selected_options, list):
                for option in selected_options:
                    option_name = option.get('name', 'N/A')
                    option_line = f"  -> {option_name}"
                    wrapped_option_lines = word_wrap_text(option_line, NORMAL_FONT_LINE_WIDTH, initial_indent="  ", subsequent_indent="    ") 
                    for opt_line_part in wrapped_option_lines:
                        ticket_content += to_bytes(opt_line_part + "\n")

            # Print item comment (indented)
            item_comment = normalize_print_text(item.get('comment', ''))
            if item_comment:
                ticket_content += SelectFontA + DoubleHeight + BoldOn
                wrapped_comments = word_wrap_text(f"NOTE: {item_comment}", NORMAL_FONT_LINE_WIDTH, initial_indent="    ", subsequent_indent="    ")
                for comment_line in wrapped_comments:
                     ticket_content += to_bytes(comment_line + "\n")
                ticket_content += NormalText + BoldOff
            
            # Add a separator between items
            if item_idx < len(order_data.get('items', [])) - 1:
                ticket_content += to_bytes("." * NORMAL_FONT_LINE_WIDTH + "\n")

        # --- Footer Section (As per app DUMMY.py) ---
        ticket_content += SelectFontA + NormalText
        ticket_content += to_bytes("-" * NORMAL_FONT_LINE_WIDTH + "\n\n") 
        
        universal_comment = normalize_print_text(order_data.get('universalComment', ''))
        if universal_comment:
            ticket_content += to_bytes("=" * NORMAL_FONT_LINE_WIDTH + "\n")
            ticket_content += SelectFontA + DoubleHeight + BoldOn
            ticket_content += to_bytes("ORDER NOTES\n")
            ticket_content += NormalText + BoldOff
            ticket_content += SelectFontA + DoubleHeight + BoldOn
            wrapped_universal_comment_lines = word_wrap_text(universal_comment, NORMAL_FONT_LINE_WIDTH, initial_indent="", subsequent_indent="") 
            for line in wrapped_universal_comment_lines:
                ticket_content += to_bytes(line + "\n")
            ticket_content += NormalText + BoldOff
            ticket_content += to_bytes("\n")
        
        ticket_content += to_bytes("\n")
        ticket_content += AlignCenter + SelectFontB
        disclaimer_text = "This is not a legal receipt and is for informational purposes only."
        wrapped_disclaimer_lines = word_wrap_text(disclaimer_text, SMALL_FONT_LINE_WIDTH)
        for line in wrapped_disclaimer_lines:
            ticket_content += to_bytes(line + "\n")

        ticket_content += SelectFontA + AlignLeft
            
        ticket_content += to_bytes("\n\n\n\n") 
        ticket_content += FullCut

        target_printer = printer_name or PRINTER_NAME
        hprinter = win32print.OpenPrinter(target_printer)
        try:
            doc_name = f"Order_{order_data.get('number', 'N/A')}_Ticket_{copy_info.replace(' ','_')}"
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
    app.run(host='0.0.0.0', port=5000, debug=True)
