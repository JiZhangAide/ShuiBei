# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from config import APP_DB_PATH, ARCHIVE_DB_PATH, LEDGER_DB_PATH, SYNC_DB_PATH, DEFAULT_CURRENCY

_DB_LOCK = threading.RLock()
_SCHEMA_READY: set[tuple[str, str]] = set()
_WAL_READY: dict[str, tuple[int, int]] = {}
_DB_GENERATION: dict[str, int] = {}


_DB_PERMISSION_WARNED: set[str] = set()


def _strict_db_permissions() -> bool:
    raw = os.environ.get("SHUIBEI_STRICT_DB_PERMISSIONS")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _db_permission_failure(path: Path, reason: str) -> None:
    message = f"[ShuiBei][SECURITY] database permission hardening failed for {path}: {reason}"
    if _strict_db_permissions():
        raise PermissionError(message)
    key = str(path)
    if key not in _DB_PERMISSION_WARNED:
        _DB_PERMISSION_WARNED.add(key)
        print(message, file=sys.stderr, flush=True)


def _file_identity(path: Path | str) -> tuple[int, int] | None:
    try:
        st = Path(path).stat()
        return (int(st.st_dev), int(st.st_ino))
    except OSError:
        return None


def _is_local_private_db(path: Path | str) -> bool:
    try:
        rp = Path(path).resolve()
        return rp in {
            Path(LEDGER_DB_PATH).resolve(), Path(APP_DB_PATH).resolve(),
            Path(ARCHIVE_DB_PATH).resolve(), Path(SYNC_DB_PATH).resolve(),
        }
    except Exception:
        return False


def _harden_local_db_mode(path: Path | str) -> None:
    """Require owner-only permissions for ShuiBei-owned SQLite databases."""
    if not _is_local_private_db(path):
        return
    p = Path(path)
    try:
        st = p.stat()
        if hasattr(os, "geteuid") and int(st.st_uid) != int(os.geteuid()):
            _db_permission_failure(p, "database file owner does not match the running user")
            return
        if (st.st_mode & 0o077) != 0:
            os.chmod(p, 0o600)
            st = p.stat()
        if (st.st_mode & 0o077) != 0:
            _db_permission_failure(p, f"mode remained {oct(st.st_mode & 0o777)} instead of 0o600")
    except PermissionError:
        raise
    except Exception as exc:
        _db_permission_failure(p, f"{type(exc).__name__}: {str(exc)[:200]}")


def _schema_key(kind: str, path: Path | str) -> tuple[str, str]:
    return (str(kind), str(Path(path).resolve()))


def db_generation(path: Path | str) -> int:
    return int(_DB_GENERATION.get(str(Path(path).resolve()), 0))


def _bump_db_generation(path: Path | str) -> None:
    key = str(Path(path).resolve())
    _DB_GENERATION[key] = int(_DB_GENERATION.get(key, 0)) + 1


def connect(path: Path | str, timeout: float = 30.0, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=timeout, check_same_thread=False)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not readonly:
        _harden_local_db_mode(p)
    try:
        conn.execute(f"PRAGMA busy_timeout={max(1000, int(timeout * 1000))}")
        if not readonly:
            # journal_mode is persistent per database file. Re-applying it on every
            # short-lived connection adds locking/syscall overhead on hot message paths.
            key = str(p.resolve())
            ident = _file_identity(p)
            with _DB_LOCK:
                if ident is None or _WAL_READY.get(key) != ident:
                    conn.execute("PRAGMA journal_mode=WAL")
                    ident = _file_identity(p)
                    if ident is not None:
                        _WAL_READY[key] = ident
            # These PRAGMAs are connection-local and must still be applied each time.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    return conn


def ledger_hot_locks(owner_id: int, peer_id: int):
    """SQLite compatibility shim; BEGIN IMMEDIATE provides the local write lock."""
    return ()


@contextmanager
def tx(path: Path | str, immediate: bool = False, *, pg_locks=()):
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_ledger_sync_identity_columns(conn: sqlite3.Connection) -> None:
    """给账本行增加可恢复的同步身份。只增加内部字段，不改变金额/余额/既有 ID。"""
    for table in ("ledger", "item_ledger"):
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "sync_event_uuid" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN sync_event_uuid TEXT NOT NULL DEFAULT ''")
        if "sync_origin" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN sync_origin TEXT NOT NULL DEFAULT ''")
        if "sync_origin_row_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN sync_origin_row_id INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_sync_event_uuid "
            f"ON {table}(owner_id,sync_event_uuid) WHERE sync_event_uuid<>''"
        )


