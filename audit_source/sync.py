# -*- coding: utf-8 -*-
from __future__ import annotations

import fcntl
import json
import os
import stat
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from config import LEDGER_DB_PATH, MAIN_LEDGER_DB_PATH, SYNC_DB_PATH
from db import connect, ensure_ledger_business_columns, ensure_ledger_sync_identity_columns, get_settings, init_ledger_db, init_sync_db, set_setting

_TABLES = ("ledger", "item_ledger")
# 企业账本 enterprise_ledger 永远不属于 ShuiBei 同步范围。
# 这是安全边界，不允许通过配置扩展；企业账本只由主机器人维护。
_SYNC_ALLOWED_TABLES = frozenset({"ledger", "item_ledger"})

def _assert_personal_sync_scope() -> None:
    if set(_TABLES) != set(_SYNC_ALLOWED_TABLES):
        raise PermissionError("企业账本或其他业务表禁止同步到水杯记账")
_SYNC_LOCK_PATH = Path(SYNC_DB_PATH).with_suffix(".lock")
_LOCAL_LOCK = threading.RLock()


class SyncDisabled(RuntimeError):
    pass


class MainLedgerUnavailable(RuntimeError):
    pass


@contextmanager
def _global_sync_lock():
    _SYNC_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(_SYNC_LOCK_PATH),
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("sync lock is not a regular file")
        if hasattr(os, "geteuid") and int(st.st_uid) != int(os.geteuid()):
            raise PermissionError("sync lock owner mismatch")
        os.fchmod(fd, 0o600)
        fh = os.fdopen(fd, "r+", encoding="utf-8")
        fd = -1
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()
    finally:
        if fd >= 0:
            os.close(fd)


def set_sync_enabled(owner_id: int, enabled: bool) -> None:
    set_setting(int(owner_id), "sync_enabled", 1 if enabled else 0)


def sync_enabled(owner_id: int) -> bool:
    return bool(int(get_settings(int(owner_id)).get("sync_enabled") or 0))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (str(table),),
    ).fetchone()
    return bool(row)


def _ensure_main() -> None:
    if not Path(MAIN_LEDGER_DB_PATH).is_file():
        raise MainLedgerUnavailable("主账本暂不可用")
    conn = connect(MAIN_LEDGER_DB_PATH)
    try:
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "ledger" not in tables or "item_ledger" not in tables:
            raise MainLedgerUnavailable("主账本暂不可用")
        # r20: 同步身份直接跟随账本行持久化。这样三个 SQLite 只提交一部分时，
        # 下一轮仍可从已提交的数据行恢复 event UUID，而不是重复追加流水。
        ensure_ledger_sync_identity_columns(conn)
        ensure_ledger_business_columns(conn)
        conn.commit()
    finally:
        conn.close()


def _assert_external_links_intact(main_conn: sqlite3.Connection, owner_id: int) -> None:
    """最新 JiZhangBen 的外部到账/收款会把 ledger_record_id 绑定到主账本行。

    ShuiBei 同步绝不能改变这些主账本 ID；发现同步前已有悬空映射时直接拒绝继续，
    避免在未知损坏状态上继续写入。
    """
    if not _table_exists(main_conn, "ledger_external_links_v167"):
        return
    row = main_conn.execute(
        """
        SELECT COUNT(*)
        FROM ledger_external_links_v167 x
        LEFT JOIN ledger l ON l.id=x.ledger_record_id
        WHERE x.owner_id=? AND l.id IS NULL
        """,
        (int(owner_id),),
    ).fetchone()
    if int(row[0] or 0) > 0:
        raise MainLedgerUnavailable("主账本关联状态异常，已取消本次同步")


