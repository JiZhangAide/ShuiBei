# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import re
import time
from decimal import Decimal

import requests

from config import (
    COINGECKO_SIMPLE_PRICE_API,
    HEXARATE_RATE_API,
    HTTP_TIMEOUT,
    TONAPI_BASE,
    TRC20_USDT_CONTRACT,
    TRONGRID_ACCOUNT_API,
    TRONGRID_API_KEY,
    TRONGRID_TRC20_API,
    TRONGRID_TX_API,
)

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_TRON_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_TON_RE = re.compile(r"^(?:EQ|UQ|Ef|Uf)[A-Za-z0-9_-]{46}$")
_TON_DOMAIN_RE = re.compile(r"^(?!@)([A-Za-z0-9_-]{1,63}\.)*[A-Za-z0-9_-]{1,63}\.ton$", re.I)

_rate_cache = {"at": 0.0, "text": ""}


def _tron_b58decode(value: str) -> bytes:
    n = 0
    for ch in str(value or ""):
        idx = _ALPHABET.find(ch)
        if idx < 0:
            raise ValueError("bad base58")
        n = n * 58 + idx
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(str(value)) - len(str(value).lstrip("1"))
    return b"\x00" * pad + raw


def is_tron_address(value: str) -> bool:
    s = str(value or "").strip()
    if not _TRON_RE.fullmatch(s):
        return False
    try:
        raw = _tron_b58decode(s)
        if len(raw) != 25:
            return False
        payload, checksum = raw[:-4], raw[-4:]
        digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
        return checksum == digest and payload[:1] == b"A"
    except Exception:
        return False


def _headers() -> dict:
    h = {"Accept": "application/json", "User-Agent": "ShuiBei/1.0"}
    if TRONGRID_API_KEY:
        h["TRON-PRO-API-KEY"] = TRONGRID_API_KEY
    return h


def _fmt_decimal(v: Decimal, places: int = 6) -> str:
    s = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return s or "0"


def _tron_balance(address: str) -> tuple[Decimal, Decimal]:
    trx = Decimal("0")
    usdt = Decimal("0")
    try:
        r = requests.get(TRONGRID_ACCOUNT_API.format(address=address), headers=_headers(), timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            obj = r.json() if r.content else {}
            data = (obj.get("data") or [{}])[0] if isinstance(obj, dict) and obj.get("data") else {}
            trx = Decimal(str(data.get("balance") or 0)) / Decimal("1000000")
            for entry in data.get("trc20") or []:
                if isinstance(entry, dict):
                    for contract, raw in entry.items():
                        if str(contract) == TRC20_USDT_CONTRACT:
                            usdt = Decimal(str(raw or 0)) / Decimal("1000000")
                            break
    except Exception:
        pass
    return trx, usdt


def _usdt_transfers(address: str, limit: int = 8) -> list[dict]:
    try:
        r = requests.get(
            TRONGRID_TRC20_API.format(address=address),
            params={"limit": min(20, max(1, int(limit))), "only_confirmed": "true", "contract_address": TRC20_USDT_CONTRACT, "order_by": "block_timestamp,desc"},
            headers=_headers(), timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        obj = r.json() or {}
        out = []
        for it in obj.get("data") or []:
            token = it.get("token_info") or {}
            decimals = int(token.get("decimals") or 6)
            amount = Decimal(str(it.get("value") or 0)) / (Decimal(10) ** decimals)
            frm, to = str(it.get("from") or ""), str(it.get("to") or "")
            out.append({
                "direction": "入" if to == address else "出" if frm == address else "?",
                "amount": amount,
                "from": frm, "to": to,
                "txid": str(it.get("transaction_id") or ""),
                "ts": int(it.get("block_timestamp") or 0) // 1000,
            })
        return out
    except Exception:
        return []


def _trx_transactions(address: str, limit: int = 5) -> list[dict]:
    try:
        r = requests.get(
            TRONGRID_TX_API.format(address=address),
            params={"limit": min(20, max(1, int(limit))), "only_confirmed": "true", "order_by": "block_timestamp,desc"},
            headers=_headers(), timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        obj = r.json() or {}
        out = []
        for it in obj.get("data") or []:
            raw = (((it.get("raw_data") or {}).get("contract") or [{}])[0].get("parameter") or {}).get("value") or {}
            amount = Decimal(str(raw.get("amount") or 0)) / Decimal("1000000")
            if amount <= 0:
                continue
            out.append({"amount": amount, "txid": str(it.get("txID") or ""), "ts": int(it.get("block_timestamp") or 0) // 1000})
        return out
    except Exception:
        return []


def query_tron(address: str) -> str:
    addr = str(address or "").strip()
    if not is_tron_address(addr):
        return "TRON 地址格式不正确。"
    trx, usdt = _tron_balance(addr)
    usdt_rows = _usdt_transfers(addr, 8)
    trx_rows = _trx_transactions(addr, 5)
    lines = [
        "⛓ TRON / USDT 查询",
        f"地址：{addr}",
        f"TRX 余额：{_fmt_decimal(trx)}",
        f"USDT 余额：{_fmt_decimal(usdt)}",
    ]
    if usdt_rows:
        lines.append("\n最近 USDT：")
        for r in usdt_rows:
            tm = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])) if r["ts"] else "未知"
            lines.append(f"{r['direction']} {_fmt_decimal(r['amount'])} USDT｜{tm}")
    if trx_rows:
        lines.append("\n最近 TRX 转账：")
        for r in trx_rows:
            tm = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])) if r["ts"] else "未知"
            lines.append(f"{_fmt_decimal(r['amount'])} TRX｜{tm}")
    if not usdt_rows and not trx_rows:
        lines.append("\n暂无可显示的最近流水，或上游接口暂不可用。")
    return "\n".join(lines)


