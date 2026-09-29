# -*- coding: utf-8 -*-
"""ShuiBei r29 merchant-customer domain.

Balances remain authoritative in ledger.db. This module only builds an owner-scoped
customer index and additive CRM/reminder metadata around existing Business/ledger data.
"""
from __future__ import annotations

import time
from datetime import datetime

from config import APP_DB_PATH, LEDGER_DB_PATH, ARCHIVE_DB_PATH, DEVELOPER_PROFILE_HISTORY_API_PATH
from db import connect, init_ledger_db, tx
from developer_api import DeveloperAPIUnavailable, get_json
from features import ensure_feature_schema

RECENT_ACTIVE_SECONDS = 7 * 86400
INACTIVE_SECONDS = 30 * 86400


def customer_profile_history(peer_id: int, limit: int = 6) -> dict:
    """Fetch public Telegram profile history through MoQing Developer API only."""
    pid = int(peer_id or 0)
    cap = max(1, min(6, int(limit or 6)))
    if pid <= 0:
        return {"available": True, "total": 0, "history": []}
    try:
        payload = get_json(DEVELOPER_PROFILE_HISTORY_API_PATH, params={"target": str(pid)})
    except DeveloperAPIUnavailable:
        return {"available": False, "total": 0, "history": []}
    rows = payload.get("history")
    if not isinstance(rows, list):
        return {"available": False, "total": 0, "history": []}
    history = []
    for row in rows[:cap]:
        if not isinstance(row, dict):
            continue
        history.append({
            "username": str(row.get("username") or ""),
            "full_name": str(row.get("full_name") or ""),
            "about": str(row.get("about") or ""),
            "observed_at": int(row.get("observed_at") or 0),
        })
    return {
        "available": True,
        "total": int(payload.get("history_total") or len(history)),
        "history": history,
    }