def _rows_for_owner(conn: sqlite3.Connection, table: str, owner_id: int):
    if table == "ledger":
        return conn.execute(
            """
            SELECT id,owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time,
                   category,cost_micro,sync_event_uuid,sync_origin,sync_origin_row_id
            FROM ledger WHERE owner_id=? ORDER BY id ASC
            """,
            (int(owner_id),),
        ).fetchall()
    if table == "item_ledger":
        return conn.execute(
            """
            SELECT id,owner_id,peer_id,item_name,user_name,action,amount_micro,balance_micro,remark,time,
                   sync_event_uuid,sync_origin,sync_origin_row_id
            FROM item_ledger WHERE owner_id=? ORDER BY id ASC
            """,
            (int(owner_id),),
        ).fetchall()
    raise ValueError("unsupported table")


def _scope_from_row(table: str, row: sqlite3.Row):
    if table == "ledger":
        return (int(row["peer_id"] or 0),)
    return (int(row["peer_id"] or 0), str(row["item_name"] or ""))


def _effect_from_values(action: str, amount: int, balance: int, prev_balance: int) -> dict:
    action = str(action or "")
    if action == "清账":
        return {"kind": "reset", "delta_micro": 0}
    if action in ("入", "收入", "+"):
        return {"kind": "delta", "delta_micro": abs(int(amount or 0))}
    if action in ("出", "支出", "-"):
        return {"kind": "delta", "delta_micro": -abs(int(amount or 0))}
    # 兼容历史动作：按该行相对上一行的真实余额变化保留效果。
    return {"kind": "delta", "delta_micro": int(balance or 0) - int(prev_balance or 0)}


def _payload_from_row(table: str, row: sqlite3.Row, prev_balance: int) -> dict:
    action = str(row["action"] or "")
    balance = int(row["balance_micro"] or 0)
    amount = int(row["amount_micro"] or 0)
    payload = {
        "peer_id": int(row["peer_id"] or 0),
        "user_name": str(row["user_name"] or ""),
        "action": action,
        "amount_micro": amount,
        "remark": str(row["remark"] or ""),
        "time": str(row["time"] or ""),
        "effect": _effect_from_values(action, amount, balance, int(prev_balance or 0)),
    }
    if table == "ledger":
        payload["category"] = str(row["category"] or "")
        payload["cost_micro"] = max(0, int(row["cost_micro"] or 0))
    if table == "item_ledger":
        payload["item_name"] = str(row["item_name"] or "")
    return payload