def is_ton_target(value: str) -> bool:
    s = str(value or "").strip()
    return bool(_TON_RE.fullmatch(s) or _TON_DOMAIN_RE.fullmatch(s))


def _ton_get(path: str, params: dict | None = None):
    try:
        r = requests.get(f"{TONAPI_BASE.rstrip('/')}/{path.lstrip('/')}", params=params or {}, headers={"Accept": "application/json", "User-Agent": "ShuiBei/1.0"}, timeout=HTTP_TIMEOUT)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _ton_amount(nano) -> Decimal:
    try:
        return Decimal(str(nano or 0)) / Decimal("1000000000")
    except Exception:
        return Decimal("0")


def query_ton(target: str) -> str:
    t = str(target or "").strip()
    if not is_ton_target(t):
        return "TON 地址/域名格式不正确。"
    # .ton 域名先尝试 TonAPI DNS 解析；接口不可用时直接按目标查询，保持只读失败。
    resolved = t
    if _TON_DOMAIN_RE.fullmatch(t):
        dns = _ton_get(f"dns/{t}") or _ton_get(f"dns/{t}/resolve")
        if isinstance(dns, dict):
            resolved = str(dns.get("wallet") or dns.get("address") or dns.get("resolver") or t)
    account = _ton_get(f"accounts/{resolved}") or {}
    if not account and resolved != t:
        account = _ton_get(f"accounts/{t}") or {}
    if not account:
        return "TON 查询暂时没有结果。"
    address = str(account.get("address") or resolved)
    balance = _ton_amount(account.get("balance"))
    status = str(account.get("status") or "unknown")
    events = _ton_get(f"accounts/{address}/events", {"limit": 8}) or {}
    arr = events.get("events") or [] if isinstance(events, dict) else []
    lines = ["💎 TON 查询", f"目标：{t}", f"地址：{address}", f"余额：{_fmt_decimal(balance, 9)} TON", f"状态：{status}"]
    if arr:
        lines.append("\n最近活动：")
        for ev in arr[:8]:
            ts = int(ev.get("timestamp") or 0)
            tm = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "未知"
            eid = str(ev.get("event_id") or "")
            lines.append(f"{tm}｜{eid[:16]}{'…' if len(eid) > 16 else ''}")
    return "\n".join(lines)