def _ledger_time_ts(value: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return 0


def _latest_ledger_rows(owner_id: int) -> dict[int, dict]:
    init_ledger_db()
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT peer_id, MAX(id) AS max_id
                FROM ledger
                WHERE owner_id=? AND COALESCE(peer_id,0)<>0
                GROUP BY peer_id
            )
            SELECT l.peer_id,l.user_name,l.balance_micro,l.time,l.id
            FROM ledger l JOIN latest x ON x.max_id=l.id
            WHERE l.owner_id=?
            """,
            (int(owner_id), int(owner_id)),
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        pid = int(r["peer_id"] or 0)
        if pid <= 0:
            continue
        out[pid] = {
            "peer_id": pid,
            "user_name": str(r["user_name"] or ""),
            "balance_micro": int(r["balance_micro"] or 0),
            "ledger_time": str(r["time"] or ""),
            "ledger_time_ts": _ledger_time_ts(str(r["time"] or "")),
            "ledger_id": int(r["id"] or 0),
        }
    return out


def _app_customer_maps(owner_id: int):
    ensure_feature_schema()
    conn = connect(APP_DB_PATH)
    try:
        business = {
            int(r["peer_id"]): dict(r)
            for r in conn.execute(
                "SELECT * FROM merchant_customers WHERE owner_id=?",
                (int(owner_id),),
            ).fetchall()
        }
        meta = {
            int(r["peer_id"]): dict(r)
            for r in conn.execute(
                "SELECT * FROM customer_meta WHERE owner_id=?",
                (int(owner_id),),
            ).fetchall()
        }
        collection = {
            int(r["peer_id"]): dict(r)
            for r in conn.execute(
                "SELECT * FROM customer_collection_meta WHERE owner_id=?",
                (int(owner_id),),
            ).fetchall()
        }
        return business, meta, collection
    finally:
        conn.close()


def touch_business_customer(owner_id: int, connection_id: str, peer_id: int, name: str = "", username: str = "", *, incoming: bool, now_ts: int | None = None) -> bool:
    """Update the owner-scoped customer activity index for a valid enabled Business connection."""
    ensure_feature_schema()
    oid, pid = int(owner_id), int(peer_id)
    cid = str(connection_id or "").strip()
    if oid <= 0 or pid <= 0 or not cid:
        return False
    now = int(time.time() if now_ts is None else now_ts)
    clean_name = str(name or "").strip()[:150]
    clean_username = str(username or "").strip().lstrip("@")[:64]
    with tx(APP_DB_PATH, immediate=True) as conn:
        valid = conn.execute(
            "SELECT 1 FROM business_connections WHERE connection_id=? AND owner_id=? AND is_enabled=1 LIMIT 1",
            (cid, oid),
        ).fetchone()
        if not valid:
            return False
        incoming_ts = now if incoming else 0
        outgoing_ts = 0 if incoming else now
        conn.execute(
            """
            INSERT INTO merchant_customers(
                owner_id,peer_id,connection_id,peer_name,peer_username,first_seen_at,last_seen_at,last_incoming_at,last_outgoing_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(owner_id,peer_id) DO UPDATE SET
                connection_id=excluded.connection_id,
                peer_name=CASE WHEN excluded.peer_name<>'' THEN excluded.peer_name ELSE merchant_customers.peer_name END,
                peer_username=CASE WHEN excluded.peer_username<>'' THEN excluded.peer_username ELSE merchant_customers.peer_username END,
                first_seen_at=CASE WHEN merchant_customers.first_seen_at<=0 THEN excluded.first_seen_at ELSE MIN(merchant_customers.first_seen_at,excluded.first_seen_at) END,
                last_seen_at=MAX(merchant_customers.last_seen_at,excluded.last_seen_at),
                last_incoming_at=MAX(merchant_customers.last_incoming_at,excluded.last_incoming_at),
                last_outgoing_at=MAX(merchant_customers.last_outgoing_at,excluded.last_outgoing_at)
            """,
            (oid, pid, cid, clean_name, clean_username, now, now, incoming_ts, outgoing_ts),
        )
    return True


def _merge_customer(owner_id: int, peer_id: int, ledger_row: dict | None, business_row: dict | None, meta_row: dict | None, collection_row: dict | None) -> dict:
    lr, br, mr, cr = ledger_row or {}, business_row or {}, meta_row or {}, collection_row or {}
    business_seen = int(br.get("last_seen_at") or 0)
    ledger_seen = int(lr.get("ledger_time_ts") or 0)
    name = str(br.get("peer_name") or lr.get("user_name") or f"客户 {peer_id}")
    return {
        "owner_id": int(owner_id),
        "peer_id": int(peer_id),
        "name": name,
        "username": str(br.get("peer_username") or ""),
        "balance_micro": int(lr.get("balance_micro") or 0),
        "last_contact_at": max(business_seen, ledger_seen),
        "first_seen_at": int(br.get("first_seen_at") or 0),
        "last_incoming_at": int(br.get("last_incoming_at") or 0),
        "last_outgoing_at": int(br.get("last_outgoing_at") or 0),
        "connection_id": str(br.get("connection_id") or ""),
        "has_business": bool(br),
        "label": str(mr.get("label") or ""),
        "note": str(mr.get("note") or ""),
        "last_reminded_at": int(cr.get("last_reminded_at") or 0),
        "remind_count": int(cr.get("remind_count") or 0),
    }


def list_customers(owner_id: int, mode: str = "all", limit: int = 50, now_ts: int | None = None) -> list[dict]:
    oid = int(owner_id)
    now = int(time.time() if now_ts is None else now_ts)
    mode = str(mode or "all").lower()
    ledger_rows = _latest_ledger_rows(oid)
    business, meta, collection = _app_customer_maps(oid)
    ids = set(ledger_rows) | set(business)
    rows = [_merge_customer(oid, pid, ledger_rows.get(pid), business.get(pid), meta.get(pid), collection.get(pid)) for pid in ids]
    if mode == "debt":
        rows = [r for r in rows if int(r["balance_micro"]) < 0]
    elif mode == "prepay":
        rows = [r for r in rows if int(r["balance_micro"]) > 0]
    elif mode == "recent":
        rows = [r for r in rows if int(r["last_contact_at"]) > 0 and now - int(r["last_contact_at"]) <= RECENT_ACTIVE_SECONDS]
    elif mode == "inactive":
        rows = [r for r in rows if int(r["last_contact_at"]) <= 0 or now - int(r["last_contact_at"]) >= INACTIVE_SECONDS]
    elif mode != "all":
        raise ValueError("unsupported customer filter")
    rows.sort(key=lambda r: (int(r["last_contact_at"]), abs(int(r["balance_micro"])), int(r["peer_id"])), reverse=True)
    return rows[:max(1, min(5000, int(limit or 50)))]


def _known_customer(owner_id: int, peer_id: int) -> bool:
    oid, pid = int(owner_id), int(peer_id)
    ensure_feature_schema(); init_ledger_db()
    conn = connect(APP_DB_PATH)
    try:
        if conn.execute("SELECT 1 FROM merchant_customers WHERE owner_id=? AND peer_id=? LIMIT 1", (oid, pid)).fetchone():
            return True
    finally:
        conn.close()
    conn = connect(LEDGER_DB_PATH)
    try:
        return conn.execute("SELECT 1 FROM ledger WHERE owner_id=? AND peer_id=? LIMIT 1", (oid, pid)).fetchone() is not None
    finally:
        conn.close()


def set_customer_meta(owner_id: int, peer_id: int, *, label: str | None = None, note: str | None = None) -> dict:
    oid, pid = int(owner_id), int(peer_id)
    if not _known_customer(oid, pid):
        raise ValueError("unknown customer")
    ensure_feature_schema()
    conn = connect(APP_DB_PATH)
    try:
        old = conn.execute("SELECT label,note FROM customer_meta WHERE owner_id=? AND peer_id=?", (oid, pid)).fetchone()
        old_label = str(old[0] or "") if old else ""
        old_note = str(old[1] or "") if old else ""
    finally:
        conn.close()
    new_label = old_label if label is None else str(label or "").strip()[:40]
    new_note = old_note if note is None else str(note or "").strip()[:1000]
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO customer_meta(owner_id,peer_id,label,note,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(owner_id,peer_id) DO UPDATE SET label=excluded.label,note=excluded.note,updated_at=excluded.updated_at
            """, (oid, pid, new_label, new_note, now),
        )
    return {"owner_id": oid, "peer_id": pid, "label": new_label, "note": new_note, "updated_at": now}


def _latest_balance(owner_id: int, peer_id: int) -> int | None:
    init_ledger_db()
    conn = connect(LEDGER_DB_PATH)
    try:
        row = conn.execute("SELECT balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1", (int(owner_id), int(peer_id))).fetchone()
        return None if not row else int(row[0] or 0)
    finally:
        conn.close()


def mark_collection_reminded(owner_id: int, peer_id: int, now_ts: int | None = None) -> dict:
    oid, pid = int(owner_id), int(peer_id)
    bal = _latest_balance(oid, pid)
    if bal is None or bal >= 0:
        raise ValueError("customer has no debt")
    now = int(time.time() if now_ts is None else now_ts)
    ensure_feature_schema()
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO customer_collection_meta(owner_id,peer_id,last_reminded_at,remind_count,updated_at) VALUES(?,?,?,1,?)
            ON CONFLICT(owner_id,peer_id) DO UPDATE SET last_reminded_at=excluded.last_reminded_at,remind_count=customer_collection_meta.remind_count+1,updated_at=excluded.updated_at
            """, (oid, pid, now, now),
        )
        row = conn.execute("SELECT last_reminded_at,remind_count,updated_at FROM customer_collection_meta WHERE owner_id=? AND peer_id=?", (oid, pid)).fetchone()
    return {"owner_id": oid, "peer_id": pid, "last_reminded_at": int(row[0]), "remind_count": int(row[1]), "updated_at": int(row[2])}


def customer_detail(owner_id: int, peer_id: int) -> dict | None:
    """Fetch one customer directly instead of materializing/sorting the whole customer set."""
    oid, pid = int(owner_id), int(peer_id)
    ensure_feature_schema()
    init_ledger_db()

    conn = connect(APP_DB_PATH)
    try:
        br = conn.execute(
            "SELECT * FROM merchant_customers WHERE owner_id=? AND peer_id=? LIMIT 1",
            (oid, pid),
        ).fetchone()
        mr = conn.execute(
            "SELECT * FROM customer_meta WHERE owner_id=? AND peer_id=? LIMIT 1",
            (oid, pid),
        ).fetchone()
        cr = conn.execute(
            "SELECT * FROM customer_collection_meta WHERE owner_id=? AND peer_id=? LIMIT 1",
            (oid, pid),
        ).fetchone()
    finally:
        conn.close()

    conn = connect(LEDGER_DB_PATH)
    try:
        hist = conn.execute(
            """SELECT id,user_name,action,amount_micro,balance_micro,remark,time
               FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 8""",
            (oid, pid),
        ).fetchall()
    finally:
        conn.close()

    if not br and not hist:
        return None

    latest = hist[0] if hist else None
    lr = None
    if latest is not None:
        ledger_time = str(latest["time"] or "")
        lr = {
            "peer_id": pid,
            "user_name": str(latest["user_name"] or ""),
            "balance_micro": int(latest["balance_micro"] or 0),
            "ledger_time": ledger_time,
            "ledger_time_ts": _ledger_time_ts(ledger_time),
            "ledger_id": int(latest["id"] or 0),
        }
    out = _merge_customer(
        oid, pid, lr,
        dict(br) if br else None,
        dict(mr) if mr else None,
        dict(cr) if cr else None,
    )
    out["recent_ledger"] = [
        {
            "id": int(r["id"] or 0),
            "action": str(r["action"] or ""),
            "amount_micro": int(r["amount_micro"] or 0),
            "balance_micro": int(r["balance_micro"] or 0),
            "remark": str(r["remark"] or ""),
            "time": str(r["time"] or ""),
        }
        for r in hist
    ]
    return out


def merchant_summary(owner_id: int, now_ts: int | None = None) -> dict:
    """Compute dashboard counters without building/sorting full CRM customer objects."""
    oid = int(owner_id)
    now = int(time.time() if now_ts is None else now_ts)
    ledger_rows = _latest_ledger_rows(oid)
    ensure_feature_schema()
    conn = connect(APP_DB_PATH)
    try:
        business_seen = {
            int(r["peer_id"]): int(r["last_seen_at"] or 0)
            for r in conn.execute(
                "SELECT peer_id,last_seen_at FROM merchant_customers WHERE owner_id=?",
                (oid,),
            ).fetchall()
        }
    finally:
        conn.close()

    ids = set(ledger_rows) | set(business_seen)
    debt_count = debt_amount = prepay_count = prepay_amount = recent_count = 0
    for pid in ids:
        lr = ledger_rows.get(pid) or {}
        bal = int(lr.get("balance_micro") or 0)
        if bal < 0:
            debt_count += 1
            debt_amount += abs(bal)
        elif bal > 0:
            prepay_count += 1
            prepay_amount += bal
        last_contact = max(int(lr.get("ledger_time_ts") or 0), int(business_seen.get(pid) or 0))
        if last_contact > 0 and now - last_contact <= RECENT_ACTIVE_SECONDS:
            recent_count += 1
    return {
        "customer_count": len(ids),
        "debt_count": debt_count,
        "debt_amount_micro": debt_amount,
        "prepay_count": prepay_count,
        "prepay_amount_micro": prepay_amount,
        "recent_count": recent_count,
    }



def bootstrap_customer_index() -> int:
    """Best-effort seed from existing local Business indexes/archive.

    Existing source rows remain untouched. Only customers belonging to an enabled
    owner-matching Business connection are accepted by touch_business_customer().
    """
    ensure_feature_schema()
    seeded = 0
    # Existing last-active row gives trustworthy peer display identity.
    conn = connect(APP_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT owner_id,connection_id,chat_id,peer_name,peer_username,updated_at FROM last_active_business WHERE COALESCE(chat_id,0)>0"
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            if touch_business_customer(int(r[0]), str(r[1] or ""), int(r[2]), str(r[3] or ""), str(r[4] or ""), incoming=True, now_ts=int(r[5] or time.time())):
                seeded += 1
        except Exception:
            pass

    # Archive rows can recover more historical customers. Use identity only from
    # messages actually sent by the customer (sender_id == chat_id), never from
    # the merchant's outgoing message metadata.
    try:
        conn = connect(ARCHIVE_DB_PATH, readonly=True)
        try:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_messages'").fetchone()
            archive_rows = conn.execute(
                """SELECT owner_id,chat_id,business_connection_id,sender_id,sender_name,sender_username,date_ts
                   FROM archive_messages
                   WHERE COALESCE(owner_id,0)>0 AND COALESCE(chat_id,0)>0
                   ORDER BY COALESCE(date_ts,0) ASC,id ASC"""
            ).fetchall() if exists else []
        finally:
            conn.close()
    except Exception:
        archive_rows = []
    grouped = {}
    for r in archive_rows:
        oid, peer = int(r[0] or 0), int(r[1] or 0)
        if oid <= 0 or peer <= 0:
            continue
        key = (oid, peer)
        item = grouped.setdefault(key, {"connection_id":"","name":"","username":"","last_seen":0,"has_incoming":False})
        if str(r[2] or ""):
            item["connection_id"] = str(r[2] or "")
        item["last_seen"] = max(int(item["last_seen"] or 0), int(r[6] or 0))
        if int(r[3] or 0) == peer:
            item["has_incoming"] = True
            if str(r[4] or "").strip(): item["name"] = str(r[4] or "").strip()[:150]
            if str(r[5] or "").strip(): item["username"] = str(r[5] or "").strip().lstrip("@")[:64]
    for (oid, peer), item in grouped.items():
        if not item["has_incoming"] or not item["connection_id"]:
            continue
        try:
            if touch_business_customer(oid, item["connection_id"], peer, item["name"], item["username"], incoming=True, now_ts=int(item["last_seen"] or time.time())):
                seeded += 1
        except Exception:
            pass
    return seeded
