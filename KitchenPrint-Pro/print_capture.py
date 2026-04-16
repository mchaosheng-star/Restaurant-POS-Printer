from __future__ import annotations

import ctypes
import hashlib
import io
import json
import logging
import os
import re
import select
import socket
import subprocess
import threading
import time
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)
MENU_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "menu.json")
_MENU_INDEX = None
_MENU_INDEX_MTIME = None


def _safe_makedirs(path: str):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def detect_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        try:
            s.close()
        except Exception:
            pass
        if ip and ip != "127.0.0.1":
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def get_interface_index_for_ip(ip: str) -> int:
    target = str(ip or "").strip()
    if not target:
        return 0
    forced = str(os.environ.get("PRINT_CAPTURE_INTERFACE_INDEX", "")).strip()
    if forced.isdigit():
        return int(forced)
    try:
        powershell_exe = None
        for candidate in (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            r"C:\Program Files\PowerShell\7\pwsh.exe",
        ):
            if os.path.exists(candidate):
                powershell_exe = candidate
                break
        if not powershell_exe:
            raise FileNotFoundError("powershell executable not found")
        ps = [
            powershell_exe,
            "-NoProfile",
            "-Command",
            f"(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {{$_.IPAddress -eq '{target}'}} | Select-Object -First 1 -ExpandProperty InterfaceIndex)"
        ]
        out = subprocess.check_output(ps, stderr=subprocess.DEVNULL, text=True).strip()
        if out.isdigit():
            return int(out)
    except Exception:
        pass
    try:
        import psutil  # type: ignore
        for name, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if getattr(addr, "family", None) == socket.AF_INET and getattr(addr, "address", "") == target:
                    try:
                        return socket.if_nametoindex(name)
                    except Exception:
                        return 0
    except Exception:
        return 0
    return 0


def sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _build_txt_record(props: dict[bytes, bytes]) -> bytes:
    out = bytearray()
    for key, value in props.items():
        chunk = key
        if value not in (None, b""):
            chunk += b"=" + value
        if len(chunk) > 255:
            chunk = chunk[:255]
        out.append(len(chunk))
        out.extend(chunk)
    return bytes(out)


def _bytes_to_text(data: bytes) -> str:
    if not data:
        return ""
    skip = 0
    filtered = bytearray()
    i = 0
    while i < len(data):
        if skip:
            skip -= 1
            i += 1
            continue
        b = data[i]
        if b == 0x1B:
            if i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (0x40,):
                    skip = 1
                elif cmd in (0x45, 0x4D, 0x61, 0x21, 0x2D, 0x33):
                    skip = 2
                else:
                    skip = 1
            i += 1
            continue
        if b == 0x1D:
            if i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (0x21, 0x56, 0x42, 0x68, 0x77):
                    skip = 2
                elif cmd == 0x76 and i + 7 < len(data) and data[i + 2] == 0x30:
                    width = data[i + 4] + (data[i + 5] << 8)
                    height = data[i + 6] + (data[i + 7] << 8)
                    skip = 7 + (width * height)
                else:
                    skip = 1
            i += 1
            continue
        if b in (9, 10, 13):
            filtered.append(b)
        elif 32 <= b <= 126:
            filtered.append(b)
        i += 1
    try:
        return filtered.decode("utf-8", errors="replace")
    except Exception:
        return filtered.decode("cp437", errors="replace")


