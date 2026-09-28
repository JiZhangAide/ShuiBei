# -*- coding: utf-8 -*-
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

DAY = 86400
TRIAL_DAYS = 30
REFERRAL_TTL_SECONDS = 3 * DAY
REMINDER_WINDOW_SECONDS = 2 * DAY


class MembershipService:
    """Single source of truth for ShuiBei membership, referrals and channel state.

    All time-extending operations are idempotent and transactional.  Message/ledger
    content never enters these tables.
    """

    def __init__(self, db_path: Path | str, now_fn: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.now_fn = now_fn
        self._init_schema()

    def _now(self) -> int:
        return int(self.now_fn())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
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

    def _init_schema(self) -> None:
        with self._tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS membership_users (
                    user_id INTEGER PRIMARY KEY,
                    activated_at INTEGER NOT NULL DEFAULT 0,
                    trial_granted_at INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER NOT NULL DEFAULT 0,
                    expiry_reminded_for INTEGER NOT NULL DEFAULT 0,
                    channel_member INTEGER NOT NULL DEFAULT 0,
                    telegram_premium INTEGER NOT NULL DEFAULT 0,
                    channel_checked_at INTEGER NOT NULL DEFAULT 0,
                    last_seen_at INTEGER NOT NULL DEFAULT 0,
                    referral_token TEXT NOT NULL DEFAULT '' UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_membership_expires
                    ON membership_users(expires_at, expiry_reminded_for);
                CREATE INDEX IF NOT EXISTS idx_membership_seen
                    ON membership_users(last_seen_at);

                CREATE TABLE IF NOT EXISTS membership_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    days INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    expires_before INTEGER NOT NULL,
                    expires_after INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_membership_grants_user
                    ON membership_grants(user_id, id);

                CREATE TABLE IF NOT EXISTS referrals (
                    invitee_id INTEGER PRIMARY KEY,
                    inviter_id INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    bound_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    completed_at INTEGER NOT NULL DEFAULT 0,
                    expired_at INTEGER NOT NULL DEFAULT 0,
                    premium_snapshot INTEGER NOT NULL DEFAULT 0,
                    business_at_completion INTEGER NOT NULL DEFAULT 0,
                    base_reward_days INTEGER NOT NULL DEFAULT 0,
                    business_topup_days INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_referrals_inviter
                    ON referrals(inviter_id, completed_at);
                CREATE INDEX IF NOT EXISTS idx_referrals_expiry
                    ON referrals(expires_at, completed_at, expired_at);

                CREATE TABLE IF NOT EXISTS shuibei_error_counters (
                    category TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0,
                    last_at INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    @staticmethod
    def _row_dict(row) -> dict:
        return dict(row) if row else {}

    def _ensure_user_conn(self, conn: sqlite3.Connection, user_id: int, now: int) -> sqlite3.Row:
        uid = int(user_id)
        if uid <= 0:
            raise ValueError("invalid user id")
        conn.execute(
            "INSERT OR IGNORE INTO membership_users(user_id,last_seen_at) VALUES(?,?)",
            (uid, now),
        )
        conn.execute(
            "UPDATE membership_users SET last_seen_at=? WHERE user_id=?",
            (now, uid),
        )
        return conn.execute("SELECT * FROM membership_users WHERE user_id=?", (uid,)).fetchone()

    def _new_token_conn(self, conn: sqlite3.Connection) -> str:
        for _ in range(16):
            token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
            if len(token) < 8:
                continue
            if not conn.execute("SELECT 1 FROM membership_users WHERE referral_token=?", (token,)).fetchone():
                return token
        raise RuntimeError("unable to allocate referral token")

    def _ensure_referral_token_conn(self, conn: sqlite3.Connection, user_id: int) -> str:
        row = conn.execute("SELECT referral_token FROM membership_users WHERE user_id=?", (int(user_id),)).fetchone()
        token = str(row[0] or "") if row else ""
        if token:
            return token
        token = self._new_token_conn(conn)
        conn.execute("UPDATE membership_users SET referral_token=? WHERE user_id=?", (token, int(user_id)))
        return token

    def _grant_days_conn(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        days: int,
        reason: str,
        idempotency_key: str,
        now: int,
    ) -> dict:
        uid = int(user_id)
        add_days = int(days)
        if add_days <= 0:
            raise ValueError("days must be positive")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency key required")

        self._ensure_user_conn(conn, uid, now)
        existing = conn.execute(
            "SELECT expires_after FROM membership_grants WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if existing:
            row = conn.execute("SELECT * FROM membership_users WHERE user_id=?", (uid,)).fetchone()
            return {**self._row_dict(row), "granted": False, "grant_days": 0}

        row = conn.execute("SELECT expires_at FROM membership_users WHERE user_id=?", (uid,)).fetchone()
        before = int(row[0] or 0)
        after = max(now, before) + add_days * DAY
        conn.execute(
            """
            INSERT INTO membership_grants(
                user_id,days,reason,idempotency_key,created_at,expires_before,expires_after
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (uid, add_days, str(reason or "")[:80], key, now, before, after),
        )
        conn.execute(
            "UPDATE membership_users SET expires_at=?, expiry_reminded_for=0, last_seen_at=? WHERE user_id=?",
            (after, now, uid),
        )
        row = conn.execute("SELECT * FROM membership_users WHERE user_id=?", (uid,)).fetchone()
        return {**self._row_dict(row), "granted": True, "grant_days": add_days}

    def activate_user(self, user_id: int) -> dict:
        now = self._now()
        with self._tx() as conn:
            row = self._ensure_user_conn(conn, user_id, now)
            uid = int(user_id)
            if int(row["activated_at"] or 0) == 0:
                conn.execute("UPDATE membership_users SET activated_at=? WHERE user_id=?", (now, uid))
            if int(row["trial_granted_at"] or 0) == 0:
                result = self._grant_days_conn(conn, uid, TRIAL_DAYS, "new_user_trial", f"trial:{uid}", now)
                conn.execute("UPDATE membership_users SET trial_granted_at=? WHERE user_id=?", (now, uid))
            else:
                result = self._row_dict(conn.execute("SELECT * FROM membership_users WHERE user_id=?", (uid,)).fetchone())
            self._ensure_referral_token_conn(conn, uid)
            current = self._row_dict(conn.execute("SELECT * FROM membership_users WHERE user_id=?", (uid,)).fetchone())
            if result.get("granted"):
                current["granted"] = True
                current["grant_days"] = TRIAL_DAYS
            return current

    def touch_user(self, user_id: int, *, is_premium: bool | None = None) -> dict:
        now = self._now()
        with self._tx() as conn:
            self._ensure_user_conn(conn, user_id, now)
            if is_premium is not None:
                conn.execute(
                    "UPDATE membership_users SET telegram_premium=?, last_seen_at=? WHERE user_id=?",
                    (1 if is_premium else 0, now, int(user_id)),
                )
            return self._row_dict(conn.execute("SELECT * FROM membership_users WHERE user_id=?", (int(user_id),)).fetchone())

    def get_user(self, user_id: int) -> dict:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM membership_users WHERE user_id=?", (int(user_id),)).fetchone()
            return self._row_dict(row)
        finally:
            conn.close()

    def get_referral_token(self, user_id: int) -> str:
        now = self._now()
        with self._tx() as conn:
            self._ensure_user_conn(conn, user_id, now)
            return self._ensure_referral_token_conn(conn, int(user_id))

    def resolve_referral_token(self, token: str) -> int:
        token = str(token or "").strip()
        if not token:
            return 0
        conn = self._connect()
        try:
            row = conn.execute("SELECT user_id FROM membership_users WHERE referral_token=?", (token,)).fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()

    def grant_days(self, user_id: int, days: int, reason: str, idempotency_key: str) -> dict:
        now = self._now()
        with self._tx() as conn:
            return self._grant_days_conn(conn, user_id, days, reason, idempotency_key, now)

    def is_active(self, user_id: int) -> bool:
        row = self.get_user(user_id)
        return int(row.get("expires_at") or 0) > self._now()

    def mark_channel_membership(self, user_id: int, is_member: bool, is_premium: bool = False) -> None:
        now = self._now()
        with self._tx() as conn:
            self._ensure_user_conn(conn, user_id, now)
            conn.execute(
                """
                UPDATE membership_users
                SET channel_member=?, telegram_premium=?, channel_checked_at=?, last_seen_at=?
                WHERE user_id=?
                """,
                (1 if is_member else 0, 1 if is_premium else 0, now, now, int(user_id)),
            )

    def has_channel_access(self, user_id: int, *, max_age: int = 3600) -> bool | None:
        row = self.get_user(user_id)
        if not row:
            return None
        checked = int(row.get("channel_checked_at") or 0)
        if checked <= 0 or self._now() - checked > int(max_age):
            return None
        return bool(int(row.get("channel_member") or 0))

    def bind_referral(self, invitee_id: int, inviter_id: int, token: str) -> dict:
        now = self._now()
        invitee = int(invitee_id)
        inviter = int(inviter_id)
        if invitee <= 0 or inviter <= 0:
            return {"status": "invalid"}
        if invitee == inviter:
            return {"status": "self_referral"}
        with self._tx() as conn:
            invitee_row = self._ensure_user_conn(conn, invitee, now)
            self._ensure_user_conn(conn, inviter, now)
            existing = conn.execute("SELECT * FROM referrals WHERE invitee_id=?", (invitee,)).fetchone()
            if existing:
                return self._referral_state(existing, now)
            # Referral rewards are acquisition rewards: an already-activated
            # ShuiBei user cannot become a brand-new invitee later.
            if int(invitee_row["activated_at"] or 0) > 0:
                return {"status": "existing_user"}
            conn.execute(
                """
                INSERT INTO referrals(invitee_id,inviter_id,token,bound_at,expires_at)
                VALUES(?,?,?,?,?)
                """,
                (invitee, inviter, str(token or "")[:64], now, now + REFERRAL_TTL_SECONDS),
            )
            row = conn.execute("SELECT * FROM referrals WHERE invitee_id=?", (invitee,)).fetchone()
            return self._referral_state(row, now)

    @staticmethod
    def _referral_state(row, now: int) -> dict:
        if not row:
            return {"status": "none"}
        data = dict(row)
        if int(data.get("completed_at") or 0) > 0:
            data["status"] = "completed"
        elif int(data.get("expired_at") or 0) > 0 or int(data.get("expires_at") or 0) < now:
            data["status"] = "expired"
        else:
            data["status"] = "pending"
        return data

    def referral_for_invitee(self, invitee_id: int) -> dict:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM referrals WHERE invitee_id=?", (int(invitee_id),)).fetchone()
            return self._referral_state(row, self._now())
        finally:
            conn.close()

    def complete_referral(self, invitee_id: int, is_premium: bool, business_connected: bool = False) -> dict:
        now = self._now()
        invitee = int(invitee_id)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM referrals WHERE invitee_id=?", (invitee,)).fetchone()
            if not row:
                return {"status": "none", "inviter_days": 0, "invitee_days": 0}
            data = dict(row)
            if int(data["completed_at"] or 0) > 0:
                return {
                    "status": "completed",
                    "inviter_id": int(data["inviter_id"]),
                    "inviter_days": int(data["base_reward_days"] or 0),
                    "invitee_days": 7,
                    "duplicate": True,
                }
            if int(data["expired_at"] or 0) > 0 or now > int(data["expires_at"] or 0):
                conn.execute(
                    "UPDATE referrals SET expired_at=CASE WHEN expired_at=0 THEN ? ELSE expired_at END WHERE invitee_id=?",
                    (now, invitee),
                )
                return {"status": "expired", "inviter_days": 0, "invitee_days": 0}

            user = self._ensure_user_conn(conn, invitee, now)
            if not bool(int(user["channel_member"] or 0)):
                return {"status": "pending", "inviter_days": 0, "invitee_days": 0}

            inviter = int(data["inviter_id"])
            premium = bool(is_premium)
            inviter_days = (14 if premium else 10) if business_connected else (10 if premium else 7)

            self._grant_days_conn(
                conn, invitee, 7, "referral_invitee_bonus", f"ref:{invitee}:invitee", now
            )
            self._grant_days_conn(
                conn, inviter, inviter_days, "referral_inviter_reward", f"ref:{invitee}:inviter-base", now
            )
            conn.execute(
                """
                UPDATE referrals
                SET completed_at=?, premium_snapshot=?, business_at_completion=?, base_reward_days=?
                WHERE invitee_id=?
                """,
                (now, 1 if premium else 0, 1 if business_connected else 0, inviter_days, invitee),
            )
            return {
                "status": "completed",
                "inviter_id": inviter,
                "inviter_days": inviter_days,
                "invitee_days": 7,
                "premium": premium,
                "business": bool(business_connected),
                "duplicate": False,
            }

    def apply_business_topup(self, invitee_id: int) -> dict:
        now = self._now()
        invitee = int(invitee_id)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM referrals WHERE invitee_id=?", (invitee,)).fetchone()
            if not row:
                return {"status": "none", "topup_days": 0}
            data = dict(row)
            if int(data["completed_at"] or 0) <= 0:
                return {"status": "pending", "topup_days": 0}
            if int(data["business_at_completion"] or 0) or int(data["business_topup_days"] or 0) > 0:
                return {
                    "status": "completed",
                    "inviter_id": int(data["inviter_id"]),
                    "topup_days": 0,
                }
            user_row = conn.execute("SELECT telegram_premium FROM membership_users WHERE user_id=?", (invitee,)).fetchone()
            premium_now = bool(int(user_row[0] or 0)) if user_row else False
            target = 14 if (premium_now or int(data["premium_snapshot"] or 0)) else 10
            before = int(data["base_reward_days"] or 0)
            topup = max(0, target - before)
            inviter = int(data["inviter_id"])
            if topup > 0:
                self._grant_days_conn(
                    conn, inviter, topup, "referral_business_topup", f"ref:{invitee}:business-topup", now
                )
            conn.execute(
                "UPDATE referrals SET business_topup_days=? WHERE invitee_id=?",
                (topup, invitee),
            )
            return {"status": "completed", "inviter_id": inviter, "topup_days": topup}

    def expire_pending_referrals(self, limit: int = 500) -> int:
        now = self._now()
        lim = max(1, min(5000, int(limit)))
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT invitee_id FROM referrals
                WHERE completed_at=0 AND expired_at=0 AND expires_at<?
                ORDER BY expires_at ASC LIMIT ?
                """,
                (now, lim),
            ).fetchall()
            if not rows:
                return 0
            ids = [int(r[0]) for r in rows]
            ph = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE referrals SET expired_at=? WHERE invitee_id IN ({ph})",
                [now] + ids,
            )
            return len(ids)

    def due_expiry_reminders(self, limit: int = 100) -> list[dict]:
        now = self._now()
        lim = max(1, min(1000, int(limit)))
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT user_id,expires_at FROM membership_users
                WHERE expires_at>? AND expires_at<=?
                  AND expiry_reminded_for<>expires_at
                ORDER BY expires_at ASC LIMIT ?
                """,
                (now, now + REMINDER_WINDOW_SECONDS, lim),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_expiry_reminded(self, user_id: int, expires_at: int) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE membership_users SET expiry_reminded_for=?
                WHERE user_id=? AND expires_at=? AND expiry_reminded_for<>expires_at
                """,
                (int(expires_at), int(user_id), int(expires_at)),
            )
            return int(cur.rowcount or 0) > 0

    def valid_referral_count(self, inviter_id: int) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE inviter_id=? AND completed_at>0",
                (int(inviter_id),),
            ).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    def record_error(self, category: str) -> None:
        cat = str(category or "unknown")[:64]
        now = self._now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO shuibei_error_counters(category,count,last_at) VALUES(?,1,?)
                ON CONFLICT(category) DO UPDATE SET count=shuibei_error_counters.count+1,last_at=excluded.last_at
                """,
                (cat, now),
            )

    def admin_stats(self, *, recent_seconds: int = 7 * DAY) -> dict:
        now = self._now()
        conn = self._connect()
        try:
            out = {}
            out["users_total"] = int(conn.execute("SELECT COUNT(*) FROM membership_users WHERE activated_at>0").fetchone()[0])
            out["users_recent"] = int(conn.execute(
                "SELECT COUNT(*) FROM membership_users WHERE last_seen_at>=?", (now - int(recent_seconds),)
            ).fetchone()[0])
            out["members_active"] = int(conn.execute(
                "SELECT COUNT(*) FROM membership_users WHERE expires_at>?", (now,)
            ).fetchone()[0])
            out["members_expired"] = int(conn.execute(
                "SELECT COUNT(*) FROM membership_users WHERE activated_at>0 AND expires_at<=?", (now,)
            ).fetchone()[0])
            out["referrals_pending"] = int(conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE completed_at=0 AND expired_at=0 AND expires_at>=?", (now,)
            ).fetchone()[0])
            out["referrals_valid"] = int(conn.execute("SELECT COUNT(*) FROM referrals WHERE completed_at>0").fetchone()[0])
            out["referrals_expired"] = int(conn.execute(
                "SELECT COUNT(*) FROM referrals WHERE expired_at>0 OR (completed_at=0 AND expires_at<?)", (now,)
            ).fetchone()[0])
            out["errors"] = [dict(r) for r in conn.execute(
                "SELECT category,count,last_at FROM shuibei_error_counters ORDER BY last_at DESC LIMIT 10"
            ).fetchall()]
            return out
        finally:
            conn.close()