def exchange_rate_text(force: bool = False) -> str:
    now = time.time()
    if not force and _rate_cache["text"] and now - float(_rate_cache["at"] or 0) < 45:
        return str(_rate_cache["text"])
    cny = Decimal("0"); trx = Decimal("0"); ton = Decimal("0")
    try:
        r = requests.get(HEXARATE_RATE_API.format(base="USDT", target="CNY"), timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            obj = r.json() or {}
            data = obj.get("data") or {}
            cny = Decimal(str(data.get("mid") or data.get("rate") or data.get("value") or 0))
    except Exception:
        pass
    try:
        r = requests.get(COINGECKO_SIMPLE_PRICE_API, params={"ids": "the-open-network,tron", "vs_currencies": "usd"}, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            obj = r.json() or {}
            ton = Decimal(str(((obj.get("the-open-network") or {}).get("usd")) or 0))
            trx = Decimal(str(((obj.get("tron") or {}).get("usd")) or 0))
    except Exception:
        pass
    lines = ["💱 当前汇率"]
    lines.append(f"1 USDT ≈ {_fmt_decimal(cny, 4)} CNY" if cny else "USDT/CNY：暂不可用")
    lines.append(f"1 TRX ≈ {_fmt_decimal(trx, 6)} USD" if trx else "TRX/USD：暂不可用")
    lines.append(f"1 TON ≈ {_fmt_decimal(ton, 6)} USD" if ton else "TON/USD：暂不可用")
    text = "\n".join(lines)
    _rate_cache.update({"at": now, "text": text})
    return text

# ================== v21：与墨清记账一致的直接汇率换算 ==================
_RATE_QUERY_RE = re.compile(r"^\s*(\d+(?:\.\d{1,8})?)\s*(u|usdt|rmb|cny|cry|人民币|元|trx|ton)\s*$", re.I)
_rate_values_cache = {"at": 0.0, "usdt_cny": Decimal("0"), "trx_usdt": Decimal("0"), "ton_usdt": Decimal("0")}


def _rate_values(force: bool = False):
    now = time.time()
    if not force and now - float(_rate_values_cache.get("at") or 0) < 45:
        if all(Decimal(str(_rate_values_cache.get(k) or 0)) > 0 for k in ("usdt_cny", "trx_usdt", "ton_usdt")):
            return dict(_rate_values_cache)
    cny = Decimal("0"); trx = Decimal("0"); ton = Decimal("0")
    try:
        r = requests.get(HEXARATE_RATE_API.format(base="USD", target="CNY"), timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            data = (r.json() or {}).get("data") or {}
            mid = Decimal(str(data.get("mid") or data.get("rate") or data.get("value") or 0))
            unit = Decimal(str(data.get("unit") or 1))
            if mid > 0 and unit > 0:
                cny = mid / unit
    except Exception:
        pass
    try:
        r = requests.get(COINGECKO_SIMPLE_PRICE_API, params={"ids": "the-open-network,tron", "vs_currencies": "usd"}, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            obj = r.json() or {}
            ton = Decimal(str(((obj.get("the-open-network") or {}).get("usd")) or 0))
            trx = Decimal(str(((obj.get("tron") or {}).get("usd")) or 0))
    except Exception:
        pass
    if cny > 0 and trx > 0 and ton > 0:
        _rate_values_cache.update({"at": now, "usdt_cny": cny, "trx_usdt": trx, "ton_usdt": ton})
    return dict(_rate_values_cache)


def convert_exchange_query(text: str) -> tuple[bool, str | None]:
    m = _RATE_QUERY_RE.fullmatch(str(text or "").strip())
    if not m:
        return False, None
    try:
        amount = Decimal(m.group(1))
    except Exception:
        return True, "汇率格式错误。"
    if amount <= 0:
        return True, "金额必须大于 0。"
    unit = str(m.group(2) or "").lower()
    if unit in ("u", "usdt"): unit = "usdt"
    elif unit in ("rmb", "cny", "cry", "人民币", "元"): unit = "rmb"
    elif unit == "trx": unit = "trx"
    elif unit == "ton": unit = "ton"
    else: return False, None
    snap = _rate_values()
    cny = Decimal(str(snap.get("usdt_cny") or 0)); trx_u = Decimal(str(snap.get("trx_usdt") or 0)); ton_u = Decimal(str(snap.get("ton_usdt") or 0))
    if cny <= 0 or trx_u <= 0 or ton_u <= 0:
        return True, "⚠️ 实时汇率暂时无法获取，请稍后重试。"
    if unit == "usdt": usdt = amount
    elif unit == "rmb": usdt = amount / cny
    elif unit == "trx": usdt = amount * trx_u
    else: usdt = amount * ton_u
    rmb = usdt * cny; trx = usdt / trx_u; ton = usdt / ton_u
    labels = {
        "usdt": f"{_fmt_decimal(usdt, 4)}u",
        "rmb": f"{_fmt_decimal(rmb, 2)}RMB（CNY）",
        "trx": f"{_fmt_decimal(trx, 4)}TRX",
        "ton": f"{_fmt_decimal(ton, 4)}TON",
    }
    order = {"usdt": ["usdt","rmb","trx","ton"], "rmb": ["rmb","usdt","trx","ton"], "trx": ["trx","usdt","ton","rmb"], "ton": ["ton","usdt","trx","rmb"]}[unit]
    return True, "<b>" + " = ".join(labels[x] for x in order) + "</b>"
