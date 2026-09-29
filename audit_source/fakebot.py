# -*- coding: utf-8 -*-
from __future__ import annotations
import re
try:
    from .config import DEVELOPER_FAKEBOT_API_PATH
    from .developer_api import DeveloperAPIUnavailable, get_json
except ImportError:
    from config import DEVELOPER_FAKEBOT_API_PATH
    from developer_api import DeveloperAPIUnavailable, get_json

def normalize_username(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(value or "").strip().lower().lstrip("@"))

def find_suspect_state(value: str) -> tuple[bool, dict | None, bool]:
    username = normalize_username(value)
    if not username: return True, None, False
    try:
        payload = get_json(DEVELOPER_FAKEBOT_API_PATH, params={"username": username})
    except DeveloperAPIUnavailable:
        return False, None, False
    if str(payload.get("detail") or "") == "invalid_username": return True, None, False
    if bool(payload.get("official")): return True, None, True
    if not bool(payload.get("suspect")): return True, None, False
    match = normalize_username(str(payload.get("match") or ""))
    if not match: return False, None, False
    ratio = float(payload.get("similarity") or 0.0)
    try: distance = int(payload.get("distance") or 0)
    except Exception: distance = 0
    return True, {
        "official": match, "suspect": username, "distance": distance, "ratio": ratio,
        "score": float(payload.get("score") or (ratio * 100 - distance * 7)),
    }, False

def find_suspect(value: str):
    return find_suspect_state(value)[1]

def format_manual(value: str) -> str:
    from features import fakebot_result_html
    username = normalize_username(value)
    if not username: return fakebot_result_html("", None, official=False, available=True)
    available, hit, official = find_suspect_state(username)
    return fakebot_result_html(username, hit, official=official, available=available)
