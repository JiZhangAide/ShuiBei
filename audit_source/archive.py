# -*- coding: utf-8 -*-
from __future__ import annotations

import threading

from config import APP_DB_PATH, ARCHIVE_DB_PATH, MESSAGE_PROTECTION_KEY_PATH, MESSAGE_PROTECTION_TTL_SECONDS
from db import business_owner_id, get_settings
from membership import MembershipService
from message_protection import MessageProtectionStore

_STORE = None
_MEMBERSHIP = None
_LOCK = threading.RLock()


def _store() -> MessageProtectionStore:
    global _STORE
    if _STORE is None:
        with _LOCK:
            if _STORE is None:
                _STORE = MessageProtectionStore(
                    ARCHIVE_DB_PATH,
                    MESSAGE_PROTECTION_KEY_PATH,
                    ttl_seconds=MESSAGE_PROTECTION_TTL_SECONDS,
                )
    return _STORE


def _membership() -> MembershipService:
    global _MEMBERSHIP
    if _MEMBERSHIP is None:
        with _LOCK:
            if _MEMBERSHIP is None:
                _MEMBERSHIP = MembershipService(APP_DB_PATH)
    return _MEMBERSHIP


def _owner_id(business_connection_id: str) -> int:
    bc_id = str(business_connection_id or "")
    owner_id = int(business_owner_id(bc_id) or 0)
    if owner_id > 0:
        return owner_id
    try:
        from features import business_owner_any_id
        return int(business_owner_any_id(bc_id) or 0)
    except Exception:
        return 0


def _protection_enabled(owner_id: int) -> bool:
    if int(owner_id) <= 0:
        return False
    try:
        settings = get_settings(int(owner_id))
        toggled = bool(
            int(settings.get("anti_revoke_enabled", 0) or 0)
            and int(settings.get("anti_edit_enabled", 0) or 0)
        )
        return toggled and _membership().is_active(int(owner_id))
    except Exception:
        return False


def cache_business_message(api, message: dict) -> dict | None:
    bc_id = str((message or {}).get("business_connection_id") or "")
    owner_id = _owner_id(bc_id)
    if not _protection_enabled(owner_id):
        return None
    try:
        cached = _store().cache_business_message(message or {}, owner_id)
    except Exception:
        return None
    if not cached:
        return None
    return {
        "owner_id": owner_id,
        "chat_id": int(((message or {}).get("chat") or {}).get("id") or 0),
        "message_id": int((message or {}).get("message_id") or 0),
        "text_only": True,
    }


def handle_edited_business_message(api, message: dict) -> None:
    bc_id = str((message or {}).get("business_connection_id") or "")
    owner_id = _owner_id(bc_id)
    enabled = _protection_enabled(owner_id)
    if not enabled:
        return
    try:
        _store().handle_edited_business_message(api, message or {}, owner_id, enabled=True)
    except Exception:
        return


def handle_deleted_business_messages(api, deleted: dict) -> None:
    bc_id = str((deleted or {}).get("business_connection_id") or "")
    owner_id = _owner_id(bc_id)
    enabled = _protection_enabled(owner_id)
    if not enabled:
        return
    try:
        _store().handle_deleted_business_messages(api, deleted or {}, owner_id, enabled=True)
    except Exception:
        return


def archive_status(owner_id: int) -> str:
    if _protection_enabled(int(owner_id)):
        return "文本防撤回 / 防编辑：已开启（会员功能，媒体/文件不归档）"
    return "文本防撤回 / 防编辑：未开启或会员已到期（媒体/文件不归档）"


def cleanup_expired(limit: int = 500) -> int:
    try:
        return int(_store().cleanup_expired(limit=limit) or 0)
    except Exception:
        return 0