_LINE_ITEM_RE = re.compile(r"^\s*(?:(\d+)\s*[xX]\s+)?(.+?)\s*$")
_PRICE_RE = re.compile(r"\s+\$?\d+(?:\.\d{2})?$")
_QTY_SUFFIX_RE = re.compile(r"^(.*?)(?:\s+|\s*[xX]\s*)(\d+)$")
_ITEMS_START_RE = re.compile(r"^(?:\d+\s*items?\b|items?\b|your\s+order\b|order\s+details\b|order\s+summary\b)", re.I)
_ORDER_SECTION_END_RE = re.compile(r"^(subtotal|delivery\s*fee|service\s*fee|tax|taxes|tip|tips|total|payment|paid|balance\s+due)\b", re.I)
_ORDER_NUMBER_PATTERNS = [
    re.compile(r"\b(?:order\s*(?:number|no\.?|#|id)|delivery\s*id)\s*[:#]?\s*([A-Z0-9\-]{4,})\b", re.I),
    re.compile(r"\b#([A-Z0-9\-]{4,})\b"),
]
_COMMENT_MARKERS = ("note", "notes", "special instructions", "special requests", "customer note", "comment")
_SKIP_LINE_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"^(subtotal|tax|fees?|tip|tips|discount|total|balance|payment|cash|card)\b",
        r"^(menu\s*item|qty|quantity|description|amount|price)\b",
        r"^(merchant|restaurant|pickup|delivery|dropoff|drop-off|driver|dasher|courier)\b",
        r"^(order|receipt|customer|phone|address|email|placed|ready|eta|time|date)\b",
        r"^(uber|ubereats|uber eats|doordash|door dash|grubhub|grub hub)\b",
        r"^(items?\s+\d+|page\s+\d+|kitchenprintpro)\b",
        r"^(thank you|thanks|enjoy)\b",
    ]
]
_NON_FOOD_PATTERNS = [
    re.compile(p, re.I) for p in [
        r"confirmation\s*code",
        r"self\s*delivery",
        r"deliver\s*to",
        r"pickup\s*by",
        r"pickup\s*time",
        r"contact[- ]?free",
        r"leave order",
        r"text ?customer",
        r"house/?apartment",
        r"condiment packets",
        r"include\s*plates?\s*utensils?",
        r"^[=\-_*]{3,}$",
        r"^no$",
        r"^[\d(). -]{7,}$",
        r"^[A-Z]{2}\d{4,}$",
    ]
]
_MODIFIER_PREFIXES = (
    "-",
    "+",
    "*",
    "minus ",
    "no ",
    "add ",
    "extra ",
    "sub ",
    "substitute ",
    "with ",
    "without ",
    "remove ",
    "side ",
    "sauce ",
    "sauces ",
    "dressing ",
    "allergy ",
    "option ",
    "choice ",
)


def _normalize_lines(text: str) -> list[str]:
    lines = []
    for raw in (text or "").splitlines():
        s = str(raw or "").replace("\x00", " ")
        s = re.sub(r"^@a(?=[A-Za-z])", "", s)
        s = re.sub(r"^@(?=[A-Za-z])", "", s)
        s = re.sub(r"^!([0-9A-Fa-f])([0-9]+\s*[xX]\s+)", r"\2", s)
        s = re.sub(r"^![0-9A-Fa-f]{2}(?=\s+[A-Za-z])", "", s)
        s = re.sub(r"^!(?=[=\-_*]{3,})", "", s)
        s = re.sub(r"^V$", "", s)
        s = " ".join(s.split())
        if not s:
            continue
        lines.append(s[:240])
    return lines