def _discover_side(sync_conn: sqlite3.Connection, data_conn: sqlite3.Connection, owner_id: int, table: str, side: str) -> int:
    """发现并恢复同步事件。

    r20 的关键约束：event UUID 同时写在账本行本身。即使 main/water/sync 三个
    SQLite 只提交了一部分，下一轮也能从任意已提交的镜像恢复同一事件身份。
    """
    if side not in ("main", "water"):
        raise ValueError("invalid sync side")
    id_col = "main_row_id" if side == "main" else "water_row_id"
    rows = _rows_for_owner(data_conn, table, owner_id)
    prev_by_scope = {}
    created = 0
    now = int(time.time())

    for row in rows:
        scope = _scope_from_row(table, row)
        prev = int(prev_by_scope.get(scope, 0))
        rid = int(row["id"])
        row_uuid = str(row["sync_event_uuid"] or "").strip()
        row_origin = str(row["sync_origin"] or "").strip()
        row_origin_id = int(row["sync_origin_row_id"] or 0)
        payload = _payload_from_row(table, row, prev)

        ev = sync_conn.execute(
            f"""
            SELECT event_uuid,owner_id,table_name,origin,origin_row_id,{id_col}
            FROM sync_events
            WHERE owner_id=? AND table_name=? AND {id_col}=?
            LIMIT 1
            """,
            (int(owner_id), table, rid),
        ).fetchone()

        if ev is None and row_uuid:
            ev = sync_conn.execute(
                """
                SELECT event_uuid,owner_id,table_name,origin,origin_row_id,main_row_id,water_row_id
                FROM sync_events WHERE event_uuid=? LIMIT 1
                """,
                (row_uuid,),
            ).fetchone()
            if ev is not None:
                if int(ev["owner_id"]) != int(owner_id) or str(ev["table_name"]) != table:
                    raise RuntimeError("同步事件身份冲突，已停止同步")
                sync_conn.execute(
                    f"UPDATE sync_events SET {id_col}=?,updated_at=? WHERE event_uuid=?",
                    (rid, now, row_uuid),
                )

        # 源行的本地 UUID 可能因该数据 DB commit 失败而丢失；这时用稳定的
        # (origin, origin_row_id) 从已经恢复/提交的 sync event 找回来。
        if ev is None and not row_uuid:
            ev = sync_conn.execute(
                """
                SELECT event_uuid,owner_id,table_name,origin,origin_row_id,main_row_id,water_row_id
                FROM sync_events
                WHERE owner_id=? AND table_name=? AND origin=? AND origin_row_id=?
                LIMIT 1
                """,
                (int(owner_id), table, side, rid),
            ).fetchone()
            if ev is not None:
                row_uuid = str(ev["event_uuid"])
                row_origin = str(ev["origin"])
                row_origin_id = int(ev["origin_row_id"])
                sync_conn.execute(
                    f"UPDATE sync_events SET {id_col}=?,updated_at=? WHERE event_uuid=?",
                    (rid, now, row_uuid),
                )

        if ev is not None:
            if not row_uuid:
                row_uuid = str(ev["event_uuid"])
            if not row_origin:
                row_origin = str(ev["origin"])
            if not row_origin_id:
                row_origin_id = int(ev["origin_row_id"])

        if not row_uuid:
            row_uuid = uuid.uuid4().hex
            row_origin = side
            row_origin_id = rid
            sync_conn.execute(
                f"""
                INSERT INTO sync_events(
                    event_uuid,owner_id,table_name,origin,origin_row_id,payload_json,{id_col},created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    row_uuid,
                    int(owner_id),
                    table,
                    row_origin,
                    row_origin_id,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    rid,
                    now,
                    now,
                ),
            )
            created += 1
        elif ev is None:
            # UUID 已随另一数据库提交，但 sync.db 没提交：从账本行重建索引事件。
            sync_conn.execute(
                f"""
                INSERT INTO sync_events(
                    event_uuid,owner_id,table_name,origin,origin_row_id,payload_json,{id_col},created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    row_uuid,
                    int(owner_id),
                    table,
                    row_origin or side,
                    row_origin_id or rid,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    rid,
                    now,
                    now,
                ),
            )
            row_origin = row_origin or side
            row_origin_id = row_origin_id or rid
            created += 1

        # 身份字段必须在数据 DB 自己的事务中落盘。重复 UPDATE 是幂等的。
        if (
            str(row["sync_event_uuid"] or "") != row_uuid
            or str(row["sync_origin"] or "") != row_origin
            or int(row["sync_origin_row_id"] or 0) != int(row_origin_id)
        ):
            data_conn.execute(
                f"""
                UPDATE {table}
                SET sync_event_uuid=?,sync_origin=?,sync_origin_row_id=?
                WHERE owner_id=? AND id=?
                """,
                (row_uuid, row_origin, int(row_origin_id), int(owner_id), rid),
            )

        prev_by_scope[scope] = int(row["balance_micro"] or 0)
    return created

def _existing_ids(data_conn: sqlite3.Connection, table: str, owner_id: int) -> set[int]:
    return {
        int(r[0])
        for r in data_conn.execute(
            f"SELECT id FROM {table} WHERE owner_id=?",
            (int(owner_id),),
        ).fetchall()
    }


def _main_row_is_external(main_conn: sqlite3.Connection, table: str, row_id: int) -> bool:
    if table != "ledger" or not _table_exists(main_conn, "ledger_external_links_v167"):
        return False
    return bool(
        main_conn.execute(
            "SELECT 1 FROM ledger_external_links_v167 WHERE ledger_record_id=? LIMIT 1",
            (int(row_id),),
        ).fetchone()
    )


