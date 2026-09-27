# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from config import BOT_TOKEN, HTTP_TIMEOUT, OFFSET_FILE
from emoji_knowledge import premiumize_html_text


class TelegramAPIError(RuntimeError):
    pass


def _downgrade_reply_markup(markup):
    """Telegram/Bot 权限不支持新按钮字段时，降级为普通 InlineKeyboard。"""
    if not isinstance(markup, dict):
        return markup
    try:
        clone = json.loads(json.dumps(markup, ensure_ascii=False))
    except Exception:
        return markup
    changed = False
    for row in clone.get("inline_keyboard") or []:
        for b in row or []:
            if not isinstance(b, dict):
                continue
            if "style" in b:
                b.pop("style", None); changed = True
            if "icon_custom_emoji_id" in b:
                b.pop("icon_custom_emoji_id", None); changed = True
    return clone if changed else markup


def _is_button_compat_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(k in text for k in (
        "button", "inline keyboard", "reply markup", "custom emoji",
        "icon_custom_emoji", "style", "can't use", "cannot use",
    ))


_TG_EMOJI_RE = re.compile(r'<tg-emoji\s+emoji-id=["\']\d+["\']>(.*?)</tg-emoji>', re.I | re.S)

def _downgrade_custom_emoji_html(text: str) -> str:
    """保留标签中的普通 fallback emoji，只移除 Telegram custom-emoji 包装。"""
    return _TG_EMOJI_RE.sub(lambda m: m.group(1), str(text or ""))

def _is_text_custom_emoji_compat_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(k in text for k in (
        "custom emoji", "custom_emoji", "tg-emoji",
        "unsupported start tag", "can't parse entities", "cannot parse entities",
        "can't use", "cannot use",
    ))


def _call_with_emoji_fallback(api, method: str, payload: dict):
    """Premium text/buttons first; Telegram rejects them -> retry once/twice with safe plain fallback.

    No POST is automatically replayed on transport errors: fallback is only attempted after an
    explicit Telegram API error that says the markup/custom-emoji representation is incompatible.
    """
    current = dict(payload)
    for _ in range(3):
        try:
            return api.call(method, current)
        except TelegramAPIError as exc:
            lower = str(exc or "").lower()
            if method == "editMessageText" and "message is not modified" in lower:
                return None
            changed = False
            raw_text = str(current.get("text") or "")
            if "<tg-emoji" in raw_text and _is_text_custom_emoji_compat_error(exc):
                plain = _downgrade_custom_emoji_html(raw_text)
                if plain != raw_text:
                    current["text"] = plain
                    changed = True
            markup = current.get("reply_markup")
            fallback_markup = _downgrade_reply_markup(markup)
            if markup and fallback_markup != markup and _is_button_compat_error(exc):
                current["reply_markup"] = fallback_markup
                changed = True
            if not changed:
                raise
    return api.call(method, current)


