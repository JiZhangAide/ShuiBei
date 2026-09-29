# -*- coding: utf-8 -*-
from __future__ import annotations
import re
try:
    from .config import DEVELOPER_SCAM_API_PATH
    from .developer_api import DeveloperAPIUnavailable, get_json
except ImportError:
    from config import DEVELOPER_SCAM_API_PATH
    from developer_api import DeveloperAPIUnavailable, get_json

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
_ID_RE = re.compile(r"^\d{6,15}$")

def normalize_key(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("https://t.me/"):
        raw = raw[len("https://t.me/"):]
    raw = raw.strip().strip("/").lstrip("@")
    if _ID_RE.fullmatch(raw): return raw
    if _USERNAME_RE.fullmatch(raw): return raw.lower()
    return ""

def query_records(value: str) -> tuple[bool, list[dict]]:
    key = normalize_key(value)
    if not key: return True, []
    query = key if key.isdigit() else "@" + key
    try:
        payload = get_json(DEVELOPER_SCAM_API_PATH, params={"query": query})
    except DeveloperAPIUnavailable:
        return False, []
    if str(payload.get("detail") or "") == "invalid_query": return True, []
    rows = payload.get("records")
    if not isinstance(rows, list): return False, []
    out = []
    for row in rows:
        if not isinstance(row, dict): continue
        out.append({
            "time": str(row.get("time") or ""),
            "link": str(row.get("link") or ""),
            "channel": str(row.get("channel") or ""),
            "message_id": int(row.get("message_id") or 0),
        })
    return True, out

def format_result(value: str) -> str:
    from features import scam_result_html
    available, rows = query_records(value)
    return scam_result_html(value, available, rows)
