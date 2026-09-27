# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from membership import MembershipService


_MEMBER_STATUSES = {"creator", "administrator", "member"}


class AccessController:
    def __init__(
        self,
        membership: MembershipService,
        *,
        required_channel: str,
        required_channel_url: str,
        bot_username: str,
        admin_ids=(),
        fallback_bot: str = "@jizhangaide_bot",
        price_7d: str = "3",
        price_30d: str = "6.66",
        now_fn: Callable[[], float] = time.time,
        cleanup_fn: Callable[[int], int] | None = None,
        business_count_fn: Callable[[], int] | None = None,
        business_connected_fn: Callable[[int], bool] | None = None,
        channel_check_api=None,
    ):
        self.membership = membership
        self.required_channel = str(required_channel or "@jizhangaide")
        self.required_channel_url = str(required_channel_url or "https://t.me/jizhangaide")
        self.bot_username = str(bot_username or "ShuiBei_bot").lstrip("@")
        self.admin_ids = {int(x) for x in (admin_ids or ()) if int(x) > 0}
        self.fallback_bot = str(fallback_bot or "@jizhangaide_bot")
        self.price_7d = str(price_7d or "3")
        self.price_30d = str(price_30d or "6.66")
        self.now_fn = now_fn
        self.cleanup_fn = cleanup_fn
        self.business_count_fn = business_count_fn
        self.business_connected_fn = business_connected_fn
        self.channel_check_api = channel_check_api
        self._last_maintenance = 0

    def _now(self) -> int:
        return int(self.now_fn())

    @staticmethod
    def _is_member(chat_member: dict) -> bool:
        status = str((chat_member or {}).get("status") or "").lower()
        if status in _MEMBER_STATUSES:
            return True
        if status == "restricted":
            return bool((chat_member or {}).get("is_member"))
        return False

    @staticmethod
    def _premium(user: dict) -> bool:
        return bool((user or {}).get("is_premium"))

    @staticmethod
    def _noop(update: dict) -> dict:
        return {"update_id": int((update or {}).get("update_id") or 0)}

    def _join_markup(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": "加入频道", "url": self.required_channel_url}],
                [{"text": "我已加入，验证", "callback_data": "membership:verify_channel"}],
            ]
        }

    def _send_join_prompt(self, api, user_id: int) -> None:
        try:
            api.send_message(
                int(user_id),
                f"使用水杯记账前需要先加入 {html.escape(self.required_channel)}。\n\n"
                "加入后点击“我已加入，验证”即可继续。",
                reply_markup=self._join_markup(),
            )
        except Exception:
            pass

    def _live_channel_check(self, api, user_id: int, premium_hint: bool = False) -> tuple[bool, bool]:
        try:
            checker = self.channel_check_api or api
            member = checker.get_chat_member(self.required_channel, int(user_id)) or {}
        except Exception:
            self.membership.record_error("channel_check")
            return False, bool(premium_hint)
        joined = self._is_member(member)
        premium = bool(((member.get("user") or {}).get("is_premium"))) or bool(premium_hint)
        try:
            self.membership.mark_channel_membership(int(user_id), joined, premium)
            if joined:
                self.membership.activate_user(int(user_id))
        except Exception:
            self.membership.record_error("channel_state")
            return False, premium
        return joined, premium

    def _ensure_channel(self, api, user: dict, *, force: bool = False) -> tuple[bool, bool]:
        user = user or {}
        user_id = int(user.get("id") or 0)
        if user_id <= 0:
            return False, False
        premium_hint = self._premium(user)
        if not force:
            try:
                cached = self.membership.has_channel_access(user_id, max_age=1800)
            except Exception:
                cached = None
            if cached is True:
                self.membership.activate_user(user_id)
                if "is_premium" in user:
                    self.membership.touch_user(user_id, is_premium=premium_hint)
                else:
                    self.membership.touch_user(user_id)
                stored_premium = bool(int(self.membership.get_user(user_id).get("telegram_premium") or 0))
                return True, (premium_hint if "is_premium" in user else stored_premium)
            if cached is False:
                return False, premium_hint
        return self._live_channel_check(api, user_id, premium_hint)

    def _complete_referral(self, api, user_id: int, premium: bool) -> None:
        business_connected = False
        if self.business_connected_fn:
            try:
                business_connected = bool(self.business_connected_fn(int(user_id)))
            except Exception:
                self.membership.record_error("business_state")
        try:
            result = self.membership.complete_referral(
                int(user_id), is_premium=bool(premium), business_connected=business_connected
            )
        except Exception:
            self.membership.record_error("referral_complete")
            return
        if result.get("status") != "completed" or result.get("duplicate"):
            return
        inviter_id = int(result.get("inviter_id") or 0)
        inviter_days = int(result.get("inviter_days") or 0)
        invitee_days = int(result.get("invitee_days") or 0)
        if inviter_id > 0 and inviter_days > 0:
            try:
                api.send_message(
                    inviter_id,
                    "🎉 <b>邀请成功</b>\n\n"
                    f"被邀请用户已在有效期内加入 {html.escape(self.required_channel)}。\n"
                    f"你已获得 <b>{inviter_days} 天</b>会员时长。",
                )
            except Exception:
                pass
        if invitee_days > 0:
            try:
                api.send_message(
                    int(user_id),
                    f"🎁 通过邀请链接完成加入，你额外获得 <b>{invitee_days} 天</b>会员时长。",
                )
            except Exception:
                pass

    def _membership_text(self, user_id: int) -> str:
        self.membership.activate_user(int(user_id))
        state = self.membership.get_user(int(user_id))
        expires_at = int(state.get("expires_at") or 0)
        active = expires_at > self._now()
        expiry_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at)) if expires_at else "-"
        token = self.membership.get_referral_token(int(user_id))
        link = f"https://t.me/{self.bot_username}?start=ref_{token}"
        count = self.membership.valid_referral_count(int(user_id))
        status = "有效" if active else "已到期"
        return (
            "👑 <b>水杯会员 / 增值服务</b>\n\n"
            f"状态：<b>{status}</b>\n"
            f"到期时间：<code>{html.escape(expiry_text)}</code>\n"
            f"有效邀请：<b>{count}</b> 人\n\n"
            f"7 天：<b>{html.escape(self.price_7d)} USDT</b>\n"
            f"30 天：<b>{html.escape(self.price_30d)} USDT</b>\n\n"
            "新用户首次激活免费试用 30 天。文本防撤回 / 防编辑及其它增值服务为会员专属；"
            "记账、反诈与基础工具永久免费。\n\n"
            "邀请奖励（无上限）：\n"
            "• 普通 Telegram 用户：邀请人 +7 天\n"
            "• Telegram Premium：邀请人 +10 天\n"
            "• 被邀请用户设置水杯为 Business Bot：普通总计 10 天 / Premium 总计 14 天\n"
            "• 被邀请用户通过邀请链接成功加入频道：本人额外 +7 天\n"
            "• 被邀请用户必须在绑定邀请后的 3 天内加入频道，超时永久作废\n\n"
            f"你的邀请链接：\n<code>{html.escape(link)}</code>"
        )

    def _membership_markup(self) -> dict:
        return {
            "inline_keyboard": [
                [
                    {"text": f"7天 {self.price_7d}U", "url": f"https://t.me/{self.fallback_bot.lstrip('@')}?start=shuibei_vip_7"},
                    {"text": f"30天 {self.price_30d}U", "url": f"https://t.me/{self.fallback_bot.lstrip('@')}?start=shuibei_vip_30"},
                ],
                [{"text": "我的邀请链接", "callback_data": "membership:invite"}],
                [{"text": "加入要求频道", "url": self.required_channel_url}],
            ]
        }

    def _send_membership(self, api, user_id: int) -> None:
        try:
            api.send_message(int(user_id), self._membership_text(int(user_id)), reply_markup=self._membership_markup())
        except Exception:
            self.membership.record_error("membership_ui")

    def _send_invite(self, api, user_id: int) -> None:
        try:
            token = self.membership.get_referral_token(int(user_id))
            link = f"https://t.me/{self.bot_username}?start=ref_{token}"
            api.send_message(
                int(user_id),
                "🔗 <b>你的专属邀请链接</b>\n\n"
                f"<code>{html.escape(link)}</code>\n\n"
                "对方必须在首次绑定邀请后的 3 天内加入要求频道。邀请奖励无上限。",
            )
        except Exception:
            self.membership.record_error("invite_ui")

    def _admin_text(self) -> str:
        stats = self.membership.admin_stats()
        business_count = 0
        if self.business_count_fn:
            try:
                business_count = int(self.business_count_fn() or 0)
            except Exception:
                business_count = 0
        errors = stats.get("errors") or []
        error_lines = [f"• {html.escape(str(x.get('category') or 'unknown'))}: {int(x.get('count') or 0)}" for x in errors[:10]]
        return "\n".join([
            "🛠️ <b>水杯 /yh</b>",
            "",
            f"激活用户：<b>{int(stats.get('users_total') or 0)}</b>",
            f"7日活跃：<b>{int(stats.get('users_recent') or 0)}</b>",
            f"有效会员：<b>{int(stats.get('members_active') or 0)}</b>",
            f"到期会员：<b>{int(stats.get('members_expired') or 0)}</b>",
            f"邀请待完成：<b>{int(stats.get('referrals_pending') or 0)}</b>",
            f"邀请有效：<b>{int(stats.get('referrals_valid') or 0)}</b>",
            f"邀请过期：<b>{int(stats.get('referrals_expired') or 0)}</b>",
            f"Business 连接：<b>{business_count}</b>",
            "",
            "<b>最近错误计数</b>",
            *(error_lines or ["• 无"]),
        ])

    def _handle_start(self, api, message: dict) -> bool:
        user = (message or {}).get("from") or {}
        user_id = int(user.get("id") or 0)
        text = str((message or {}).get("text") or "").strip()
        payload = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if payload.startswith("ref_") and user_id > 0:
            token = payload[4:].strip()
            try:
                inviter = self.membership.resolve_referral_token(token)
                if inviter > 0:
                    self.membership.bind_referral(user_id, inviter, token)
            except Exception:
                self.membership.record_error("referral_bind")
        state_before = self.membership.get_user(user_id)
        trial_before = int(state_before.get("trial_granted_at") or 0)
        joined, premium = self._ensure_channel(api, user, force=True)
        if not joined:
            self._send_join_prompt(api, user_id)
            return False
        state = self.membership.activate_user(user_id)
        if trial_before == 0 and int(state.get("trial_granted_at") or 0) > 0:
            try:
                api.send_message(user_id, "🎁 新用户 30 天免费会员试用已激活。")
            except Exception:
                pass
        self._complete_referral(api, user_id, premium)
        return True

    def _handle_private_message(self, api, message: dict) -> bool:
        chat = (message or {}).get("chat") or {}
        if str(chat.get("type") or "") != "private":
            return True
        user = (message or {}).get("from") or {}
        user_id = int(user.get("id") or 0)
        text = str((message or {}).get("text") or "").strip()
        lower = text.lower()
        if lower.startswith("/start"):
            return self._handle_start(api, message)
        if lower == "/yh":
            if user_id in self.admin_ids:
                try:
                    api.send_message(user_id, self._admin_text())
                except Exception:
                    pass
            else:
                try:
                    api.send_message(user_id, "无权限。")
                except Exception:
                    pass
            return False
        if lower in {"/vip", "/member", "/membership", "/invite"}:
            joined, _ = self._ensure_channel(api, user, force=True)
            if not joined:
                self._send_join_prompt(api, user_id)
                return False
            if lower == "/invite":
                self._send_invite(api, user_id)
            else:
                self._send_membership(api, user_id)
            return False
        joined, _ = self._ensure_channel(api, user)
        if not joined:
            self._send_join_prompt(api, user_id)
            return False
        premium_command = (
            lower in {"/quick", "/offline", "/sync", "/kwlist", "/archive"}
            or lower.startswith(("/kwadd ", "/kwdel "))
        )
        if premium_command and not self.membership.is_active(user_id):
            self._send_membership(api, user_id)
            return False
        return True

    def _handle_callback(self, api, cq: dict) -> bool:
        user = (cq or {}).get("from") or {}
        user_id = int(user.get("id") or 0)
        data = str((cq or {}).get("data") or "")
        cq_id = str((cq or {}).get("id") or "")
        if data == "membership:verify_channel":
            joined, premium = self._ensure_channel(api, user, force=True)
            try:
                api.answer_callback(cq_id, "验证成功" if joined else "暂未检测到已加入", alert=not joined)
            except Exception:
                pass
            if joined:
                self.membership.activate_user(user_id)
                self._complete_referral(api, user_id, premium)
                self._send_membership(api, user_id)
            else:
                self._send_join_prompt(api, user_id)
            return False
        if data == "membership:invite":
            joined, _ = self._ensure_channel(api, user, force=True)
            try:
                api.answer_callback(cq_id, "邀请链接已发送" if joined else "请先加入频道")
            except Exception:
                pass
            if joined:
                self._send_invite(api, user_id)
            else:
                self._send_join_prompt(api, user_id)
            return False
        if data == "membership:open":
            joined, _ = self._ensure_channel(api, user, force=True)
            if joined:
                self._send_membership(api, user_id)
            else:
                self._send_join_prompt(api, user_id)
            return False
        joined, _ = self._ensure_channel(api, user)
        if not joined:
            try:
                api.answer_callback(cq_id, "请先加入要求频道", alert=True)
            except Exception:
                pass
            self._send_join_prompt(api, user_id)
            return False
        premium_callback = (
            data in {"menu:quick", "menu:offline", "menu:sync", "menu:kw", "menu:archive", "protection:toggle"}
            or data.startswith(("quick:", "offline:", "sync:", "kw:", "setv2:toggle:protection:"))
        )
        if premium_callback and not self.membership.is_active(user_id):
            try:
                api.answer_callback(cq_id, "该增值服务为会员功能", alert=True)
            except Exception:
                pass
            self._send_membership(api, user_id)
            return False
        return True

    def _handle_chat_member(self, api, update: dict) -> None:
        cmu = (update or {}).get("chat_member") or {}
        chat = cmu.get("chat") or {}
        username = str(chat.get("username") or "").lstrip("@").lower()
        required = self.required_channel.lstrip("@").lower()
        if username and username != required:
            return
        new_member = cmu.get("new_chat_member") or {}
        user = new_member.get("user") or {}
        user_id = int(user.get("id") or 0)
        if user_id <= 0:
            return
        joined = self._is_member(new_member)
        premium = self._premium(user)
        try:
            self.membership.mark_channel_membership(user_id, joined, premium)
            if joined:
                self.membership.activate_user(user_id)
                self._complete_referral(api, user_id, premium)
        except Exception:
            self.membership.record_error("chat_member_event")

    def _handle_business_connection(self, api, update: dict) -> None:
        bc = (update or {}).get("business_connection") or {}
        user = bc.get("user") or {}
        user_id = int(user.get("id") or 0)
        if user_id <= 0:
            return
        try:
            self.membership.touch_user(user_id, is_premium=self._premium(user))
        except Exception:
            pass
        try:
            from db import upsert_business_connection
            upsert_business_connection(str(bc.get("id") or ""), user, bool(bc.get("is_enabled", True)))
        except Exception:
            pass
        if not bool(bc.get("is_enabled", True)):
            return
        try:
            topup = self.membership.apply_business_topup(user_id)
        except Exception:
            self.membership.record_error("business_topup")
            return
        days = int(topup.get("topup_days") or 0)
        inviter_id = int(topup.get("inviter_id") or 0)
        if days > 0 and inviter_id > 0:
            try:
                api.send_message(
                    inviter_id,
                    "💼 <b>Business Bot 邀请奖励补发</b>\n\n"
                    f"你之前邀请的用户现已将水杯设置为 Business Bot，补发 <b>{days} 天</b>会员时长。",
                )
            except Exception:
                pass

    def _business_owner(self, update: dict) -> int:
        message = (
            (update or {}).get("business_message")
            or (update or {}).get("edited_business_message")
            or (update or {}).get("deleted_business_messages")
            or {}
        )
        bc_id = str(message.get("business_connection_id") or "")
        if not bc_id:
            return 0
        try:
            from db import business_owner_id
            owner_id = int(business_owner_id(bc_id) or 0)
        except Exception:
            owner_id = 0
        if owner_id > 0:
            return owner_id
        try:
            from features import business_owner_any_id
            return int(business_owner_any_id(bc_id) or 0)
        except Exception:
            return 0

    def _gate_business_update(self, api, update: dict) -> bool:
        owner_id = self._business_owner(update)
        if owner_id <= 0:
            return True
        joined, _ = self._ensure_channel(api, {"id": owner_id})
        if not joined:
            return False
        allowed = bool(self.membership.is_active(owner_id))
        for key in ("business_message", "edited_business_message", "deleted_business_messages"):
            payload = update.get(key)
            if isinstance(payload, dict):
                payload["_shuibei_protection_allowed"] = allowed
        return True

    def _maintenance(self, api) -> None:
        now = self._now()
        if self._last_maintenance and now - self._last_maintenance < 600:
            return
        self._last_maintenance = now
        try:
            self.membership.expire_pending_referrals(limit=500)
        except Exception:
            self.membership.record_error("referral_cleanup")
        if self.cleanup_fn:
            try:
                self.cleanup_fn(500)
            except Exception:
                self.membership.record_error("message_cleanup")
        try:
            reminders = self.membership.due_expiry_reminders(limit=100)
        except Exception:
            self.membership.record_error("reminder_query")
            reminders = []
        for item in reminders:
            user_id = int(item.get("user_id") or 0)
            expires_at = int(item.get("expires_at") or 0)
            if user_id <= 0 or expires_at <= 0:
                continue
            expiry_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at))
            try:
                api.send_message(
                    user_id,
                    "⏰ <b>会员即将到期</b>\n\n"
                    f"当前会员将在 <code>{html.escape(expiry_text)}</code> 到期。\n"
                    f"如果到期后仍希望继续使用文本防撤回 / 防编辑等增值服务，可移步 {html.escape(self.fallback_bot)}。",
                )
            except Exception:
                continue
            try:
                self.membership.mark_expiry_reminded(user_id, expires_at)
            except Exception:
                pass

    def filter_updates(self, api, updates) -> list[dict]:
        self._maintenance(api)
        result = []
        for update in list(updates or []):
            keep = True
            try:
                if update.get("chat_member"):
                    self._handle_chat_member(api, update)
                    keep = False
                elif update.get("business_connection"):
                    self._handle_business_connection(api, update)
                    keep = True
                elif update.get("message"):
                    keep = self._handle_private_message(api, update.get("message") or {})
                elif update.get("callback_query"):
                    keep = self._handle_callback(api, update.get("callback_query") or {})
                elif any(update.get(k) for k in ("business_message", "edited_business_message", "deleted_business_messages")):
                    keep = self._gate_business_update(api, update)
            except Exception:
                self.membership.record_error("access_controller")
                keep = False
            result.append(update if keep else self._noop(update))
        return result