def _normalize_ocr_item_text(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    s = s.replace("×", "x ")
    s = s.replace("·", " ")
    s = re.sub(r"^[.·•\-]+\s*", "", s)
    s = re.sub(r"^x\s+", "", s, flags=re.I)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", s)
    s = re.sub(r"\bof([A-Z])", r"of \1", s)
    s = re.sub(r"(^|\s)![0-9A-Fa-f]{2}\s+[xX]\s+", r"\1", s)
    s = re.sub(r"(^|\s)![0-9A-Fa-f]{2}(?=\s+[A-Za-z])", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_menu_lookup_name(value: str) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = s.replace("&", " and ")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\b(combo|meal|size|regular|large|medium|small)\b", " ", s)
    s = re.sub(r"\bpcs?\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _load_menu_index() -> dict[str, dict]:
    global _MENU_INDEX, _MENU_INDEX_MTIME
    try:
        mtime = os.path.getmtime(MENU_FILE)
        if _MENU_INDEX is not None and _MENU_INDEX_MTIME == mtime:
            return _MENU_INDEX
        with open(MENU_FILE, "r", encoding="utf-8") as f:
            raw_menu = json.load(f)
    except Exception:
        _MENU_INDEX = {}
        _MENU_INDEX_MTIME = None
        return {}

    index = {}
    if isinstance(raw_menu, dict):
        for category, items in raw_menu.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                key = _normalize_menu_lookup_name(name)
                if not key or key in index:
                    continue
                index[key] = {
                    "name": name,
                    "nameZh": str(item.get("nameZh", "")).strip(),
                    "category": str(category).strip(),
                }
    _MENU_INDEX = index
    _MENU_INDEX_MTIME = mtime
    return index


def _menu_item_for_name(item_name: str):
    key = _normalize_menu_lookup_name(item_name)
    if not key:
        return None
    menu = _load_menu_index()
    if key in menu:
        return menu[key]
    for menu_key, menu_item in menu.items():
        if menu_key and (menu_key in key or key in menu_key):
            return menu_item
    return None


def _attach_menu_metadata(item: dict) -> dict:
    menu_item = _menu_item_for_name(item.get("name", ""))
    if not menu_item:
        return item
    item["category"] = menu_item.get("category", "")
    if menu_item.get("nameZh"):
        item["nameZh"] = menu_item.get("nameZh", "")
    return item


def _extract_order_section_lines(text: str) -> list[str]:
    lines = _normalize_lines(text)
    start_idx = 0
    for idx, line in enumerate(lines):
        if _ITEMS_START_RE.search(line):
            start_idx = idx + 1
            break
    section = []
    for line in lines[start_idx:]:
        normalized = _normalize_ocr_item_text(line)
        if not normalized:
            continue
        if _ORDER_SECTION_END_RE.search(normalized):
            break
        if re.fullmatch(r"\$?\d+(?:\.\d{2})?", normalized):
            continue
        if any(p.search(normalized) for p in _NON_FOOD_PATTERNS):
            continue
        section.append(normalized)
    return section


def detect_order_source(text: str, fallback: str = "IPP") -> str:
    low = str(text or "").lower()
    if "uber eats" in low or "ubereats" in low or re.search(r"\buber\b", low):
        return "Uber Eats"
    if "doordash" in low or "door dash" in low:
        return "DoorDash"
    if "grubhub" in low or "grub hub" in low:
        return "Grubhub"
    return fallback


def extract_order_number(text: str, fallback_number: Optional[str] = None) -> str:
    for pattern in _ORDER_NUMBER_PATTERNS:
        m = pattern.search(text or "")
        if m:
            return m.group(1).strip()
    return fallback_number or f"IPP-{_now_id()}"


def _looks_like_skip_line(line: str) -> bool:
    s = str(line or "").strip()
    if not s:
        return True
    if len(s) <= 1:
        return True
    if s == "V":
        return True
    if re.fullmatch(r"[=\-_*]{3,}", s):
        return True
    if re.fullmatch(r"[\d\s:/$.\-]+", s):
        return True
    return any(p.search(s) for p in _SKIP_LINE_PATTERNS)


def _clean_item_name(line: str) -> str:
    s = _normalize_ocr_item_text(_PRICE_RE.sub("", str(line or "").strip()))
    s = re.sub(r"(^|\s)![0-9A-Fa-f]{2}\b", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -:")


def _parse_item_line(line: str):
    s = _clean_item_name(line)
    if not s or _looks_like_skip_line(s):
        return None

    qty = 1
    name = s
    m = re.match(r"^(\d+)\s*[xX]\s+(.+)$", s)
    if m:
        qty = max(1, int(m.group(1)))
        name = m.group(2).strip()
    else:
        m = re.match(r"^(\d+)\s+(.+)$", s)
        if m and len(m.group(2).split()) >= 2:
            qty = max(1, int(m.group(1)))
            name = m.group(2).strip()
        else:
            m = _QTY_SUFFIX_RE.match(s)
            if m and m.group(2).isdigit() and len(m.group(1).split()) >= 2:
                qty = max(1, int(m.group(2)))
                name = m.group(1).strip()

    low = name.lower()
    if any(marker in low for marker in _COMMENT_MARKERS):
        return None
    if _looks_like_skip_line(name):
        return None
    return _attach_menu_metadata({
        "name": name[:120],
        "quantity": qty,
        "price": 0.0,
        "selectedOptions": [],
        "comment": "",
    })


def _is_modifier_line(line: str) -> bool:
    s = str(line or "").strip()
    if not s:
        return False
    low = s.lower()
    if any(low.startswith(prefix) for prefix in _MODIFIER_PREFIXES):
        return True
    if low.startswith("note:") or low.startswith("notes:") or low.startswith("comment:"):
        return True
    return False


def _append_item_modifier(item: dict, line: str):
    s = str(line or "").strip()
    if not s:
        return
    if ":" in s and s.split(":", 1)[0].strip().lower() in _COMMENT_MARKERS:
        text = s.split(":", 1)[1].strip()
        if text:
            current = str(item.get("comment", "")).strip()
            item["comment"] = f"{current} | {text}".strip(" |")[:200] if current else text[:200]
        return
    text = s.lstrip("-+* ").strip()
    if not text:
        return
    options = item.get("selectedOptions")
    if not isinstance(options, list):
        options = []
        item["selectedOptions"] = options
    options.append({"name": text[:120]})


def _fallback_items_from_lines(lines: list[str]) -> list[dict]:
    items = []
    previous_item = None
    for line in lines[:80]:
        cleaned = _normalize_ocr_item_text(line)
        if not cleaned:
            continue
        if previous_item is not None and _is_modifier_line(cleaned):
            _append_item_modifier(previous_item, cleaned)
            continue
        parsed = _parse_item_line(cleaned)
        if not parsed:
            continue
        items.append(parsed)
        previous_item = parsed
    return items


def extract_items_from_text(text: str) -> list[dict]:
    lines = _extract_order_section_lines(text) or _normalize_lines(text)
    items = []
    previous_item = None
    for line in lines:
        normalized = _normalize_ocr_item_text(line)
        if not normalized:
            continue
        if previous_item is not None and _is_modifier_line(normalized):
            _append_item_modifier(previous_item, normalized)
            continue
        cleaned = _clean_item_name(normalized)
        if not cleaned:
            continue
        low = cleaned.lower()
        if low == "plate" and previous_item is not None and "plate" not in str(previous_item.get("name", "")).lower():
            previous_item["name"] = f"{previous_item.get('name', '').strip()} Plate".strip()
            _attach_menu_metadata(previous_item)
            continue
        if any(low.startswith(marker + ":") for marker in _COMMENT_MARKERS):
            if previous_item is not None:
                previous_item["comment"] = cleaned.split(":", 1)[1].strip()[:200]
            continue
        parsed = _parse_item_line(cleaned)
        if not parsed:
            continue
        items.append(parsed)
        previous_item = parsed
    return items


def extract_comment_from_text(text: str) -> str:
    block = re.search(r"add\w+\s+instructions?\s*(.+?)(?:\n\s*\d+\s*items?\b)", str(text or ""), re.I | re.S)
    if block:
        raw = " ".join(_normalize_lines(block.group(1)))
        normalized = _normalize_ocr_item_text(raw)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized:
            return normalized[:200]
    lines = _normalize_lines(text)
    note_lines = []
    capture = False
    for line in lines:
        normalized = _normalize_ocr_item_text(line)
        low = normalized.lower()
        if ("additional" in low and "instruction" in low) or "special instruction" in low:
            capture = True
            continue
        if _ITEMS_START_RE.search(low) or _ORDER_SECTION_END_RE.search(low):
            if note_lines:
                break
            capture = False
        if capture:
            if any(p.search(normalized) for p in _NON_FOOD_PATTERNS):
                continue
            if normalized:
                note_lines.append(normalized)
                continue
        for marker in _COMMENT_MARKERS:
            if low.startswith(marker + ":"):
                return normalized.split(":", 1)[1].strip()[:200]
    if note_lines:
        return " ".join(note_lines)[:200]
    return ""


def extract_text_from_pdf(path: str) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        PdfReader = None  # type: ignore
    text = ""
    if PdfReader is not None:
        try:
            reader = PdfReader(path)
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:
            logger.exception("PDF text extraction failed for %s: %s", path, e)
    if text and text.strip():
        return text
    return ocr_pdf_to_text(path)


def ocr_pdf_to_text(path: str) -> str:
    try:
        import fitz  # type: ignore
    except Exception:
        return ""
    chunks = []
    try:
        doc = fitz.open(path)
    except Exception as e:
        logger.exception("PDF OCR open failed for %s: %s", path, e)
        return ""
    try:
        page_count = min(len(doc), 6)
        for i in range(page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            page_text = ocr_image_bytes(pix.tobytes("png"))
            if page_text.strip():
                chunks.append(page_text)
    except Exception as e:
        logger.exception("PDF OCR failed for %s: %s", path, e)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return "\n".join(chunks).strip()


def ocr_image_bytes(data: bytes) -> str:
    text = _ocr_with_rapidocr(data)
    if text.strip():
        return text
    return _ocr_with_tesseract(data)


def _ocr_with_rapidocr(data: bytes) -> str:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception:
        return ""
    try:
        engine = RapidOCR()
        result, _ = engine(data)
        if not result:
            return ""
        return "\n".join(str(line[1]) for line in result if len(line) > 1 and line[1])
    except Exception as e:
        logger.exception("RapidOCR failed: %s", e)
        return ""


def _ocr_with_tesseract(data: bytes) -> str:
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore
    except Exception:
        return ""
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
        return pytesseract.image_to_string(image)
    except Exception as e:
        logger.exception("Tesseract OCR failed: %s", e)
        return ""


def build_order_from_pdf(path: str) -> tuple[dict, str]:
    text = extract_text_from_pdf(path)
    source = detect_order_source(text, fallback="IPP")
    number = extract_order_number(text)
    items = extract_items_from_text(text)
    comment = extract_comment_from_text(text)
    if not items:
        base = os.path.basename(path)
        items = [{
            "name": f"Captured print job {base}",
            "quantity": 1,
            "price": 0.0,
            "selectedOptions": [],
            "comment": "",
        }]
    order_data = {
        "number": number,
        "tableNumber": source,
        "items": items,
        "universalComment": comment,
    }
    return order_data, text


def build_order_from_saved_job(path: str) -> tuple[dict, str]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return build_order_from_pdf(path)
    if ext in (".jpg", ".jpeg", ".png"):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception:
            raw = b""
        text = ocr_image_bytes(raw)
        source = detect_order_source(text, fallback="IPP")
        order_data = parse_receipt_text_to_order(text, source=source, fallback_number=extract_order_number(text))
        order_data["universalComment"] = extract_comment_from_text(text)
        return order_data, text
    text = ""
    if ext in (".txt", ".bin", ".urf", ".jpg", ".jpeg"):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            text = _bytes_to_text(raw)
        except Exception:
            text = ""
    source = detect_order_source(text, fallback="IPP")
    order_data = parse_receipt_text_to_order(text, source=source, fallback_number=f"IPP-{_now_id()}")
    return order_data, text


def parse_receipt_text_to_order(text: str, source: str, fallback_number: Optional[str] = None) -> dict:
    lines = _extract_order_section_lines(text) or _normalize_lines(text)
    items = extract_items_from_text(text)
    if not items and lines:
        items = _fallback_items_from_lines(lines)
    if not items:
        items = [{
            "name": f"Captured print job {extract_order_number(text, fallback_number)}",
            "quantity": 1,
            "price": 0.0,
            "selectedOptions": [],
            "comment": "",
        }]

    source_name = detect_order_source(text, fallback=source)
    num = extract_order_number(text, fallback_number or f"{source_name}-{_now_id()}")
    return {
        "number": num,
        "tableNumber": source_name,
        "items": items,
        "universalComment": extract_comment_from_text(text),
    }


def _escpos_cut_length(data: bytes, idx: int) -> int:
    if idx + 2 >= len(data):
        return 0
    if data[idx] != 0x1D or data[idx + 1] != 0x56:
        return 0
    mode = data[idx + 2]
    if mode in (65, 66):
        return 4 if idx + 3 < len(data) else 3
    return 3


def split_escpos_jobs(data: bytes) -> list[bytes]:
    if not data:
        return []
    jobs = []
    start = 0
    idx = 0
    while idx < len(data):
        cut_len = _escpos_cut_length(data, idx)
        if not cut_len:
            idx += 1
            continue
        end = min(len(data), idx + cut_len)
        chunk = data[start:end]
        if chunk.strip(b"\x00"):
            jobs.append(chunk)
        start = end
        idx = end
    tail = data[start:]
    if tail.strip(b"\x00"):
        jobs.append(tail)
    return jobs or [data]


class Raw9100Receiver:
    def __init__(
        self,
        host: str,
        port: int,
        jobs_dir: str,
        on_job: Callable[[bytes, str], None],
        recv_timeout_sec: float = 10.0,
        max_bytes: int = 10 * 1024 * 1024,
    ):
        self.host = host
        self.port = int(port)
        self.jobs_dir = jobs_dir
        self.on_job = on_job
        self.recv_timeout_sec = float(recv_timeout_sec)
        self.max_bytes = int(max_bytes)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        _safe_makedirs(self.jobs_dir)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(10)
        srv.settimeout(1.0)
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                continue
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()

    def _handle_conn(self, conn: socket.socket, addr):
        try:
            conn.settimeout(self.recv_timeout_sec)
            chunks = []
            total = 0
            while True:
                try:
                    part = conn.recv(65536)
                except TimeoutError:
                    break
                if not part:
                    break
                chunks.append(part)
                total += len(part)
                if total >= self.max_bytes:
                    break
            data = b"".join(chunks)
        except Exception:
            data = b""
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not data:
            return

        jobs = split_escpos_jobs(data)
        multi = len(jobs) > 1
        for idx, job_data in enumerate(jobs, start=1):
            job_id = _now_id()
            base = f"raw9100_{job_id}_{idx:02d}" if multi else f"raw9100_{job_id}"
            try:
                with open(os.path.join(self.jobs_dir, base + ".bin"), "wb") as f:
                    f.write(job_data)
            except Exception:
                pass

            try:
                txt = _bytes_to_text(job_data)
                if txt.strip():
                    with open(os.path.join(self.jobs_dir, base + ".txt"), "w", encoding="utf-8", errors="replace") as f:
                        f.write(txt)
            except Exception:
                pass

            try:
                self.on_job(job_data, f"{addr[0]}:{addr[1]}")
            except Exception:
                pass


def start_ipp_receiver_if_available(jobs_dir: str, host: str, port: int, public_host: Optional[str] = None) -> bool:
    try:
        from ippserver.behaviour import SaveFilePrinter  # type: ignore
        from ippserver.constants import SectionEnum, TagEnum  # type: ignore
        from ippserver.server import IPPServer, IPPRequestHandler, run_server  # type: ignore
    except Exception:
        return False

    _safe_makedirs(jobs_dir)
    public_host = str(public_host or host or "127.0.0.1")

    class AirPrintSaveFilePrinter(SaveFilePrinter):
        def printer_list_attributes(self):
            attr = super().printer_list_attributes()
            attr[(SectionEnum.printer, b'printer-name', TagEnum.name_without_language)] = [b'KitchenPrintPro']
            attr[(SectionEnum.printer, b'printer-info', TagEnum.text_without_language)] = [b'KitchenPrint-Pro AirPrint']
            attr[(SectionEnum.printer, b'printer-make-and-model', TagEnum.text_without_language)] = [b'KitchenPrint-Pro AirPrint']
            attr[(SectionEnum.printer, b'document-format-default', TagEnum.mime_media_type)] = [b'application/pdf']
            attr[(SectionEnum.printer, b'document-format-supported', TagEnum.mime_media_type)] = [
                b'application/pdf',
                b'image/urf',
                b'image/jpeg',
                b'application/octet-stream',
            ]
            return attr

        def leaf_filename(self, ipp_request):
            ext = "pdf"
            try:
                fmt = ipp_request.only(SectionEnum.operation, b'document-format', TagEnum.mime_media_type)
                if fmt == b'image/urf':
                    ext = "urf"
                elif fmt == b'image/jpeg':
                    ext = "jpg"
                elif fmt == b'application/octet-stream':
                    ext = "bin"
            except Exception:
                pass
            return f"ipp-job-{_now_id()}.{ext}"

    def _runner():
        beh = AirPrintSaveFilePrinter(directory=jobs_dir, filename_ext="pdf")
        base_uri = f"ipp://{public_host}:{int(port)}/ipp/print".encode("utf-8")
        beh.base_uri = base_uri
        beh.printer_uri = base_uri
        server = IPPServer((host, int(port)), IPPRequestHandler, beh)
        run_server(server)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return True


class NativeBonjourAdvertiser:
    def __init__(self, service_name: str, host: str, port: int):
        self.service_name = service_name
        self.host = host
        self.port = int(port)
        self.interface_index = get_interface_index_for_ip(host)
        self._refs: list[ctypes.c_void_p] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        try:
            lib = ctypes.WinDLL("dnssd.dll")
        except Exception:
            return False

        self._configure_api(lib)
        txt = _build_txt_record({
            b"txtvers": b"1",
            b"qtotal": b"1",
            b"rp": b"ipp/print",
            b"ty": self.service_name.encode("utf-8", errors="ignore"),
            b"product": b"(KitchenPrint-Pro)",
            b"note": b"KitchenPrint-Pro",
            b"pdl": b"application/pdf,image/urf",
            b"URF": b"CP255,SRGB24,W8,OB9,PQ4-5",
            b"Color": b"F",
            b"Duplex": b"F",
            b"Transparent": b"T",
            b"Binary": b"T",
            b"kind": b"document,photo",
        })

        ipp_ref = ctypes.c_void_p()
        rc = self._DNSServiceRegister(
            ctypes.byref(ipp_ref),
            0,
            self.interface_index,
            self.service_name.encode("utf-8"),
            b"_ipp._tcp,_universal",
            None,
            None,
            socket.htons(self.port),
            len(txt),
            txt,
            None,
            None,
        )
        if rc != 0:
            return False
        self._refs = [ipp_ref]
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return True

    def _configure_api(self, lib):
        self._DNSServiceRegister = lib.DNSServiceRegister
        self._DNSServiceRegister.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint16,
            ctypes.c_uint16,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._DNSServiceRegister.restype = ctypes.c_int32

        self._DNSServiceRefSockFD = lib.DNSServiceRefSockFD
        self._DNSServiceRefSockFD.argtypes = [ctypes.c_void_p]
        self._DNSServiceRefSockFD.restype = ctypes.c_int

        self._DNSServiceProcessResult = lib.DNSServiceProcessResult
        self._DNSServiceProcessResult.argtypes = [ctypes.c_void_p]
        self._DNSServiceProcessResult.restype = ctypes.c_int32

        self._DNSServiceRefDeallocate = lib.DNSServiceRefDeallocate
        self._DNSServiceRefDeallocate.argtypes = [ctypes.c_void_p]
        self._DNSServiceRefDeallocate.restype = None

    def _pump(self):
        while not self._stop.is_set():
            fds = []
            fd_map = {}
            for ref in self._refs:
                fd = self._DNSServiceRefSockFD(ref)
                if fd >= 0:
                    fds.append(fd)
                    fd_map[fd] = ref
            if not fds:
                time.sleep(1.0)
                continue
            try:
                readable, _, _ = select.select(fds, [], [], 1.0)
            except Exception:
                time.sleep(1.0)
                continue
            for fd in readable:
                ref = fd_map.get(fd)
                if ref:
                    try:
                        self._DNSServiceProcessResult(ref)
                    except Exception:
                        pass

    def stop(self):
        self._stop.set()
        for ref in self._refs:
            try:
                self._DNSServiceRefDeallocate(ref)
            except Exception:
                pass
        self._refs = []


def start_native_airprint_mdns_if_available(service_name: str, host: str, port: int):
    adv = NativeBonjourAdvertiser(service_name=service_name, host=host, port=port)
    if adv.start():
        return adv
    return None


def start_airprint_mdns_if_available(service_name: str, host: str, port: int) -> bool:
    try:
        from zeroconf import ServiceInfo, Zeroconf  # type: ignore
    except Exception:
        return False

    zc = Zeroconf()
    props = {
        b"txtvers": b"1",
        b"qtotal": b"1",
        b"rp": b"ipp/print",
        b"ty": service_name.encode("utf-8", errors="ignore"),
        b"adminurl": b"/",
        b"note": b"KitchenPrint-Pro",
        b"pdl": b"application/pdf",
        b"URF": b"none",
        b"Color": b"F",
        b"Duplex": b"F",
    }
    addr = socket.inet_aton(host)
    ipp = ServiceInfo(
        type_="_ipp._tcp.local.",
        name=f"{service_name}._ipp._tcp.local.",
        addresses=[addr],
        port=int(port),
        properties=props,
        server=f"{service_name}.local.",
    )
    printer = ServiceInfo(
        type_="_printer._tcp.local.",
        name=f"{service_name}._printer._tcp.local.",
        addresses=[addr],
        port=int(port),
        properties=props,
        server=f"{service_name}.local.",
    )
    try:
        zc.register_service(ipp)
        zc.register_service(printer)
    except Exception:
        try:
            zc.close()
        except Exception:
            pass
        return False

    def _keepalive():
        while True:
            time.sleep(3600)

    threading.Thread(target=_keepalive, daemon=True).start()
    return True

