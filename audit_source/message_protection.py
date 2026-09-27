# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DAY = 86400
DEFAULT_TTL_SECONDS = 7 * DAY
_KEY_BYTES = 32
_NONCE_BYTES = 12
_EXPECTED_COLUMNS = {
    "lookup_key", "nonce", "ciphertext", "created_at", "updated_at",
}


class MessageProtectionStore:
    """Short-lived encrypted text cache with minimal plaintext metadata.

    Telegram identifiers and sender identity are never stored as plaintext.  A
    deterministic HMAC lookup key locates a message; the encrypted payload holds
    only the text and sender information needed to render a recovery notice.
    """

    def __init__(
        self,
        db_path: Path | str,
        key_path: Path | str,
        *,
        now_fn: Callable[[], float] = time.time,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        self.db_path = Path(db_path)
        self.key_path = Path(key_path)
        self.now_fn = now_fn
        self.ttl_seconds = max(DAY, int(ttl_seconds))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self._master_key = self._load_or_create_master_key()
        self._init_schema()

    def _now(self) -> int:
        return int(self.now_fn())

    @staticmethod
    def _chmod_private(path: Path) -> None:
        try:
            st = path.stat()
            if hasattr(os, "geteuid") and int(st.st_uid) != int(os.geteuid()):
                return
            if st.st_mode & 0o077:
                os.chmod(path, 0o600)
        except Exception:
            pass

    def _harden_db_files(self) -> None:
        self._chmod_private(self.db_path)
        self._chmod_private(Path(str(self.db_path) + "-wal"))
        self._chmod_private(Path(str(self.db_path) + "-shm"))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self._harden_db_files()
        return conn

    @contextmanager
    def _tx(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            self._harden_db_files()

    def _read_key(self) -> bytes:
        st = os.lstat(self.key_path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise RuntimeError("invalid message protection key path")
        raw = self.key_path.read_bytes()
        if len(raw) != _KEY_BYTES:
            raise RuntimeError("invalid message protection key length")
        self._chmod_private(self.key_path)
        return raw

    def _load_or_create_master_key(self) -> bytes:
        try:
            return self._read_key()
        except FileNotFoundError:
            pass

        raw = os.urandom(_KEY_BYTES)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(self.key_path), flags, 0o600)
        except FileExistsError:
            return self._read_key()
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._chmod_private(self.key_path)
        return raw

    def _init_schema(self) -> None:
        conn = self._connect()
        migrated = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Never migrate legacy plaintext/archive rows into the privacy cache.
            for table in ("archive_edit_history", "archive_messages"):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    conn.execute(f"DROP TABLE {table}")
                    migrated = True

            existing = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_messages'"
            ).fetchone()
            if existing:
                cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(protected_messages)").fetchall()}
                if cols != _EXPECTED_COLUMNS:
                    # Previous versions exposed Telegram/sender metadata in columns.
                    # The cache is intentionally disposable; drop instead of migrating.
                    conn.execute("DROP TABLE protected_messages")
                    migrated = True

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS protected_messages (
                    lookup_key BLOB PRIMARY KEY,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_protected_expiry
                    ON protected_messages(updated_at);
                """
            )
            conn.commit()
            if migrated:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    conn.execute("VACUUM")
                except Exception:
                    pass
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
            self._harden_db_files()

    @staticmethod
    def _text_of(message: dict) -> str:
        return str((message or {}).get("text") or (message or {}).get("caption") or "")

    @staticmethod
    def _sender_payload(user: dict) -> dict:
        user = user or {}
        first = str(user.get("first_name") or "")
        last = str(user.get("last_name") or "")
        name = (first + ((" " + last) if last else "")).strip() or "未知"
        return {
            "id": int(user.get("id") or 0),
            "name": name,
            "username": str(user.get("username") or ""),
        }

    def _owner_key(self, owner_id: int) -> bytes:
        oid = int(owner_id)
        if oid <= 0:
            raise ValueError("invalid owner id")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"shuibei-message-protection-v2",
            info=f"owner:{oid}".encode("ascii"),
        ).derive(self._master_key)

    def _lookup_key(self, owner_id: int, bc_id: str, chat_id: int, message_id: int) -> bytes:
        material = f"{str(bc_id or '')}|{int(chat_id)}|{int(message_id)}".encode("utf-8")
        return hmac.new(self._owner_key(owner_id), material, hashlib.sha256).digest()

    @staticmethod
    def _aad(lookup_key: bytes) -> bytes:
        return b"shuibei-protected-message-v2|" + bytes(lookup_key)

    def _encrypt_payload(self, owner_id: int, lookup_key: bytes, payload: dict) -> tuple[bytes, bytes]:
        nonce = os.urandom(_NONCE_BYTES)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ciphertext = AESGCM(self._owner_key(owner_id)).encrypt(nonce, raw, self._aad(lookup_key))
        return nonce, ciphertext

    def _decrypt_row(self, owner_id: int, row) -> dict:
        lookup_key = bytes(row["lookup_key"])
        raw = AESGCM(self._owner_key(owner_id)).decrypt(
            bytes(row["nonce"]), bytes(row["ciphertext"]), self._aad(lookup_key)
        )
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("invalid protected payload")
        return obj

    def cache_business_message(self, message: dict, owner_id: int) -> bool:
        content = self._text_of(message)
        if not content:
            return False
        bc_id = str((message or {}).get("business_connection_id") or "")
        chat_id = int(((message or {}).get("chat") or {}).get("id") or 0)
        message_id = int((message or {}).get("message_id") or 0)
        if int(owner_id) <= 0 or not bc_id or chat_id == 0 or message_id <= 0:
            return False
        lookup_key = self._lookup_key(owner_id, bc_id, chat_id, message_id)
        payload = {"text": content, "sender": self._sender_payload((message or {}).get("from") or {})}
        nonce, ciphertext = self._encrypt_payload(owner_id, lookup_key, payload)
        now = self._now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO protected_messages(lookup_key,nonce,ciphertext,created_at,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(lookup_key) DO UPDATE SET
                    nonce=excluded.nonce,
                    ciphertext=excluded.ciphertext,
                    updated_at=excluded.updated_at
                """,
                (sqlite3.Binary(lookup_key), sqlite3.Binary(nonce), sqlite3.Binary(ciphertext), now, now),
            )
        return True

    def _fetch_row(self, owner_id: int, bc_id: str, chat_id: int, message_id: int):
        lookup_key = self._lookup_key(owner_id, bc_id, chat_id, message_id)
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM protected_messages WHERE lookup_key=?",
                (sqlite3.Binary(lookup_key),),
            ).fetchone()
        finally:
            conn.close()
            self._harden_db_files()

    def read_message(self, owner_id: int, bc_id: str, chat_id: int, message_id: int) -> str:
        row = self._fetch_row(owner_id, bc_id, chat_id, message_id)
        if not row:
            return ""
        try:
            return str(self._decrypt_row(owner_id, row).get("text") or "")
        except Exception:
            return ""

    @staticmethod
    def _chunks(text: str, size: int = 3200):
        value = str(text or "")
        step = max(500, int(size))
        for start in range(0, len(value), step):
            yield value[start:start + step]

    @staticmethod
    def _sender_show(payload: dict) -> str:
        sender = (payload or {}).get("sender") or {}
        username = str(sender.get("username") or "").strip().lstrip("@")
        if username:
            return "@" + html.escape(username)
        return html.escape(str(sender.get("name") or "未知"))

    def handle_edited_business_message(self, api, message: dict, owner_id: int, enabled: bool) -> None:
        if not enabled:
            return
        bc_id = str((message or {}).get("business_connection_id") or "")
        chat_id = int(((message or {}).get("chat") or {}).get("id") or 0)
        message_id = int((message or {}).get("message_id") or 0)
        new_text = self._text_of(message)
        if not new_text or not bc_id or chat_id == 0 or message_id <= 0:
            return
        row = self._fetch_row(owner_id, bc_id, chat_id, message_id)
        old_payload = {}
        if row:
            try:
                old_payload = self._decrypt_row(owner_id, row)
            except Exception:
                old_payload = {}
        old_text = str(old_payload.get("text") or "")
        if old_text and old_text != new_text:
            old_preview = old_text[:1200] + ("\n……" if len(old_text) > 1200 else "")
            new_preview = new_text[:1200] + ("\n……" if len(new_text) > 1200 else "")
            try:
                api.send_message(
                    int(owner_id),
                    "✏️ <b>检测到文本消息编辑</b>\n"
                    f"发送者：{self._sender_show(old_payload)}\n\n"
                    f"编辑前：\n<blockquote>{html.escape(old_preview)}</blockquote>\n\n"
                    f"编辑后：\n<blockquote>{html.escape(new_preview)}</blockquote>",
                )
            except Exception:
                pass
        self.cache_business_message(message, owner_id)

    def handle_deleted_business_messages(self, api, deleted: dict, owner_id: int, enabled: bool) -> None:
        if not enabled:
            return
        bc_id = str((deleted or {}).get("business_connection_id") or "")
        chat_id = int(((deleted or {}).get("chat") or {}).get("id") or 0)
        ids = []
        for value in (deleted or {}).get("message_ids") or []:
            try:
                mid = int(value)
            except Exception:
                continue
            if mid > 0:
                ids.append(mid)
        if not bc_id or chat_id == 0 or not ids:
            return

        keys = [self._lookup_key(owner_id, bc_id, chat_id, mid) for mid in ids]
        conn = self._connect()
        try:
            rows = []
            for start in range(0, len(keys), 200):
                chunk = keys[start:start + 200]
                ph = ",".join("?" for _ in chunk)
                rows.extend(conn.execute(
                    f"SELECT * FROM protected_messages WHERE lookup_key IN ({ph})",
                    [sqlite3.Binary(k) for k in chunk],
                ).fetchall())
        finally:
            conn.close()
            self._harden_db_files()

        found_keys = []
        for row in rows:
            found_keys.append(bytes(row["lookup_key"]))
            try:
                payload = self._decrypt_row(owner_id, row)
            except Exception:
                continue
            content = str(payload.get("text") or "")
            if not content:
                continue
            for index, part in enumerate(self._chunks(content)):
                prefix = (
                    "🗑️ <b>检测到文本消息删除</b>\n"
                    f"发送者：{self._sender_show(payload)}\n\n"
                    if index == 0 else ""
                )
                try:
                    api.send_message(int(owner_id), prefix + f"<blockquote>{html.escape(part)}</blockquote>")
                except Exception:
                    break

        if found_keys:
            with self._tx() as conn:
                for start in range(0, len(found_keys), 200):
                    chunk = found_keys[start:start + 200]
                    ph = ",".join("?" for _ in chunk)
                    conn.execute(
                        f"DELETE FROM protected_messages WHERE lookup_key IN ({ph})",
                        [sqlite3.Binary(k) for k in chunk],
                    )

    def cleanup_expired(self, limit: int = 500) -> int:
        cutoff = self._now() - self.ttl_seconds
        lim = max(1, min(5000, int(limit)))
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT lookup_key FROM protected_messages WHERE updated_at<? ORDER BY updated_at ASC LIMIT ?",
                (cutoff, lim),
            ).fetchall()
            if not rows:
                return 0
            keys = [bytes(r[0]) for r in rows]
            ph = ",".join("?" for _ in keys)
            conn.execute(
                f"DELETE FROM protected_messages WHERE lookup_key IN ({ph})",
                [sqlite3.Binary(k) for k in keys],
            )
            return len(keys)

    def count_rows(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM protected_messages").fetchone()[0])
        finally:
            conn.close()
            self._harden_db_files()