_DEFAULT = None
_DEFAULT_LOCK = threading.RLock()


def _business_count(db_path: Path) -> int:
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM business_connections WHERE is_enabled=1").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return 0


def _business_connected(db_path: Path, user_id: int) -> bool:
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            row = conn.execute(
                "SELECT 1 FROM business_connections WHERE owner_id=? AND is_enabled=1 LIMIT 1",
                (int(user_id),),
            ).fetchone()
            return bool(row)
        finally:
            conn.close()
    except Exception:
        return False


def _load_channel_verifier_api(path: Path):
    try:
        p = Path(path)
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return None
        token = p.read_text("utf-8").strip()
        if not token:
            return None
        from telegram_api import TelegramAPI
        return TelegramAPI(token)
    except Exception:
        return None


def get_default_controller() -> AccessController:
    global _DEFAULT
    if _DEFAULT is not None:
        return _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is not None:
            return _DEFAULT
        from config import (
            APP_DB_PATH,
            BOT_USERNAME,
            CHANNEL_VERIFIER_TOKEN_PATH,
            MEMBERSHIP_7D_USDT,
            MEMBERSHIP_30D_USDT,
            MEMBERSHIP_FALLBACK_BOT,
            REQUIRED_CHANNEL,
            REQUIRED_CHANNEL_URL,
            SHUIBEI_ADMIN_USER_IDS,
        )
        from archive import cleanup_expired
        service = MembershipService(APP_DB_PATH)
        _DEFAULT = AccessController(
            service,
            required_channel=REQUIRED_CHANNEL,
            required_channel_url=REQUIRED_CHANNEL_URL,
            bot_username=BOT_USERNAME,
            admin_ids=SHUIBEI_ADMIN_USER_IDS,
            fallback_bot=MEMBERSHIP_FALLBACK_BOT,
            price_7d=MEMBERSHIP_7D_USDT,
            price_30d=MEMBERSHIP_30D_USDT,
            cleanup_fn=cleanup_expired,
            business_count_fn=lambda: _business_count(APP_DB_PATH),
            business_connected_fn=lambda user_id: _business_connected(APP_DB_PATH, user_id),
            channel_check_api=_load_channel_verifier_api(CHANNEL_VERIFIER_TOKEN_PATH),
        )
        return _DEFAULT


def filter_telegram_updates(api, updates) -> list[dict]:
    return get_default_controller().filter_updates(api, updates)