def _scope_rows_for_rebalance(main_conn: sqlite3.Connection, owner_id: int, table: str, target_row: sqlite3.Row):
    if table == "ledger":
        return main_conn.execute(
            """
            SELECT id,action,amount_micro,balance_micro
            FROM ledger
            WHERE owner_id=? AND peer_id=?
            ORDER BY id ASC
            """,
            (int(owner_id), int(target_row["peer_id"] or 0)),
        ).fetchall()
    return main_conn.execute(
        """
        SELECT id,action,amount_micro,balance_micro
        FROM item_ledger
        WHERE owner_id=? AND peer_id=? AND item_name=?
        ORDER BY id ASC
        """,
        (
            int(owner_id),
            int(target_row["peer_id"] or 0),
            str(target_row["item_name"] or ""),
        ),
    ).fetchall()


def _delete_main_row_preserving_ids(main_conn: sqlite3.Connection, owner_id: int, table: str, row_id: int) -> bool:
    """只删除指定镜像行，不重建主表，不改变其它 ledger id，并重算该 scope 后续余额。"""
    if table == "ledger":
        target = main_conn.execute(
            "SELECT id,peer_id FROM ledger WHERE id=? AND owner_id=?",
            (int(row_id), int(owner_id)),
        ).fetchone()
    else:
        target = main_conn.execute(
            "SELECT id,peer_id,item_name FROM item_ledger WHERE id=? AND owner_id=?",
            (int(row_id), int(owner_id)),
        ).fetchone()
    if not target:
        return True
    if _main_row_is_external(main_conn, table, int(row_id)):
        return False

    rows = _scope_rows_for_rebalance(main_conn, owner_id, table, target)
    effects = {}
    prev_original = 0
    for row in rows:
        rid = int(row["id"])
        bal = int(row["balance_micro"] or 0)
        effects[rid] = _effect_from_values(
            str(row["action"] or ""),
            int(row["amount_micro"] or 0),
            bal,
            prev_original,
        )
        prev_original = bal

    if table == "ledger" and _table_exists(main_conn, "main_ai_ledger_undo_tokens_v109"):
        main_conn.execute(
            "DELETE FROM main_ai_ledger_undo_tokens_v109 WHERE owner_id=? AND ledger_id=?",
            (int(owner_id), int(row_id)),
        )
    main_conn.execute(
        f"DELETE FROM {table} WHERE id=? AND owner_id=?",
        (int(row_id), int(owner_id)),
    )

    new_balance = 0
    for row in rows:
        rid = int(row["id"])
        if rid == int(row_id):
            continue
        effect = effects.get(rid) or {"kind": "delta", "delta_micro": 0}
        if str(effect.get("kind") or "") == "reset":
            new_balance = 0
        else:
            new_balance += int(effect.get("delta_micro") or 0)
        main_conn.execute(
            f"UPDATE {table} SET balance_micro=? WHERE id=? AND owner_id=?",
            (int(new_balance), rid, int(owner_id)),
        )
    return True


def _mark_tombstone(sync_conn: sqlite3.Connection, event_uuid: str) -> None:
    sync_conn.execute(
        "UPDATE sync_events SET tombstoned=1,updated_at=? WHERE event_uuid=?",
        (int(time.time()), str(event_uuid)),
    )