class TelegramAPI:
    def __init__(self, token: str = BOT_TOKEN):
        self.token = str(token or "").strip()
        if not self.token or self.token == "PUT_SHUIBEI_BOT_TOKEN_HERE":
            raise TelegramAPIError("ShuiBei Bot Token 配置无效。")
        self.base = f"https://api.telegram.org/bot{self.token}"
        self.session = requests.Session()

    def call(self, method: str, payload: dict | None = None, files: dict | None = None, timeout: int | tuple | None = None) -> Any:
        url = f"{self.base}/{method}"
        data = dict(payload or {})
        for key, value in list(data.items()):
            if isinstance(value, (dict, list, tuple)):
                data[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            elif isinstance(value, bool):
                data[key] = "true" if value else "false"
        try:
            r = self.session.post(url, data=data, files=files, timeout=timeout or (8, HTTP_TIMEOUT))
            obj = r.json()
        except Exception as exc:
            raise TelegramAPIError(f"Telegram 请求失败：{type(exc).__name__}") from exc
        if not isinstance(obj, dict) or not obj.get("ok"):
            desc = str((obj or {}).get("description") or f"HTTP {getattr(r, 'status_code', '?')}")
            retry_after = 0
            try:
                retry_after = int(((obj or {}).get("parameters") or {}).get("retry_after") or 0)
            except Exception:
                pass
            if retry_after > 0:
                time.sleep(min(retry_after, 30))
            raise TelegramAPIError(desc)
        return obj.get("result")

    def get_me(self):
        return self.call("getMe")

    def get_updates(self, offset: int, timeout: int = 30, allowed_updates=None, limit: int = 0):
        payload = {"offset": int(offset), "timeout": int(timeout)}
        if allowed_updates:
            payload["allowed_updates"] = list(allowed_updates)
        if int(limit or 0) > 0:
            payload["limit"] = max(1, min(100, int(limit)))
        return self.call("getUpdates", payload, timeout=(8, max(10, timeout + 8))) or []

    def get_webhook_info(self):
        return self.call("getWebhookInfo", {}) or {}

    def delete_webhook(self, *, drop_pending_updates: bool = False):
        return self.call("deleteWebhook", {"drop_pending_updates": bool(drop_pending_updates)})

    def send_message(self, chat_id: int, text: str, *, business_connection_id: str = "", reply_to_message_id: int = 0,
                     reply_markup: dict | None = None, parse_mode: str = "HTML", disable_web_page_preview: bool = True):
        payload = {
            "chat_id": int(chat_id), "text": premiumize_html_text(text) if parse_mode and str(parse_mode).upper() == "HTML" else str(text or ""),
            "disable_web_page_preview": bool(disable_web_page_preview),
        }
        if parse_mode:
            payload["parse_mode"] = str(parse_mode)
        if business_connection_id:
            payload["business_connection_id"] = str(business_connection_id)
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": int(reply_to_message_id)}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return _call_with_emoji_fallback(self, "sendMessage", payload)

    def send_message_plain(self, chat_id: int, text: str, *, reply_markup: dict | None = None):
        """Minimal transport fallback: no HTML, no Premium text conversion, no new button fields."""
        payload = {
            "chat_id": int(chat_id),
            "text": str(text or ""),
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = _downgrade_reply_markup(reply_markup)
        return self.call("sendMessage", payload)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
        payload = {"chat_id": int(chat_id), "message_id": int(message_id), "text": premiumize_html_text(text), "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return _call_with_emoji_fallback(self, "editMessageText", payload)

    def answer_callback(self, callback_query_id: str, text: str = "", alert: bool = False):
        try:
            return self.call("answerCallbackQuery", {"callback_query_id": str(callback_query_id), "text": str(text or "")[:200], "show_alert": bool(alert)})
        except TelegramAPIError as exc:
            if "query is too old" in str(exc).lower() or "query id is invalid" in str(exc).lower():
                return None
            raise

    def send_document_bytes(self, chat_id: int, filename: str, raw: bytes, caption: str = ""):
        files = {"document": (str(filename), raw, "text/plain")}
        payload = {"chat_id": int(chat_id)}
        if caption:
            payload["caption"] = str(caption)[:1024]
        return self.call("sendDocument", payload, files=files, timeout=(8, 60))

    def delete_business_messages(self, business_connection_id: str, message_ids) -> Any:
        ids = [int(x) for x in (message_ids or []) if int(x or 0) > 0]
        if not ids:
            return True
        return self.call("deleteBusinessMessages", {"business_connection_id": str(business_connection_id), "message_ids": ids})

    def copy_message(self, chat_id: int, from_chat_id: int, message_id: int):
        return self.call("copyMessage", {"chat_id": int(chat_id), "from_chat_id": int(from_chat_id), "message_id": int(message_id)})

    def get_chat(self, chat_id_or_username):
        return self.call("getChat", {"chat_id": chat_id_or_username})

    def get_chat_member(self, chat_id_or_username, user_id: int):
        return self.call("getChatMember", {"chat_id": chat_id_or_username, "user_id": int(user_id)})


def load_offset(bot_id: int = 0) -> int:
    """Load polling offset bound to the current Telegram bot id.

    r24 and older stored a bare integer. A bare offset cannot be trusted after a Bot Token
    change because Telegram update_id sequences are bot-specific. Legacy integers therefore
    migrate once to offset=0; Telegram only returns still-unconfirmed pending updates, not
    already-confirmed history.
    """
    p = Path(OFFSET_FILE)
    fd = None
    try:
        fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return 0
        raw = os.read(fd, 4096).decode("utf-8", errors="ignore").strip()
        if not raw:
            return 0
        try:
            obj = json.loads(raw)
        except Exception:
            # Legacy bare integer: never trust it across bot identities.
            try:
                int(raw)
            except Exception:
                pass
            return 0
        if not isinstance(obj, dict):
            return 0
        stored_bot = int(obj.get("bot_id") or 0)
        current_bot = int(bot_id or 0)
        if current_bot > 0 and stored_bot != current_bot:
            return 0
        return max(0, int(obj.get("offset") or 0))
    except FileNotFoundError:
        return 0
    except Exception:
        return 0
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass


def save_offset(offset: int, bot_id: int = 0) -> None:
    p = Path(OFFSET_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        os.fchmod(fd, 0o600)
        raw = json.dumps(
            {"version": 2, "bot_id": max(0, int(bot_id or 0)), "offset": max(0, int(offset))},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        os.write(fd, raw)
        os.fsync(fd)
        os.close(fd); fd = -1
        os.replace(tmp_name, p)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass
