# -*- coding: utf-8 -*-
"""Advanced bookkeeping features shared by production and source-available ShuiBei.

The legacy ledger table stays authoritative for balances.  This module adds
project books, entry metadata/status, reversals, reusable templates, recurring
receivables, close snapshots, customer summaries/timelines and trend series
without rewriting historical ledger rows.
"""
from __future__ import annotations

import calendar
import json
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from config import APP_DB_PATH, LEDGER_DB_PATH
from db import connect, db_generation, init_app_db, init_ledger_db, ledger_hot_locks, tx

_SCHEMA_LOCK = threading.RLock()
_SCHEMA_GENERATION = -1
_ENTRY_STATUSES = frozenset({"posted", "invoiced", "partial", "settled", "waived", "reversed"})


def _row_dict(row) -> dict:
    if not row:
        return {}
    try:
        return dict(row)
    except Exception:
        return {}


def _ledger_ts(value: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        return 0


def ensure_schema() -> None:
    global _SCHEMA_GENERATION
    init_app_db()
    generation = db_generation(APP_DB_PATH)
    if generation > 0 and generation == _SCHEMA_GENERATION:
        return
    with _SCHEMA_LOCK:
        generation = db_generation(APP_DB_PATH)
        if generation > 0 and generation == _SCHEMA_GENERATION:
            return
        with tx(APP_DB_PATH, immediate=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_books(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(owner_id,name)
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_books_owner
                    ON ledger_books(owner_id,archived,id);

                CREATE TABLE IF NOT EXISTS ledger_entry_meta(
                    owner_id INTEGER NOT NULL,
                    ledger_id INTEGER NOT NULL,
                    book_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'posted',
                    reversal_of INTEGER NOT NULL DEFAULT 0,
                    source_message_id INTEGER NOT NULL DEFAULT 0,
                    attachment_ref TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(owner_id,ledger_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_entry_meta_book
                    ON ledger_entry_meta(owner_id,book_id,ledger_id);
                CREATE INDEX IF NOT EXISTS idx_ledger_entry_meta_reversal
                    ON ledger_entry_meta(owner_id,reversal_of);

                CREATE TABLE IF NOT EXISTS ledger_templates(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    peer_id INTEGER NOT NULL DEFAULT 0,
                    book_id INTEGER NOT NULL DEFAULT 0,
                    kind TEXT NOT NULL,
                    amount_micro INTEGER NOT NULL,
                    remark TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    cost_micro INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(owner_id,name)
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_templates_owner
                    ON ledger_templates(owner_id,id);

                CREATE TABLE IF NOT EXISTS recurring_receivables(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    book_id INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL,
                    amount_micro INTEGER NOT NULL,
                    cadence TEXT NOT NULL,
                    next_due_at INTEGER NOT NULL,
                    remark TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    cost_micro INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recurring_due
                    ON recurring_receivables(owner_id,active,next_due_at);

                CREATE TABLE IF NOT EXISTS recurring_receivable_runs(
                    owner_id INTEGER NOT NULL,
                    recurring_id INTEGER NOT NULL,
                    period_key TEXT NOT NULL,
                    ledger_id INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(owner_id,recurring_id,period_key)
                );

                CREATE TABLE IF NOT EXISTS closing_snapshots(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    period_kind TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    start_at INTEGER NOT NULL,
                    end_at INTEGER NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(owner_id,period_kind,period_key)
                );
                CREATE INDEX IF NOT EXISTS idx_closing_snapshots_owner
                    ON closing_snapshots(owner_id,created_at);

                CREATE TABLE IF NOT EXISTS customer_timeline_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    ref_id INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    projection_key TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_customer_timeline_owner_peer
                    ON customer_timeline_events(owner_id,peer_id,created_at,id);

                CREATE TABLE IF NOT EXISTS customer_bookkeeping_profile(
                    owner_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    alias TEXT NOT NULL DEFAULT '',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(owner_id,peer_id)
                );
                CREATE INDEX IF NOT EXISTS idx_customer_bookkeeping_pinned
                    ON customer_bookkeeping_profile(owner_id,pinned,updated_at);

                CREATE TABLE IF NOT EXISTS bookkeeping_goals(
                    owner_id INTEGER NOT NULL,
                    period_key TEXT NOT NULL,
                    target_micro INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(owner_id,period_key)
                );
                """
            )
            timeline_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(customer_timeline_events)").fetchall()}
            if "projection_key" not in timeline_cols:
                conn.execute(
                    "ALTER TABLE customer_timeline_events ADD COLUMN projection_key TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_timeline_projection_key "
                "ON customer_timeline_events(owner_id,projection_key) WHERE projection_key<>''"
            )
        _SCHEMA_GENERATION = db_generation(APP_DB_PATH) or generation


def enqueue_projection_event(conn, owner_id: int, event_key: str, event_type: str, payload: dict) -> None:
    """Persist an APP_DB projection request in the same transaction as ledger truth."""
    key = str(event_key or "").strip()[:160]
    kind = str(event_type or "").strip()[:80]
    if not key or not kind:
        raise ValueError("projection event is invalid")
    conn.execute(
        """INSERT INTO ledger_projection_outbox(
               owner_id,event_key,event_type,payload_json,created_at,applied_at,attempts,last_error
           ) VALUES(?,?,?,?,?,0,0,'')
           ON CONFLICT(owner_id,event_key) DO NOTHING""",
        (
            int(owner_id),
            key,
            kind,
            json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
            int(time.time()),
        ),
    )


def _upsert_projection_meta(
    conn,
    owner_id: int,
    ledger_id: int,
    *,
    book_id: int = 0,
    status: str = "posted",
    reversal_of: int = 0,
    source_message_id: int = 0,
    attachment_ref: str = "",
    now: int,
) -> None:
    oid, lid = int(owner_id), int(ledger_id)
    old = conn.execute(
        "SELECT * FROM ledger_entry_meta WHERE owner_id=? AND ledger_id=?",
        (oid, lid),
    ).fetchone()
    old_d = _row_dict(old)
    created = int(old_d.get("created_at") or now)
    final_book = int(book_id if int(book_id or 0) > 0 else int(old_d.get("book_id") or 0))
    final_source = int(
        source_message_id
        if int(source_message_id or 0) > 0
        else int(old_d.get("source_message_id") or 0)
    )
    final_attachment = str(attachment_ref or old_d.get("attachment_ref") or "")[:500]
    conn.execute(
        """INSERT INTO ledger_entry_meta(
               owner_id,ledger_id,book_id,status,reversal_of,source_message_id,attachment_ref,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(owner_id,ledger_id) DO UPDATE SET
             book_id=excluded.book_id,status=excluded.status,reversal_of=excluded.reversal_of,
             source_message_id=excluded.source_message_id,attachment_ref=excluded.attachment_ref,
             updated_at=excluded.updated_at""",
        (
            oid,
            lid,
            final_book,
            str(status or "posted"),
            max(0, int(reversal_of or 0)),
            final_source,
            final_attachment,
            created,
            int(now),
        ),
    )


def _insert_projection_timeline(
    conn,
    owner_id: int,
    peer_id: int,
    *,
    kind: str,
    ref_id: int,
    payload: dict,
    projection_key: str,
    created_at: int,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO customer_timeline_events(
               owner_id,peer_id,kind,ref_id,payload_json,projection_key,created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            int(owner_id),
            int(peer_id),
            str(kind or "event")[:40],
            max(0, int(ref_id or 0)),
            json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
            str(projection_key or "")[:160],
            int(created_at),
        ),
    )


def _apply_projection_event(owner_id: int, event_key: str, event_type: str, payload: dict, created_at: int) -> None:
    ensure_schema()
    oid = int(owner_id)
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        if event_type == "entry_created":
            lid = int(payload.get("ledger_id") or 0)
            pid = int(payload.get("peer_id") or 0)
            _upsert_projection_meta(
                conn,
                oid,
                lid,
                book_id=int(payload.get("book_id") or 0),
                status=str(payload.get("status") or "posted"),
                source_message_id=int(payload.get("source_message_id") or 0),
                attachment_ref=str(payload.get("attachment_ref") or ""),
                now=now,
            )
            _insert_projection_timeline(
                conn,
                oid,
                pid,
                kind="ledger_created",
                ref_id=lid,
                payload={
                    "ledger_id": lid,
                    "kind": str(payload.get("kind") or ""),
                    "amount_micro": int(payload.get("amount_micro") or 0),
                    "balance_before_micro": int(payload.get("balance_before_micro") or 0),
                    "balance_after_micro": int(payload.get("balance_after_micro") or 0),
                    "book_id": int(payload.get("book_id") or 0),
                    "status": str(payload.get("status") or "posted"),
                },
                projection_key=event_key,
                created_at=created_at,
            )
            return

        if event_type == "entry_reversed":
            original_id = int(payload.get("original_ledger_id") or 0)
            reversal_id = int(payload.get("reversal_ledger_id") or 0)
            pid = int(payload.get("peer_id") or 0)
            book_id = int(payload.get("book_id") or 0)
            _upsert_projection_meta(
                conn,
                oid,
                original_id,
                status="reversed",
                now=now,
            )
            _upsert_projection_meta(
                conn,
                oid,
                reversal_id,
                book_id=book_id,
                status="posted",
                reversal_of=original_id,
                now=now,
            )
            _insert_projection_timeline(
                conn,
                oid,
                pid,
                kind="ledger_reversed",
                ref_id=reversal_id,
                payload={
                    "original_ledger_id": original_id,
                    "reversal_ledger_id": reversal_id,
                    "amount_micro": int(payload.get("amount_micro") or 0),
                },
                projection_key=event_key,
                created_at=created_at,
            )
            return

        raise ValueError("unknown projection event")


def drain_projection_outbox(owner_id: int | None = None, *, limit: int = 100) -> dict:
    """Best-effort idempotent projection replay. Accounting truth never depends on it."""
    init_ledger_db()
    ensure_schema()
    cap = max(1, min(500, int(limit or 100)))
    conn = connect(LEDGER_DB_PATH)
    try:
        if owner_id is None:
            rows = conn.execute(
                """SELECT id,owner_id,event_key,event_type,payload_json,created_at
                   FROM ledger_projection_outbox
                   WHERE applied_at=0 ORDER BY id ASC LIMIT ?""",
                (cap,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id,owner_id,event_key,event_type,payload_json,created_at
                   FROM ledger_projection_outbox
                   WHERE applied_at=0 AND owner_id=? ORDER BY id ASC LIMIT ?""",
                (int(owner_id), cap),
            ).fetchall()
    finally:
        conn.close()

    applied = 0
    failed = 0
    for row in rows:
        outbox_id = int(row["id"])
        oid = int(row["owner_id"])
        key = str(row["event_key"] or "")
        kind = str(row["event_type"] or "")
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
            _apply_projection_event(oid, key, kind, payload, int(row["created_at"] or time.time()))
        except Exception as exc:
            failed += 1
            try:
                with tx(LEDGER_DB_PATH, immediate=True) as lconn:
                    lconn.execute(
                        """UPDATE ledger_projection_outbox
                           SET attempts=attempts+1,last_error=?
                           WHERE id=? AND applied_at=0""",
                        (f"{type(exc).__name__}: {str(exc)[:300]}", outbox_id),
                    )
            except Exception:
                pass
            continue
        try:
            with tx(LEDGER_DB_PATH, immediate=True) as lconn:
                lconn.execute(
                    """UPDATE ledger_projection_outbox
                       SET applied_at=?,attempts=attempts+1,last_error=''
                       WHERE id=?""",
                    (int(time.time()), outbox_id),
                )
            applied += 1
        except Exception:
            failed += 1

    return {"seen": len(rows), "applied": applied, "failed": failed}


def _book_exists(owner_id: int, book_id: int) -> bool:
    bid = int(book_id or 0)
    if bid == 0:
        return True
    ensure_schema()
    conn = connect(APP_DB_PATH)
    try:
        return conn.execute(
            "SELECT 1 FROM ledger_books WHERE owner_id=? AND id=? AND archived=0 LIMIT 1",
            (int(owner_id), bid),
        ).fetchone() is not None
    finally:
        conn.close()


def list_books(owner_id: int, *, include_archived: bool = False) -> list[dict]:
    ensure_schema()
    conn = connect(APP_DB_PATH)
    try:
        sql = "SELECT id,name,archived,created_at,updated_at FROM ledger_books WHERE owner_id=?"
        args: list[Any] = [int(owner_id)]
        if not include_archived:
            sql += " AND archived=0"
        sql += " ORDER BY id ASC"
        rows = conn.execute(sql, tuple(args)).fetchall()
    finally:
        conn.close()
    out = [{"id": 0, "name": "主账本", "archived": False, "created_at": 0, "updated_at": 0}]
    for row in rows:
        d = _row_dict(row)
        out.append({
            "id": int(d.get("id") or 0),
            "name": str(d.get("name") or ""),
            "archived": bool(int(d.get("archived") or 0)),
            "created_at": int(d.get("created_at") or 0),
            "updated_at": int(d.get("updated_at") or 0),
        })
    return out


def create_book(owner_id: int, name: str) -> dict:
    ensure_schema()
    oid = int(owner_id)
    clean = str(name or "").strip()[:40]
    if not clean or clean == "主账本":
        raise ValueError("项目账名称无效")
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        row = conn.execute(
            "SELECT id,name,archived,created_at,updated_at FROM ledger_books WHERE owner_id=? AND name=?",
            (oid, clean),
        ).fetchone()
        if row:
            if int(row["archived"] or 0):
                conn.execute(
                    "UPDATE ledger_books SET archived=0,updated_at=? WHERE owner_id=? AND id=?",
                    (now, oid, int(row["id"])),
                )
            book_id = int(row["id"])
        else:
            cur = conn.execute(
                "INSERT INTO ledger_books(owner_id,name,archived,created_at,updated_at) VALUES(?,?,0,?,?)",
                (oid, clean, now, now),
            )
            book_id = int(cur.lastrowid or 0)
    return {"id": book_id, "name": clean, "archived": False, "created_at": now, "updated_at": now}


def set_book_archived(owner_id: int, book_id: int, archived: bool) -> bool:
    ensure_schema()
    bid = int(book_id)
    if bid <= 0:
        raise ValueError("主账本不能归档")
    with tx(APP_DB_PATH, immediate=True) as conn:
        cur = conn.execute(
            "UPDATE ledger_books SET archived=?,updated_at=? WHERE owner_id=? AND id=?",
            (1 if archived else 0, int(time.time()), int(owner_id), bid),
        )
        return int(cur.rowcount or 0) > 0


def entry_meta_map(owner_id: int, ledger_ids: list[int] | tuple[int, ...]) -> dict[int, dict]:
    ensure_schema()
    try:
        drain_projection_outbox(int(owner_id), limit=100)
    except Exception:
        pass
    ids = sorted({int(x) for x in ledger_ids if int(x) > 0})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    conn = connect(APP_DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT * FROM ledger_entry_meta WHERE owner_id=? AND ledger_id IN ({marks})",
            (int(owner_id), *ids),
        ).fetchall()
    finally:
        conn.close()
    return {int(r["ledger_id"]): _row_dict(r) for r in rows}


def set_entry_meta(
    owner_id: int,
    ledger_id: int,
    *,
    book_id: int | None = None,
    status: str | None = None,
    reversal_of: int | None = None,
    source_message_id: int | None = None,
    attachment_ref: str | None = None,
    _internal: bool = False,
) -> dict:
    ensure_schema()
    oid, lid = int(owner_id), int(ledger_id)
    init_ledger_db()
    conn = connect(LEDGER_DB_PATH)
    try:
        exists = conn.execute(
            "SELECT 1 FROM ledger WHERE owner_id=? AND id=? LIMIT 1", (oid, lid)
        ).fetchone()
    finally:
        conn.close()
    if not exists:
        raise ValueError("账目不存在")

    old = entry_meta_map(oid, [lid]).get(lid, {})
    bid = int(old.get("book_id") or 0) if book_id is None else int(book_id or 0)
    if not _book_exists(oid, bid):
        raise ValueError("项目账不存在")
    st = str(old.get("status") or "posted") if status is None else str(status or "").strip().lower()
    if st not in _ENTRY_STATUSES:
        raise ValueError("账目状态无效")
    if status is not None and st == "reversed" and not _internal:
        raise ValueError("reversed 状态只能由冲正流程设置")
    rev = int(old.get("reversal_of") or 0) if reversal_of is None else max(0, int(reversal_of or 0))
    if reversal_of is not None and rev > 0 and not _internal:
        raise ValueError("reversal_of 只能由冲正流程设置")
    source = int(old.get("source_message_id") or 0) if source_message_id is None else max(0, int(source_message_id or 0))
    attachment = str(old.get("attachment_ref") or "") if attachment_ref is None else str(attachment_ref or "")[:500]
    now = int(time.time())
    created = int(old.get("created_at") or now)
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            """INSERT INTO ledger_entry_meta(
                   owner_id,ledger_id,book_id,status,reversal_of,source_message_id,attachment_ref,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(owner_id,ledger_id) DO UPDATE SET
                   book_id=excluded.book_id,status=excluded.status,reversal_of=excluded.reversal_of,
                   source_message_id=excluded.source_message_id,attachment_ref=excluded.attachment_ref,
                   updated_at=excluded.updated_at""",
            (oid, lid, bid, st, rev, source, attachment, created, now),
        )
    return {
        "owner_id": oid,
        "ledger_id": lid,
        "book_id": bid,
        "status": st,
        "reversal_of": rev,
        "source_message_id": source,
        "attachment_ref": attachment,
        "created_at": created,
        "updated_at": now,
    }


def log_customer_event(owner_id: int, peer_id: int, kind: str, payload: dict | None = None, *, ref_id: int = 0) -> int:
    ensure_schema()
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        cur = conn.execute(
            "INSERT INTO customer_timeline_events(owner_id,peer_id,kind,ref_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                int(owner_id),
                int(peer_id),
                str(kind or "event")[:40],
                max(0, int(ref_id or 0)),
                json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                now,
            ),
        )
        return int(cur.lastrowid or 0)


def record_entry(
    owner_id: int,
    peer_id: int,
    *,
    kind: str,
    amount_micro: int,
    user_name: str = "",
    remark: str = "",
    category: str = "",
    cost_micro: int = 0,
    book_id: int = 0,
    status: str = "posted",
    source_message_id: int = 0,
    attachment_ref: str = "",
) -> dict:
    from customers import customer_detail
    from ledger import currency

    oid, pid = int(owner_id), int(peer_id)
    amount = int(amount_micro or 0)
    if amount <= 0 or amount > 10**15:
        raise ValueError("金额无效")
    clean_kind = str(kind or "").strip().lower()
    if clean_kind not in {"debt", "payment"}:
        raise ValueError("记账类型无效")
    clean_status = str(status or "posted").strip().lower()
    if clean_status not in _ENTRY_STATUSES or clean_status == "reversed":
        raise ValueError("账目状态无效")
    bid = int(book_id or 0)
    if not _book_exists(oid, bid):
        raise ValueError("项目账不存在")
    customer = customer_detail(oid, pid)
    if not customer:
        raise ValueError("客户不存在")

    name = str(user_name or customer.get("name") or "客户")
    delta = -amount if clean_kind == "debt" else amount
    action = "出" if clean_kind == "debt" else "入"
    cat = str(category or "") if clean_kind == "debt" else ""
    cost = max(0, int(cost_micro or 0)) if clean_kind == "debt" else 0
    now_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    init_ledger_db()
    with tx(
        LEDGER_DB_PATH,
        immediate=True,
        pg_locks=ledger_hot_locks(oid, pid),
    ) as conn:
        row = conn.execute(
            "SELECT balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1",
            (oid, pid),
        ).fetchone()
        before = int(row[0] or 0) if row else 0
        after = before + delta
        if not -(2**63) <= after <= 2**63 - 1:
            raise ValueError("余额超出存储范围")
        cur = conn.execute(
            """INSERT INTO ledger(
                   owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time,
                   category,cost_micro,reversal_of,reversed_by
               ) VALUES(?,?,?,?,?,?,?,?,?,?,0,0)""",
            (
                oid, pid, name, action, amount, after, str(remark or "")[:500],
                now_s, cat, cost,
            ),
        )
        lid = int(cur.lastrowid or 0)
        enqueue_projection_event(
            conn,
            oid,
            f"ledger:{lid}:created",
            "entry_created",
            {
                "ledger_id": lid,
                "peer_id": pid,
                "kind": clean_kind,
                "amount_micro": amount,
                "balance_before_micro": before,
                "balance_after_micro": after,
                "book_id": bid,
                "status": clean_status,
                "source_message_id": max(0, int(source_message_id or 0)),
                "attachment_ref": str(attachment_ref or "")[:500],
            },
        )

    try:
        drain_projection_outbox(oid, limit=50)
    except Exception:
        pass

    return {
        "id": lid,
        "peer_id": pid,
        "kind": clean_kind,
        "amount_micro": amount,
        "balance_before_micro": before,
        "balance_after_micro": after,
        "book_id": bid,
        "status": clean_status,
        "reversal_of": 0,
        "source_message_id": max(0, int(source_message_id or 0)),
        "attachment_ref": str(attachment_ref or "")[:500],
        "currency": currency(oid),
    }


def accounting_excluded_ledger_ids(owner_id: int, ledger_ids: list[int] | tuple[int, ...]) -> set[int]:
    """Return accounting-cancelled ids from authoritative ledger reversal links."""
    ids = sorted({int(x) for x in ledger_ids if int(x) > 0})
    if not ids:
        return set()
    init_ledger_db()
    marks = ",".join("?" for _ in ids)
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT id,reversal_of,reversed_by FROM ledger WHERE owner_id=? AND id IN ({marks})",
            (int(owner_id), *ids),
        ).fetchall()
    finally:
        conn.close()
    excluded: set[int] = set()
    for row in rows:
        lid = int(row["id"])
        if int(row["reversal_of"] or 0) > 0 or int(row["reversed_by"] or 0) > 0:
            excluded.add(lid)
    return excluded


def reverse_entry(owner_id: int, ledger_id: int, *, remark: str = "") -> dict:
    from ledger import currency

    ensure_schema()
    oid, lid = int(owner_id), int(ledger_id)
    init_ledger_db()
    try:
        drain_projection_outbox(oid, limit=100)
    except Exception:
        pass
    meta = entry_meta_map(oid, [lid]).get(lid, {})
    book_id = int(meta.get("book_id") or 0)

    probe = connect(LEDGER_DB_PATH)
    try:
        hint = probe.execute(
            "SELECT peer_id FROM ledger WHERE owner_id=? AND id=?",
            (oid, lid),
        ).fetchone()
    finally:
        probe.close()
    if not hint:
        raise ValueError("账目不存在")
    pid_hint = int(hint[0] or 0)

    with tx(
        LEDGER_DB_PATH,
        immediate=True,
        pg_locks=ledger_hot_locks(oid, pid_hint),
    ) as conn:
        row = conn.execute(
            """SELECT id,peer_id,user_name,action,amount_micro,remark,category,cost_micro,
                      reversal_of,reversed_by
               FROM ledger WHERE owner_id=? AND id=?""",
            (oid, lid),
        ).fetchone()
        if not row:
            raise ValueError("账目不存在")
        if int(row["reversal_of"] or 0) > 0:
            raise ValueError("冲正流水不能再次冲正")
        if int(row["reversed_by"] or 0) > 0:
            raise ValueError("该账目已经冲正")
        existing = conn.execute(
            "SELECT id FROM ledger WHERE owner_id=? AND reversal_of=? LIMIT 1",
            (oid, lid),
        ).fetchone()
        if existing:
            raise ValueError("该账目已经冲正")

        action = str(row["action"] or "")
        amount = abs(int(row["amount_micro"] or 0))
        if action in {"入", "收入", "+"}:
            delta, reverse_action = -amount, "出"
        elif action in {"出", "支出", "-"}:
            delta, reverse_action = amount, "入"
        else:
            raise ValueError("该类型流水暂不支持冲正")

        pid = int(row["peer_id"] or 0)
        current = conn.execute(
            "SELECT balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1",
            (oid, pid),
        ).fetchone()
        before = int(current[0] or 0) if current else 0
        after = before + delta
        if not -(2**63) <= after <= 2**63 - 1:
            raise ValueError("余额超出存储范围")

        note = str(remark or "").strip() or f"冲正 #{lid} · {str(row['remark'] or '').strip()}"
        cur = conn.execute(
            """INSERT INTO ledger(
                   owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time,
                   category,cost_micro,reversal_of,reversed_by
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                oid,
                pid,
                str(row["user_name"] or "客户"),
                reverse_action,
                amount,
                after,
                note[:500],
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "",
                0,
                lid,
            ),
        )
        rid = int(cur.lastrowid or 0)
        updated = conn.execute(
            "UPDATE ledger SET reversed_by=? WHERE owner_id=? AND id=? AND reversed_by=0",
            (rid, oid, lid),
        )
        if int(updated.rowcount or 0) != 1:
            raise ValueError("该账目已经冲正")

        enqueue_projection_event(
            conn,
            oid,
            f"ledger:{lid}:reversed:{rid}",
            "entry_reversed",
            {
                "original_ledger_id": lid,
                "reversal_ledger_id": rid,
                "peer_id": pid,
                "book_id": book_id,
                "amount_micro": amount,
            },
        )

    try:
        drain_projection_outbox(oid, limit=50)
    except Exception:
        pass

    return {
        "original_ledger_id": lid,
        "reversal_ledger_id": rid,
        "amount_micro": amount,
        "balance_before_micro": before,
        "balance_after_micro": after,
        "currency": currency(oid),
    }


def create_template(
    owner_id: int,
    name: str,
    *,
    kind: str,
    amount_micro: int,
    peer_id: int = 0,
    book_id: int = 0,
    remark: str = "",
    category: str = "",
    cost_micro: int = 0,
) -> dict:
    ensure_schema()
    oid = int(owner_id)
    clean_name = str(name or "").strip()[:50]
    clean_kind = str(kind or "").strip().lower()
    amount = int(amount_micro or 0)
    if not clean_name or clean_kind not in {"debt", "payment"} or amount <= 0:
        raise ValueError("模板参数无效")
    if not _book_exists(oid, int(book_id or 0)):
        raise ValueError("项目账不存在")
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            """INSERT INTO ledger_templates(
                   owner_id,name,peer_id,book_id,kind,amount_micro,remark,category,cost_micro,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(owner_id,name) DO UPDATE SET
                   peer_id=excluded.peer_id,book_id=excluded.book_id,kind=excluded.kind,
                   amount_micro=excluded.amount_micro,remark=excluded.remark,category=excluded.category,
                   cost_micro=excluded.cost_micro,updated_at=excluded.updated_at""",
            (
                oid,
                clean_name,
                max(0, int(peer_id or 0)),
                int(book_id or 0),
                clean_kind,
                amount,
                str(remark or "")[:500],
                str(category or "")[:40],
                max(0, int(cost_micro or 0)),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM ledger_templates WHERE owner_id=? AND name=?", (oid, clean_name)
        ).fetchone()
    return _row_dict(row)


def list_templates(owner_id: int, *, peer_id: int = 0, limit: int = 50) -> list[dict]:
    ensure_schema()
    oid, pid = int(owner_id), int(peer_id or 0)
    conn = connect(APP_DB_PATH)
    try:
        if pid > 0:
            rows = conn.execute(
                "SELECT * FROM ledger_templates WHERE owner_id=? AND (peer_id=0 OR peer_id=?) ORDER BY id DESC LIMIT ?",
                (oid, pid, max(1, min(100, int(limit)))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ledger_templates WHERE owner_id=? ORDER BY id DESC LIMIT ?",
                (oid, max(1, min(100, int(limit)))),
            ).fetchall()
    finally:
        conn.close()
    return [_row_dict(r) for r in rows]


def apply_template(owner_id: int, template_id: int, *, peer_id: int = 0) -> dict:
    ensure_schema()
    oid = int(owner_id)
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute(
            "SELECT * FROM ledger_templates WHERE owner_id=? AND id=?", (oid, int(template_id))
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError("模板不存在")
    d = _row_dict(row)
    pid = int(peer_id or d.get("peer_id") or 0)
    if pid <= 0:
        raise ValueError("模板未指定客户")
    result = record_entry(
        oid,
        pid,
        kind=str(d.get("kind") or ""),
        amount_micro=int(d.get("amount_micro") or 0),
        remark=str(d.get("remark") or ""),
        category=str(d.get("category") or ""),
        cost_micro=int(d.get("cost_micro") or 0),
        book_id=int(d.get("book_id") or 0),
    )
    log_customer_event(oid, pid, "template_applied", {"template_id": int(template_id)}, ref_id=int(result["id"]))
    return result


def _advance_due(ts: int, cadence: str) -> int:
    dt = datetime.fromtimestamp(int(ts))
    if cadence == "weekly":
        return int((dt + timedelta(days=7)).timestamp())
    year, month = dt.year, dt.month + 1
    if month > 12:
        year += 1
        month = 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return int(datetime(year, month, day, dt.hour, dt.minute, dt.second).timestamp())


def create_recurring_receivable(
    owner_id: int,
    peer_id: int,
    *,
    title: str,
    amount_micro: int,
    cadence: str,
    next_due_at: int,
    book_id: int = 0,
    remark: str = "",
    category: str = "",
    cost_micro: int = 0,
) -> dict:
    from customers import customer_detail

    ensure_schema()
    oid, pid = int(owner_id), int(peer_id)
    clean_title = str(title or "").strip()[:80]
    clean_cadence = str(cadence or "").strip().lower()
    amount = int(amount_micro or 0)
    due = int(next_due_at or 0)
    if not customer_detail(oid, pid):
        raise ValueError("客户不存在")
    if not clean_title or clean_cadence not in {"weekly", "monthly"} or amount <= 0 or due <= 0:
        raise ValueError("周期应收参数无效")
    if not _book_exists(oid, int(book_id or 0)):
        raise ValueError("项目账不存在")
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        cur = conn.execute(
            """INSERT INTO recurring_receivables(
                   owner_id,peer_id,book_id,title,amount_micro,cadence,next_due_at,remark,category,cost_micro,
                   active,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)""",
            (
                oid,
                pid,
                int(book_id or 0),
                clean_title,
                amount,
                clean_cadence,
                due,
                str(remark or "")[:500],
                str(category or "")[:40],
                max(0, int(cost_micro or 0)),
                now,
                now,
            ),
        )
        rid = int(cur.lastrowid or 0)
        row = conn.execute("SELECT * FROM recurring_receivables WHERE owner_id=? AND id=?", (oid, rid)).fetchone()
    log_customer_event(oid, pid, "recurring_created", {"recurring_id": rid, "title": clean_title}, ref_id=rid)
    return _row_dict(row)


def list_recurring(owner_id: int, *, peer_id: int = 0, active_only: bool = True) -> list[dict]:
    ensure_schema()
    sql = "SELECT * FROM recurring_receivables WHERE owner_id=?"
    args: list[Any] = [int(owner_id)]
    if int(peer_id or 0) > 0:
        sql += " AND peer_id=?"
        args.append(int(peer_id))
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY next_due_at ASC,id ASC"
    conn = connect(APP_DB_PATH)
    try:
        rows = conn.execute(sql, tuple(args)).fetchall()
    finally:
        conn.close()
    return [_row_dict(r) for r in rows]


def set_recurring_active(owner_id: int, recurring_id: int, active: bool) -> bool:
    ensure_schema()
    with tx(APP_DB_PATH, immediate=True) as conn:
        cur = conn.execute(
            "UPDATE recurring_receivables SET active=?,updated_at=? WHERE owner_id=? AND id=?",
            (1 if active else 0, int(time.time()), int(owner_id), int(recurring_id)),
        )
        return int(cur.rowcount or 0) > 0


def process_due_recurring(owner_id: int, *, now_ts: int | None = None, max_runs: int = 20) -> list[dict]:
    from receivables import set_due

    ensure_schema()
    oid = int(owner_id)
    now = int(time.time() if now_ts is None else now_ts)
    created: list[dict] = []
    schedules = list_recurring(oid, active_only=True)
    remaining = max(1, min(100, int(max_runs)))
    for schedule in schedules:
        due = int(schedule.get("next_due_at") or 0)
        cadence = str(schedule.get("cadence") or "monthly")
        rid = int(schedule.get("id") or 0)
        while due > 0 and due <= now and remaining > 0:
            period_key = time.strftime("%Y-%m-%d", time.localtime(due))
            claimed = False
            with tx(APP_DB_PATH, immediate=True) as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO recurring_receivable_runs(owner_id,recurring_id,period_key,ledger_id,created_at) VALUES(?,?,?,0,?)",
                    (oid, rid, period_key, now),
                )
                claimed = int(cur.rowcount or 0) > 0
            if claimed:
                try:
                    result = record_entry(
                        oid,
                        int(schedule.get("peer_id") or 0),
                        kind="debt",
                        amount_micro=int(schedule.get("amount_micro") or 0),
                        remark=(str(schedule.get("remark") or "").strip() or str(schedule.get("title") or "")),
                        category=str(schedule.get("category") or ""),
                        cost_micro=int(schedule.get("cost_micro") or 0),
                        book_id=int(schedule.get("book_id") or 0),
                        status="invoiced",
                    )
                    with tx(APP_DB_PATH, immediate=True) as conn:
                        conn.execute(
                            "UPDATE recurring_receivable_runs SET ledger_id=? WHERE owner_id=? AND recurring_id=? AND period_key=?",
                            (int(result["id"]), oid, rid, period_key),
                        )
                    try:
                        set_due(oid, int(schedule.get("peer_id") or 0), due, str(schedule.get("title") or ""))
                    except Exception:
                        pass
                    log_customer_event(
                        oid,
                        int(schedule.get("peer_id") or 0),
                        "recurring_generated",
                        {"recurring_id": rid, "period_key": period_key, "ledger_id": int(result["id"])},
                        ref_id=int(result["id"]),
                    )
                    created.append(result)
                    remaining -= 1
                except Exception:
                    with tx(APP_DB_PATH, immediate=True) as conn:
                        conn.execute(
                            "DELETE FROM recurring_receivable_runs WHERE owner_id=? AND recurring_id=? AND period_key=? AND ledger_id=0",
                            (oid, rid, period_key),
                        )
                    raise
            due = _advance_due(due, cadence)
            with tx(APP_DB_PATH, immediate=True) as conn:
                conn.execute(
                    "UPDATE recurring_receivables SET next_due_at=?,updated_at=? WHERE owner_id=? AND id=?",
                    (due, now, oid, rid),
                )
        if remaining <= 0:
            break
    return created


def create_close_snapshot(owner_id: int, period_kind: str, *, now_ts: int | None = None) -> dict:
    from receivables import report_summary

    ensure_schema()
    kind = str(period_kind or "").strip().lower()
    if kind not in {"week", "month"}:
        raise ValueError("仅支持周结或月结")
    now = int(time.time() if now_ts is None else now_ts)
    dt = datetime.fromtimestamp(now)
    period_key = dt.strftime("%Y-%m") if kind == "month" else f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
    existing = None
    conn = connect(APP_DB_PATH)
    try:
        existing = conn.execute(
            "SELECT * FROM closing_snapshots WHERE owner_id=? AND period_kind=? AND period_key=?",
            (int(owner_id), kind, period_key),
        ).fetchone()
    finally:
        conn.close()
    if existing:
        d = _row_dict(existing)
        d["summary"] = json.loads(str(d.pop("summary_json") or "{}"))
        return d
    summary = report_summary(int(owner_id), kind, now)
    created_at = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "INSERT INTO closing_snapshots(owner_id,period_kind,period_key,start_at,end_at,summary_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                int(owner_id),
                kind,
                period_key,
                int(summary.get("start_at") or 0),
                int(summary.get("end_at") or now),
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM closing_snapshots WHERE owner_id=? AND period_kind=? AND period_key=?",
            (int(owner_id), kind, period_key),
        ).fetchone()
    d = _row_dict(row)
    d["summary"] = json.loads(str(d.pop("summary_json") or "{}"))
    return d


def list_close_snapshots(owner_id: int, *, limit: int = 12) -> list[dict]:
    ensure_schema()
    conn = connect(APP_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT * FROM closing_snapshots WHERE owner_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (int(owner_id), max(1, min(50, int(limit)))),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        d = _row_dict(row)
        d["summary"] = json.loads(str(d.pop("summary_json") or "{}"))
        out.append(d)
    return out


def customer_summary(owner_id: int, peer_id: int) -> dict:
    from customers import customer_detail
    from ledger import currency
    from receivables import get_due

    oid, pid = int(owner_id), int(peer_id)
    customer = customer_detail(oid, pid)
    if not customer:
        raise ValueError("客户不存在")
    init_ledger_db()
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id,time,action,amount_micro,balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id ASC LIMIT 5000",
            (oid, pid),
        ).fetchall()
    finally:
        conn.close()
    excluded = accounting_excluded_ledger_ids(oid, [int(r["id"]) for r in rows])
    effective = [r for r in rows if int(r["id"]) not in excluded]
    inflow = sum(int(r["amount_micro"] or 0) for r in effective if str(r["action"] or "") in {"入", "收入", "+"})
    outflow = sum(int(r["amount_micro"] or 0) for r in effective if str(r["action"] or "") in {"出", "支出", "-"})
    first_ts = _ledger_ts(str(rows[0]["time"] or "")) if rows else 0
    last_ts = _ledger_ts(str(rows[-1]["time"] or "")) if rows else 0
    settlements: list[int] = []
    negative_since = 0
    longest = 0
    prev_balance = 0
    for row in rows:
        ts = _ledger_ts(str(row["time"] or ""))
        bal = int(row["balance_micro"] or 0)
        if prev_balance >= 0 and bal < 0 and ts:
            negative_since = ts
        if prev_balance < 0 and bal >= 0 and negative_since and ts >= negative_since:
            duration = ts - negative_since
            settlements.append(duration)
            longest = max(longest, duration)
            negative_since = 0
        prev_balance = bal
    now = int(time.time())
    if negative_since:
        longest = max(longest, max(0, now - negative_since))
    due = get_due(oid, pid)
    due_at = int(due.get("due_at") or 0)
    overdue_days = max(0, (now - due_at + 86399) // 86400) if due_at and due_at < now and int(customer.get("balance_micro") or 0) < 0 else 0
    return {
        "peer_id": pid,
        "record_count": len(rows),
        "effective_record_count": len(effective),
        "inflow_micro": inflow,
        "outflow_micro": outflow,
        "turnover_micro": inflow + outflow,
        "current_balance_micro": int(customer.get("balance_micro") or 0),
        "first_trade_at": first_ts,
        "last_trade_at": last_ts,
        "settlement_count": len(settlements),
        "avg_settlement_seconds": int(sum(settlements) / len(settlements)) if settlements else 0,
        "longest_unsettled_seconds": int(longest),
        "overdue_days": int(overdue_days),
        "currency": currency(oid),
    }


def customer_timeline(owner_id: int, peer_id: int, *, limit: int = 30) -> list[dict]:
    ensure_schema()
    oid, pid = int(owner_id), int(peer_id)
    cap = max(1, min(100, int(limit)))
    init_ledger_db()
    conn = connect(LEDGER_DB_PATH)
    try:
        ledger_rows = conn.execute(
            """SELECT id,time,action,amount_micro,balance_micro,remark,category,cost_micro
               FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT ?""",
            (oid, pid, cap),
        ).fetchall()
    finally:
        conn.close()
    meta = entry_meta_map(oid, [int(r["id"]) for r in ledger_rows])
    events: list[dict] = []
    for r in ledger_rows:
        lid = int(r["id"])
        m = meta.get(lid, {})
        events.append({
            "kind": "ledger",
            "created_at": _ledger_ts(str(r["time"] or "")),
            "ledger_id": lid,
            "action": str(r["action"] or ""),
            "amount_micro": int(r["amount_micro"] or 0),
            "balance_micro": int(r["balance_micro"] or 0),
            "remark": str(r["remark"] or ""),
            "category": str(r["category"] or ""),
            "cost_micro": int(r["cost_micro"] or 0),
            "status": str(m.get("status") or "posted"),
            "book_id": int(m.get("book_id") or 0),
            "reversal_of": int(m.get("reversal_of") or 0),
        })
    conn = connect(APP_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id,kind,ref_id,payload_json,created_at FROM customer_timeline_events WHERE owner_id=? AND peer_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (oid, pid, cap),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            payload = json.loads(str(r["payload_json"] or "{}"))
        except Exception:
            payload = {}
        events.append({
            "kind": str(r["kind"] or "event"),
            "created_at": int(r["created_at"] or 0),
            "ref_id": int(r["ref_id"] or 0),
            "payload": payload,
        })
    events.sort(key=lambda x: (int(x.get("created_at") or 0), int(x.get("ledger_id") or x.get("ref_id") or 0)), reverse=True)
    return events[:cap]


def trend_series(
    owner_id: int,
    *,
    days: int = 30,
    start_ts: int = 0,
    end_ts: int = 0,
    book_id: int | None = None,
) -> dict:
    from ledger import currency

    oid = int(owner_id)
    now = int(time.time())
    if int(start_ts or 0) > 0 and int(end_ts or 0) >= int(start_ts):
        start_i, end_i = int(start_ts), int(end_ts)
    else:
        count = max(2, min(180, int(days or 30)))
        end_dt = datetime.fromtimestamp(now)
        end_i = now
        start_dt = datetime(end_dt.year, end_dt.month, end_dt.day) - timedelta(days=count - 1)
        start_i = int(start_dt.timestamp())
    start_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_i))
    end_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_i))
    init_ledger_db()
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT id,time,action,amount_micro,category,cost_micro FROM ledger
               WHERE owner_id=? AND time>=? AND time<=? ORDER BY id ASC""",
            (oid, start_s, end_s),
        ).fetchall()
    finally:
        conn.close()
    ids = [int(r["id"]) for r in rows]
    meta = entry_meta_map(oid, ids)
    excluded = accounting_excluded_ledger_ids(oid, ids)
    requested_book = None if book_id is None else int(book_id)
    daily: dict[str, dict] = {}
    cursor = datetime.fromtimestamp(start_i)
    end_day = datetime.fromtimestamp(end_i)
    cursor = datetime(cursor.year, cursor.month, cursor.day)
    end_day = datetime(end_day.year, end_day.month, end_day.day)
    while cursor <= end_day:
        key = cursor.strftime("%Y-%m-%d")
        daily[key] = {
            "date": key,
            "inflow_micro": 0,
            "outflow_micro": 0,
            "gross_profit_micro": 0,
            "record_count": 0,
        }
        cursor += timedelta(days=1)
    for row in rows:
        lid = int(row["id"])
        if lid in excluded:
            continue
        if requested_book is not None:
            row_book = int(meta.get(lid, {}).get("book_id") or 0)
            if row_book != requested_book:
                continue
        day = str(row["time"] or "")[:10]
        item = daily.get(day)
        if item is None:
            continue
        action = str(row["action"] or "")
        amount = abs(int(row["amount_micro"] or 0))
        item["record_count"] += 1
        if action in {"入", "收入", "+"}:
            item["inflow_micro"] += amount
        elif action in {"出", "支出", "-"}:
            item["outflow_micro"] += amount
            if str(row["category"] or "").strip():
                item["gross_profit_micro"] += amount - max(0, int(row["cost_micro"] or 0))
    return {
        "start_at": start_i,
        "end_at": end_i,
        "book_id": requested_book,
        "currency": currency(oid),
        "series": list(daily.values()),
    }


def book_overview(owner_id: int) -> list[dict]:
    oid = int(owner_id)
    books = {int(b["id"]): b for b in list_books(oid)}
    init_ledger_db()
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id,action,amount_micro FROM ledger WHERE owner_id=? ORDER BY id ASC",
            (oid,),
        ).fetchall()
    finally:
        conn.close()
    ids = [int(r["id"]) for r in rows]
    meta = entry_meta_map(oid, ids)
    excluded = accounting_excluded_ledger_ids(oid, ids)
    agg: dict[int, dict] = {
        bid: {
            "id": bid,
            "name": str(book.get("name") or "项目账"),
            "record_count": 0,
            "inflow_micro": 0,
            "outflow_micro": 0,
        }
        for bid, book in books.items()
    }
    for row in rows:
        lid = int(row["id"])
        if lid in excluded:
            continue
        bid = int(meta.get(lid, {}).get("book_id") or 0)
        if bid not in agg:
            continue
        item = agg[bid]
        item["record_count"] += 1
        action = str(row["action"] or "")
        amount = abs(int(row["amount_micro"] or 0))
        if action in {"入", "收入", "+"}:
            item["inflow_micro"] += amount
        elif action in {"出", "支出", "-"}:
            item["outflow_micro"] += amount
    for item in agg.values():
        item["net_micro"] = int(item["inflow_micro"]) - int(item["outflow_micro"])
    return [agg[k] for k in sorted(agg)]


def customer_profile_map(owner_id: int, peer_ids: list[int] | tuple[int, ...]) -> dict[int, dict]:
    ensure_schema()
    ids = sorted({int(x) for x in peer_ids if int(x) > 0})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    conn = connect(APP_DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT peer_id,alias,pinned,updated_at FROM customer_bookkeeping_profile WHERE owner_id=? AND peer_id IN ({marks})",
            (int(owner_id), *ids),
        ).fetchall()
    finally:
        conn.close()
    return {
        int(r["peer_id"]): {
            "peer_id": int(r["peer_id"]),
            "alias": str(r["alias"] or ""),
            "pinned": bool(int(r["pinned"] or 0)),
            "updated_at": int(r["updated_at"] or 0),
        }
        for r in rows
    }


def set_customer_profile(owner_id: int, peer_id: int, *, alias: str | None = None, pinned: bool | None = None) -> dict:
    from customers import customer_detail

    ensure_schema()
    oid, pid = int(owner_id), int(peer_id)
    if not customer_detail(oid, pid):
        raise ValueError("客户不存在")
    old = customer_profile_map(oid, [pid]).get(pid, {})
    clean_alias = str(old.get("alias") or "") if alias is None else str(alias or "").strip()[:60]
    pin_value = bool(old.get("pinned")) if pinned is None else bool(pinned)
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            """INSERT INTO customer_bookkeeping_profile(owner_id,peer_id,alias,pinned,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(owner_id,peer_id) DO UPDATE SET
                 alias=excluded.alias,pinned=excluded.pinned,updated_at=excluded.updated_at""",
            (oid, pid, clean_alias, 1 if pin_value else 0, now),
        )
    log_customer_event(
        oid,
        pid,
        "customer_profile_updated",
        {"alias": clean_alias, "pinned": pin_value},
    )
    return {"peer_id": pid, "alias": clean_alias, "pinned": pin_value, "updated_at": now}


def current_goal(owner_id: int, *, now_ts: int | None = None) -> dict:
    from receivables import report_summary

    ensure_schema()
    now = int(time.time() if now_ts is None else now_ts)
    key = time.strftime("%Y-%m", time.localtime(now))
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute(
            "SELECT target_micro,updated_at FROM bookkeeping_goals WHERE owner_id=? AND period_key=?",
            (int(owner_id), key),
        ).fetchone()
    finally:
        conn.close()
    target = int(row["target_micro"] or 0) if row else 0
    month = report_summary(int(owner_id), "month", now)
    current = int(month.get("inflow_micro") or 0)
    return {
        "period_key": key,
        "target_micro": target,
        "current_micro": current,
        "progress_percent": min(999.0, round((current / target * 100.0), 1)) if target > 0 else 0.0,
        "updated_at": int(row["updated_at"] or 0) if row else 0,
        "currency": str(month.get("currency") or ""),
    }


def set_goal(owner_id: int, target_micro: int, *, now_ts: int | None = None) -> dict:
    ensure_schema()
    target = max(0, int(target_micro or 0))
    if target > 10**18:
        raise ValueError("目标金额过大")
    now = int(time.time() if now_ts is None else now_ts)
    key = time.strftime("%Y-%m", time.localtime(now))
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            """INSERT INTO bookkeeping_goals(owner_id,period_key,target_micro,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(owner_id,period_key) DO UPDATE SET
                 target_micro=excluded.target_micro,updated_at=excluded.updated_at""",
            (int(owner_id), key, target, int(time.time())),
        )
    return current_goal(int(owner_id), now_ts=now)