def ensure_ledger_business_columns(conn: sqlite3.Connection) -> None:
    """r33：为普通流水增加可选经营分类与成本。旧流水保持未分类，避免伪造历史利润。"""
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(ledger)").fetchall()}
    if "category" not in cols:
        conn.execute("ALTER TABLE ledger ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    if "cost_micro" not in cols:
        conn.execute("ALTER TABLE ledger ADD COLUMN cost_micro INTEGER NOT NULL DEFAULT 0")
    if "reversal_of" not in cols:
        conn.execute("ALTER TABLE ledger ADD COLUMN reversal_of INTEGER NOT NULL DEFAULT 0")
    if "reversed_by" not in cols:
        conn.execute("ALTER TABLE ledger ADD COLUMN reversed_by INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_owner_category_time "
        "ON ledger(owner_id,category,time)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_one_reversal "
        "ON ledger(owner_id,reversal_of) WHERE reversal_of>0"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_reversed_by "
        "ON ledger(owner_id,reversed_by) WHERE reversed_by>0"
    )


def init_ledger_db(path: Path | str = LEDGER_DB_PATH) -> None:
    key = _schema_key("ledger", path)
    with _DB_LOCK:
        if key in _SCHEMA_READY:
            return
        # A test/deployment may replace the database file at the same path.
        # Force one WAL verification whenever schema initialization genuinely reruns.
        _WAL_READY.pop(str(Path(path).resolve()), None)
        with tx(path, immediate=True) as conn:
            conn.executescript("""
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            peer_id INTEGER,
            user_name TEXT,
            action TEXT,
            amount_micro INTEGER,
            balance_micro INTEGER,
            remark TEXT,
            time TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_owner_peer_id
            ON ledger(owner_id, peer_id, id);
        CREATE INDEX IF NOT EXISTS idx_ledger_owner_time
            ON ledger(owner_id, time);

        CREATE TABLE IF NOT EXISTS item_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL DEFAULT 0,
            item_name TEXT NOT NULL,
            user_name TEXT DEFAULT '',
            action TEXT DEFAULT '',
            amount_micro INTEGER DEFAULT 0,
            balance_micro INTEGER DEFAULT 0,
            remark TEXT DEFAULT '',
            time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_item_owner_peer_item_id
            ON item_ledger(owner_id, peer_id, item_name, id);
        CREATE INDEX IF NOT EXISTS idx_item_owner_id
            ON item_ledger(owner_id, id);

        CREATE TABLE IF NOT EXISTS settlement_idempotency_v1(
            owner_id INTEGER NOT NULL,
            idem_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(owner_id, idem_key)
        );

        CREATE TABLE IF NOT EXISTS miniapp_idempotency(
            owner_id INTEGER NOT NULL,
            idem_key TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            request_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(owner_id, idem_key)
        );
        CREATE INDEX IF NOT EXISTS idx_miniapp_idempotency_created
            ON miniapp_idempotency(owner_id, created_at);

        CREATE TABLE IF NOT EXISTS ledger_projection_outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            applied_at INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            UNIQUE(owner_id,event_key)
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_projection_outbox_pending
            ON ledger_projection_outbox(applied_at,owner_id,id);
        """)
            # Older deployments may already have the MiniApp table without request_hash.
            mini_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(miniapp_idempotency)").fetchall()}
            if "request_hash" not in mini_cols:
                conn.execute("ALTER TABLE miniapp_idempotency ADD COLUMN request_hash TEXT NOT NULL DEFAULT ''")
            ensure_ledger_sync_identity_columns(conn)
            ensure_ledger_business_columns(conn)
        _bump_db_generation(path)
        _SCHEMA_READY.add(key)


def init_app_db(path: Path | str = APP_DB_PATH) -> None:
    key = _schema_key("app", path)
    with _DB_LOCK:
        if key in _SCHEMA_READY:
            return
        # A test/deployment may replace the database file at the same path.
        # Force one WAL verification whenever schema initialization genuinely reruns.
        _WAL_READY.pop(str(Path(path).resolve()), None)
        now = int(time.time())
        with tx(path, immediate=True) as conn:
            conn.executescript("""
        CREATE TABLE IF NOT EXISTS owner_settings (
            owner_id INTEGER PRIMARY KEY,
            ledger_currency TEXT NOT NULL DEFAULT 'USDT',
            item_ledger_enabled INTEGER NOT NULL DEFAULT 0,
            sync_enabled INTEGER NOT NULL DEFAULT 0,
            anti_revoke_enabled INTEGER NOT NULL DEFAULT 1,
            anti_edit_enabled INTEGER NOT NULL DEFAULT 1,
            scam_detect_enabled INTEGER NOT NULL DEFAULT 1,
            fakebot_detect_enabled INTEGER NOT NULL DEFAULT 1,
            keyword_enabled INTEGER NOT NULL DEFAULT 1,
            updated_at INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS business_connections (
            connection_id TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            is_enabled INTEGER NOT NULL DEFAULT 1,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_business_owner ON business_connections(owner_id, updated_at);

        CREATE TABLE IF NOT EXISTS keyword_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(owner_id, keyword)
        );
        CREATE INDEX IF NOT EXISTS idx_keyword_owner ON keyword_replies(owner_id, id);

        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS risk_alert_state (
            owner_id INTEGER NOT NULL,
            peer_id INTEGER NOT NULL,
            risk_key TEXT NOT NULL,
            alerted_at INTEGER NOT NULL,
            PRIMARY KEY(owner_id, peer_id, risk_key)
        );
        """)
            conn.execute(
                "INSERT OR IGNORE INTO runtime_state(key,value,updated_at) VALUES('schema_version','1',?)",
                (now,),
            )
        _bump_db_generation(path)
        _SCHEMA_READY.add(key)



def init_sync_db(path: Path | str = SYNC_DB_PATH) -> None:
    key = _schema_key("sync", path)
    with _DB_LOCK:
        if key in _SCHEMA_READY:
            return
        # A test/deployment may replace the database file at the same path.
        # Force one WAL verification whenever schema initialization genuinely reruns.
        _WAL_READY.pop(str(Path(path).resolve()), None)
        with tx(path, immediate=True) as conn:
            conn.executescript("""
        CREATE TABLE IF NOT EXISTS sync_events (
            event_uuid TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            table_name TEXT NOT NULL,
            origin TEXT NOT NULL,
            origin_row_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            main_row_id INTEGER,
            water_row_id INTEGER,
            tombstoned INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(owner_id, table_name, origin, origin_row_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sync_owner_table
            ON sync_events(owner_id, table_name, tombstoned, created_at);
        CREATE INDEX IF NOT EXISTS idx_sync_active_order
            ON sync_events(owner_id, table_name, created_at, origin_row_id, event_uuid)
            WHERE tombstoned=0;
        CREATE INDEX IF NOT EXISTS idx_sync_main_row
            ON sync_events(owner_id, table_name, main_row_id);
        CREATE INDEX IF NOT EXISTS idx_sync_water_row
            ON sync_events(owner_id, table_name, water_row_id);

        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            finished_at INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            main_before INTEGER NOT NULL DEFAULT 0,
            water_before INTEGER NOT NULL DEFAULT 0,
            merged_count INTEGER NOT NULL DEFAULT 0,
            detail TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sync_runs_owner ON sync_runs(owner_id, id DESC);
            """)
        _bump_db_generation(path)
        _SCHEMA_READY.add(key)


def init_all() -> None:
    init_ledger_db()
    init_app_db()
    init_sync_db()


def get_settings(owner_id: int) -> dict:
    init_app_db()
    oid = int(owner_id)
    if oid <= 0:
        return {}
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute("SELECT * FROM owner_settings WHERE owner_id=?", (oid,)).fetchone()
    finally:
        conn.close()
    if row:
        return dict(row)
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO owner_settings(
                owner_id, ledger_currency, item_ledger_enabled, sync_enabled,
                anti_revoke_enabled, anti_edit_enabled, scam_detect_enabled,
                fakebot_detect_enabled, keyword_enabled, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (oid, DEFAULT_CURRENCY, 0, 0, 1, 1, 1, 1, 1, int(time.time())),
        )
        row = conn.execute("SELECT * FROM owner_settings WHERE owner_id=?", (oid,)).fetchone()
    return dict(row) if row else {}

def set_setting(owner_id: int, key: str, value) -> None:
    allowed = {
        "ledger_currency", "item_ledger_enabled", "sync_enabled", "anti_revoke_enabled",
        "anti_edit_enabled", "scam_detect_enabled", "fakebot_detect_enabled", "keyword_enabled",
    }
    if key not in allowed:
        raise ValueError("unsupported setting")
    get_settings(owner_id)
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(f"UPDATE owner_settings SET {key}=?, updated_at=? WHERE owner_id=?", (value, int(time.time()), int(owner_id)))


def set_protection_enabled(owner_id: int, enabled: bool) -> None:
    """防撤回/编辑在 UI 中是一个功能，两个兼容字段必须原子同步。"""
    oid = int(owner_id)
    get_settings(oid)
    value = 1 if bool(enabled) else 0
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "UPDATE owner_settings SET anti_revoke_enabled=?, anti_edit_enabled=?, updated_at=? WHERE owner_id=?",
            (value, value, int(time.time()), oid),
        )


def upsert_business_connection(connection_id: str, owner: dict, enabled: bool = True) -> None:
    cid = str(connection_id or "").strip()
    if not cid:
        return
    owner = owner or {}
    owner_id = int(owner.get("id") or 0)
    if owner_id <= 0:
        return
    with tx(APP_DB_PATH, immediate=True) as conn:
        old = conn.execute("SELECT owner_id FROM business_connections WHERE connection_id=?", (cid,)).fetchone()
        if old and int(old[0] or 0) not in (0, owner_id):
            raise PermissionError("business connection owner mismatch")
        conn.execute("""
        INSERT INTO business_connections(connection_id, owner_id, username, first_name, is_enabled, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(connection_id) DO UPDATE SET
            username=excluded.username, first_name=excluded.first_name,
            is_enabled=excluded.is_enabled, updated_at=excluded.updated_at
        """, (cid, owner_id, str(owner.get("username") or ""), str(owner.get("first_name") or ""), 1 if enabled else 0, int(time.time())))

def business_owner_id(connection_id: str) -> int:
    cid = str(connection_id or "").strip()
    if not cid:
        return 0
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute("SELECT owner_id FROM business_connections WHERE connection_id=? AND is_enabled=1", (cid,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