def _detect_tombstones(
    sync_conn: sqlite3.Connection,
    main_conn: sqlite3.Connection,
    water_conn: sqlite3.Connection,
    owner_id: int,
    table: str,
) -> tuple[int, int]:
    """删除传播采用“来源感知”规则。

    - 主机器人原生流水：只有主账本删除才是权威删除；水杯侧删掉会在同步时恢复。
    - 水杯原生流水：水杯或主机器人任一侧删除都可撤销该同步事件。
    - 任何主账本外部资金关联行都绝不由水杯删除。
    """
    main_ids = _existing_ids(main_conn, table, owner_id)
    water_ids = _existing_ids(water_conn, table, owner_id)
    rows = sync_conn.execute(
        """
        SELECT event_uuid,origin,main_row_id,water_row_id,tombstoned
        FROM sync_events WHERE owner_id=? AND table_name=?
        """,
        (int(owner_id), table),
    ).fetchall()
    tombstoned = 0
    protected = 0
    for r in rows:
        if int(r["tombstoned"] or 0):
            continue
        event_uuid = str(r["event_uuid"])
        origin = str(r["origin"] or "")
        mid = int(r["main_row_id"]) if r["main_row_id"] is not None else None
        wid = int(r["water_row_id"]) if r["water_row_id"] is not None else None
        main_exists = mid is not None and mid in main_ids
        water_exists = wid is not None and wid in water_ids

        if origin == "main":
            if mid is not None and not main_exists:
                _mark_tombstone(sync_conn, event_uuid)
                tombstoned += 1
            elif wid is not None and not water_exists:
                # 水杯删掉主机器人原生流水不反向删除主账本；下一步会从主账本恢复水杯镜像。
                sync_conn.execute(
                    "UPDATE sync_events SET water_row_id=NULL,updated_at=? WHERE event_uuid=?",
                    (int(time.time()), event_uuid),
                )
            continue

        if origin == "water":
            if wid is not None and not water_exists:
                # 水杯撤销自己的原生流水：允许撤销其同步到主账本的镜像，但绝不碰外部资金关联行。
                if main_exists:
                    if _main_row_is_external(main_conn, table, mid):
                        sync_conn.execute(
                            "UPDATE sync_events SET water_row_id=NULL,updated_at=? WHERE event_uuid=?",
                            (int(time.time()), event_uuid),
                        )
                        protected += 1
                        continue
                    if not _delete_main_row_preserving_ids(main_conn, owner_id, table, mid):
                        sync_conn.execute(
                            "UPDATE sync_events SET water_row_id=NULL,updated_at=? WHERE event_uuid=?",
                            (int(time.time()), event_uuid),
                        )
                        protected += 1
                        continue
                    main_ids.discard(mid)
                _mark_tombstone(sync_conn, event_uuid)
                tombstoned += 1
                continue
            if mid is not None and not main_exists:
                # 主机器人主动撤销了曾从水杯同步来的流水：同步时也从水杯移除。
                _mark_tombstone(sync_conn, event_uuid)
                tombstoned += 1
                continue

    return tombstoned, protected


def _apply_effect(before: int, payload: dict) -> int:
    effect = payload.get("effect") or {}
    if str(effect.get("kind") or "") == "reset":
        return 0
    return int(before) + int(effect.get("delta_micro") or 0)


def _current_main_balance(main_conn: sqlite3.Connection, owner_id: int, table: str, payload: dict) -> int:
    peer_id = int(payload.get("peer_id") or 0)
    if table == "ledger":
        row = main_conn.execute(
            "SELECT balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1",
            (int(owner_id), peer_id),
        ).fetchone()
    else:
        row = main_conn.execute(
            """
            SELECT balance_micro FROM item_ledger
            WHERE owner_id=? AND peer_id=? AND item_name=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(owner_id), peer_id, str(payload.get("item_name") or "")),
        ).fetchone()
    return int(row[0] or 0) if row else 0


def _append_missing_events_to_main(
    sync_conn: sqlite3.Connection,
    main_conn: sqlite3.Connection,
    owner_id: int,
    table: str,
) -> int:
    """水杯新增流水只追加到主账本；绝不 DELETE/重建主表，所以既有 ID 永久保持。"""
    active = sync_conn.execute(
        """
        SELECT event_uuid,origin,origin_row_id,payload_json,main_row_id,created_at
        FROM sync_events
        WHERE owner_id=? AND table_name=? AND tombstoned=0
        ORDER BY created_at ASC, origin_row_id ASC, event_uuid ASC
        """,
        (int(owner_id), table),
    ).fetchall()
    main_ids = _existing_ids(main_conn, table, owner_id)
    appended = 0
    now_i = int(time.time())
    for r in active:
        mid = int(r["main_row_id"]) if r["main_row_id"] is not None else None
        if mid is not None and mid in main_ids:
            continue
        # main 原生事件若主行消失应已在 tombstone 阶段处理；这里不凭 payload 复活主侧删除。
        if str(r["origin"] or "") == "main":
            continue
        try:
            payload = json.loads(str(r["payload_json"] or "{}"))
        except Exception:
            payload = {}
        before = _current_main_balance(main_conn, owner_id, table, payload)
        after = _apply_effect(before, payload)
        peer_id = int(payload.get("peer_id") or 0)
        if table == "ledger":
            cur = main_conn.execute(
                """
                INSERT INTO ledger(owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time,
                                   category,cost_micro,sync_event_uuid,sync_origin,sync_origin_row_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(owner_id),
                    peer_id,
                    str(payload.get("user_name") or "未知"),
                    str(payload.get("action") or ""),
                    int(payload.get("amount_micro") or 0),
                    int(after),
                    str(payload.get("remark") or ""),
                    str(payload.get("time") or ""),
                    str(payload.get("category") or ""),
                    max(0, int(payload.get("cost_micro") or 0)),
                    str(r["event_uuid"]),
                    str(r["origin"] or "water"),
                    int(r["origin_row_id"] or 0),
                ),
            )
        else:
            cur = main_conn.execute(
                """
                INSERT INTO item_ledger(owner_id,peer_id,item_name,user_name,action,amount_micro,balance_micro,remark,time,
                                        sync_event_uuid,sync_origin,sync_origin_row_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(owner_id),
                    peer_id,
                    str(payload.get("item_name") or ""),
                    str(payload.get("user_name") or "未知"),
                    str(payload.get("action") or ""),
                    int(payload.get("amount_micro") or 0),
                    int(after),
                    str(payload.get("remark") or ""),
                    str(payload.get("time") or ""),
                    str(r["event_uuid"]),
                    str(r["origin"] or "water"),
                    int(r["origin_row_id"] or 0),
                ),
            )
        new_id = int(cur.lastrowid)
        main_ids.add(new_id)
        sync_conn.execute(
            "UPDATE sync_events SET main_row_id=?,updated_at=? WHERE event_uuid=?",
            (new_id, now_i, str(r["event_uuid"])),
        )
        appended += 1
    return appended


def _active_event_by_main_id(sync_conn: sqlite3.Connection, owner_id: int, table: str) -> dict[int, str]:
    out = {}
    for r in sync_conn.execute(
        """
        SELECT event_uuid,main_row_id FROM sync_events
        WHERE owner_id=? AND table_name=? AND tombstoned=0 AND main_row_id IS NOT NULL
        """,
        (int(owner_id), table),
    ).fetchall():
        out[int(r["main_row_id"])] = str(r["event_uuid"])
    return out


def _rebuild_water_from_main(
    sync_conn: sqlite3.Connection,
    main_conn: sqlite3.Connection,
    water_conn: sqlite3.Connection,
    owner_id: int,
    table: str,
) -> int:
    """水杯账本是可安全重建的独立副本；按主账本 ID 顺序复制，主账本自身绝不重排。"""
    rows = _rows_for_owner(main_conn, table, owner_id)
    event_by_main = _active_event_by_main_id(sync_conn, owner_id, table)
    # 理论上 discovery 后每个主行都已有 event；若异常则拒绝，而不是生成无法追踪的镜像。
    missing = [int(r["id"]) for r in rows if int(r["id"]) not in event_by_main]
    if missing:
        raise RuntimeError("同步索引暂不完整，请重试")

    water_conn.execute(f"DELETE FROM {table} WHERE owner_id=?", (int(owner_id),))
    sync_conn.execute(
        "UPDATE sync_events SET water_row_id=NULL,updated_at=? WHERE owner_id=? AND table_name=? AND tombstoned=0",
        (int(time.time()), int(owner_id), table),
    )
    now_i = int(time.time())
    count = 0
    for row in rows:
        event_uuid = event_by_main[int(row["id"])]
        origin = str(row["sync_origin"] or "main")
        origin_row_id = int(row["sync_origin_row_id"] or row["id"])
        if table == "ledger":
            cur = water_conn.execute(
                """
                INSERT INTO ledger(owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time,
                                   category,cost_micro,sync_event_uuid,sync_origin,sync_origin_row_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(owner_id),
                    int(row["peer_id"] or 0),
                    str(row["user_name"] or "未知"),
                    str(row["action"] or ""),
                    int(row["amount_micro"] or 0),
                    int(row["balance_micro"] or 0),
                    str(row["remark"] or ""),
                    str(row["time"] or ""),
                    str(row["category"] or ""),
                    max(0, int(row["cost_micro"] or 0)),
                    event_uuid,
                    origin,
                    origin_row_id,
                ),
            )
        else:
            cur = water_conn.execute(
                """
                INSERT INTO item_ledger(owner_id,peer_id,item_name,user_name,action,amount_micro,balance_micro,remark,time,
                                        sync_event_uuid,sync_origin,sync_origin_row_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(owner_id),
                    int(row["peer_id"] or 0),
                    str(row["item_name"] or ""),
                    str(row["user_name"] or "未知"),
                    str(row["action"] or ""),
                    int(row["amount_micro"] or 0),
                    int(row["balance_micro"] or 0),
                    str(row["remark"] or ""),
                    str(row["time"] or ""),
                    event_uuid,
                    origin,
                    origin_row_id,
                ),
            )
        water_id = int(cur.lastrowid)
        sync_conn.execute(
            "UPDATE sync_events SET water_row_id=?,updated_at=? WHERE event_uuid=?",
            (water_id, now_i, event_uuid),
        )
        count += 1
    return count


def preview(owner_id: int) -> dict:
    _assert_personal_sync_scope()
    oid = int(owner_id)
    _ensure_main()
    init_ledger_db()
    main = connect(MAIN_LEDGER_DB_PATH)
    water = connect(LEDGER_DB_PATH)
    try:
        _assert_external_links_intact(main, oid)

        def counts(conn):
            return {
                "ledger": int(conn.execute("SELECT COUNT(*) FROM ledger WHERE owner_id=?", (oid,)).fetchone()[0]),
                "item_ledger": int(conn.execute("SELECT COUNT(*) FROM item_ledger WHERE owner_id=?", (oid,)).fetchone()[0]),
            }

        m = counts(main)
        w = counts(water)
        return {
            "main": m,
            "water": w,
            "main_total": m["ledger"] + m["item_ledger"],
            "water_total": w["ledger"] + w["item_ledger"],
            "enabled": sync_enabled(oid),
        }
    finally:
        main.close()
        water.close()


def sync_once(owner_id: int) -> dict:
    _assert_personal_sync_scope()
    oid = int(owner_id)
    if oid <= 0:
        raise PermissionError("invalid owner")
    if not sync_enabled(oid):
        raise SyncDisabled("账本同步尚未开启")
    _ensure_main()
    init_ledger_db()
    init_sync_db()

    with _LOCAL_LOCK, _global_sync_lock():
        main = connect(MAIN_LEDGER_DB_PATH, 60)
        water = connect(LEDGER_DB_PATH, 60)
        sync_conn = connect(SYNC_DB_PATH, 60)
        run_id = None
        try:
            main.execute("BEGIN IMMEDIATE")
            water.execute("BEGIN IMMEDIATE")
            sync_conn.execute("BEGIN IMMEDIATE")
            _assert_external_links_intact(main, oid)

            main_before = sum(
                int(main.execute(f"SELECT COUNT(*) FROM {t} WHERE owner_id=?", (oid,)).fetchone()[0])
                for t in _TABLES
            )
            water_before = sum(
                int(water.execute(f"SELECT COUNT(*) FROM {t} WHERE owner_id=?", (oid,)).fetchone()[0])
                for t in _TABLES
            )
            cur = sync_conn.execute(
                """
                INSERT INTO sync_runs(owner_id,started_at,status,main_before,water_before)
                VALUES(?,?,?,?,?)
                """,
                (oid, int(time.time()), "running", main_before, water_before),
            )
            run_id = int(cur.lastrowid)

            discovered_main = 0
            discovered_water = 0
            tombstoned = 0
            protected = 0
            appended_to_main = 0
            mirrored_to_water = 0

            for table in _TABLES:
                discovered_main += _discover_side(sync_conn, main, oid, table, "main")
                discovered_water += _discover_side(sync_conn, water, oid, table, "water")
                t_count, p_count = _detect_tombstones(sync_conn, main, water, oid, table)
                tombstoned += t_count
                protected += p_count
                appended_to_main += _append_missing_events_to_main(sync_conn, main, oid, table)
                # 主账本新增/删除完成后再扫描一次，确保所有当前主行都有稳定事件映射。
                discovered_main += _discover_side(sync_conn, main, oid, table, "main")
                mirrored_to_water += _rebuild_water_from_main(sync_conn, main, water, oid, table)

            _assert_external_links_intact(main, oid)
            active_total = int(
                sync_conn.execute(
                    "SELECT COUNT(*) FROM sync_events WHERE owner_id=? AND tombstoned=0",
                    (oid,),
                ).fetchone()[0]
                or 0
            )
            main_after = sum(
                int(main.execute(f"SELECT COUNT(*) FROM {t} WHERE owner_id=?", (oid,)).fetchone()[0])
                for t in _TABLES
            )
            water_after = sum(
                int(water.execute(f"SELECT COUNT(*) FROM {t} WHERE owner_id=?", (oid,)).fetchone()[0])
                for t in _TABLES
            )
            if main_after != water_after:
                raise RuntimeError("同步结果校验失败")

            detail = json.dumps(
                {
                    "discovered_main": discovered_main,
                    "discovered_water": discovered_water,
                    "tombstoned": tombstoned,
                    "protected_main_rows": protected,
                    "appended_to_main": appended_to_main,
                    "mirrored_to_water": mirrored_to_water,
                    "main_ids_preserved": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            sync_conn.execute(
                """
                UPDATE sync_runs
                SET finished_at=?,status='success',merged_count=?,detail=?
                WHERE id=?
                """,
                (int(time.time()), active_total, detail, run_id),
            )

            # 主账本先提交，但其现有行 ID 从不重排；即使后续水杯侧提交异常，也不会破坏主机器人的资金映射。
            main.commit()
            water.commit()
            sync_conn.commit()
            return {
                "main_before": main_before,
                "water_before": water_before,
                "merged_total": active_total,
                "new_from_main": discovered_main,
                "new_from_water": discovered_water,
                "tombstoned": tombstoned,
                "protected_main_rows": protected,
                "appended_to_main": appended_to_main,
            }
        except Exception as exc:
            for c in (main, water, sync_conn):
                try:
                    c.rollback()
                except Exception:
                    pass
            if run_id:
                try:
                    s2 = connect(SYNC_DB_PATH)
                    s2.execute(
                        "UPDATE sync_runs SET finished_at=?,status='failed',detail=? WHERE id=?",
                        (int(time.time()), str(exc)[:1000], run_id),
                    )
                    s2.commit()
                    s2.close()
                except Exception:
                    pass
            raise
        finally:
            main.close()
            water.close()
            sync_conn.close()
