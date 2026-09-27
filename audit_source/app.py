# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import os
import re
import json
import hashlib
import tempfile
import threading
import time
from pathlib import Path

from runtime_guard import require_moqing_runtime
from moqing_gateway import current_gateway

from archive import cache_business_message, handle_deleted_business_messages, handle_edited_business_message
from access_control import filter_telegram_updates
from config import (
    ALLOWED_CURRENCIES, APP_DB_PATH, BOT_DISPLAY_NAME, BOT_USERNAME, FAKEBOT_ALERT_COOLDOWN_SECONDS,
    POLL_RETRY_SECONDS, POLL_TIMEOUT, RISK_ALERT_COOLDOWN_SECONDS,
)
from crypto_tools import exchange_rate_text, is_ton_target, is_tron_address, query_ton, query_tron
from db import (
    business_owner_id, connect, get_settings, init_all, set_protection_enabled, set_setting, tx, upsert_business_connection,
)
from fakebot import find_suspect, find_suspect_state, format_manual
from ledger import currency as ledger_currency, export_text, handle_text as handle_ledger_text, set_currency, query_text
from scam import query_records
from sync import MainLedgerUnavailable, SyncDisabled, preview as sync_preview, set_sync_enabled, sync_enabled, sync_once
from telegram_api import TelegramAPI, TelegramAPIError, load_offset, save_offset
from emoji_knowledge import currency_icon as premium_currency_icon, icon as premium_icon, system_icon as premium_system_icon, notice_banner as premium_notice_banner, notice_field as premium_notice_field, notice_green as premium_notice_green, notice_yellow as premium_notice_yellow



_SHUIBEI_RUNTIME_BUILD = "r28-20260827"
ALLOWED_UPDATE_TYPES = (
    "message", "callback_query", "chat_member", "business_connection",
    "business_message", "edited_business_message", "deleted_business_messages",
)
_RUNTIME_ROOT = Path(__file__).resolve().parent
_RUNTIME_READY_FILE = _RUNTIME_ROOT / ".shuibei.ready.json"
_RUNTIME_STATUS_FILE = _RUNTIME_ROOT / ".shuibei.status.json"
_RUNTIME_STATE_LOCK = threading.RLock()


def _runtime_source_fingerprint() -> str:
    h = hashlib.sha256()
    sources = [(name, _RUNTIME_ROOT / name) for name in (
        "app.py", "telegram_api.py", "config.py", "db.py", "features.py",
        "customers.py", "ledger.py", "receivables.py", "membership.py",
        "access_control.py", "message_protection.py", "sync.py", "scam.py",
        "fakebot.py", "moqing_gateway.py", "runtime_guard.py", "miniapp_auth.py",
        "crypto_tools.py", "emoji_knowledge.py",
    )]
    for name, fp in sources:
        h.update(name.encode("utf-8") + b"\0")
        try:
            h.update(fp.read_bytes())
        except Exception:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()


_RUNTIME_LOADED_FINGERPRINT = _runtime_source_fingerprint()


def _runtime_json_write(path: Path, payload: dict) -> None:
    """Atomic 0600 runtime marker. Never follows a pre-existing symlink."""
    with _RUNTIME_STATE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            os.write(fd, raw)
            os.fsync(fd)
            os.close(fd); fd = -1
            # A symlink at the destination is replaced, not followed.
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        finally:
            if fd >= 0:
                try: os.close(fd)
                except Exception: pass
            try:
                if os.path.exists(tmp): os.unlink(tmp)
            except Exception:
                pass


def _runtime_status(stage: str, *, bot_id: int = 0, username: str = "", detail: str = "", ready: bool = False) -> None:
    payload = {
        "version": 1,
        "runtime_build": _SHUIBEI_RUNTIME_BUILD,
        "source_fingerprint": _RUNTIME_LOADED_FINGERPRINT,
        "pid": int(os.getpid()),
        "parent_pid": int(os.getppid()),
        "stage": str(stage or "unknown"),
        "ready": bool(ready),
        "bot_id": int(bot_id or 0),
        "username": str(username or ""),
        "detail": str(detail or "")[:500],
        "updated_at": int(time.time()),
    }
    try:
        _runtime_json_write(_RUNTIME_STATUS_FILE, payload)
        if ready:
            _runtime_json_write(_RUNTIME_READY_FILE, payload)
    except Exception as exc:
        print(f"[ShuiBei] runtime marker warning: {type(exc).__name__}", flush=True)


def _runtime_clear_ready() -> None:
    try:
        _RUNTIME_READY_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _is_start_update(upd: dict) -> bool:
    try:
        m = upd.get("message") or {}
        if str((m.get("chat") or {}).get("type") or "") != "private":
            return False
        return str(m.get("text") or "").strip().lower().startswith("/start")
    except Exception:
        return False


def _is_poll_conflict(exc: Exception) -> bool:
    """Recognize Telegram's single-getUpdates-consumer conflict without depending on locale text."""
    text = str(exc or "").lower()
    return (
        "conflict" in text
        and any(k in text for k in ("getupdates", "get updates", "bot instance", "long poll", "long-poll"))
    )


def apply_access_control(api: TelegramAPI, updates):
    """Apply channel, membership and referral gates before business/private handlers."""
    return filter_telegram_updates(api, updates)


def _plain_home_keyboard() -> dict:
    """Zero-DB / zero-Premium emergency keyboard for /start.

    It intentionally uses only long-supported Bot API fields. A locked settings DB, broken
    Premium mapping, or a new button field must never make the bot look dead.
    """
    return {"inline_keyboard": [
        [
            {"text": "首页", "callback_data": "menu:home"},
            {"text": "消息", "callback_data": "menu:section:msg"},
            {"text": "财务", "callback_data": "menu:section:finance"},
            {"text": "其他", "callback_data": "menu:section:other"},
        ],
        [
            {"text": "记账", "callback_data": "menu:ledger"},
            {"text": "账单 / 总账", "callback_data": "menu:bill"},
        ],
        [
            {"text": "墨清反诈", "callback_data": "menu:antifraud"},
            {"text": "计算器", "callback_data": "tool:calc"},
        ],
        [
            {"text": "实时汇率", "callback_data": "tool:rate"},
            {"text": "设置", "callback_data": "menu:settings"},
        ],
    ]}


def _send_start_plain_fallback(api: TelegramAPI, chat_id: int, *, first_time: bool) -> None:
    if first_time:
        text = (
            "欢迎使用水杯记账！\n\n"
            "水杯记账是墨清记账的精简版本。这里保留记账、墨清反诈、防编辑/撤回、"
            "快捷消息 / 离线消息、计算器和实时汇率等高频功能。\n\n"
            "如果您希望体验更多功能和可玩性，可以使用墨清记账 @JiZhangAide_bot。"
        )
        markup = {"inline_keyboard": [[{"text": "收到", "callback_data": "onboarding:ack"}]]}
    else:
        text = (
            "水杯记账\n\n"
            "发送 +100 / -20 午饭快速记账；发送 @username 或数字 ID 查询诈骗记录。\n"
            "当前使用兼容菜单，完整界面可用后会自动恢复。"
        )
        markup = _plain_home_keyboard()
    # Bypass Premium/HTML conversion entirely. This is only a transport compatibility fallback.
    api.send_message_plain(chat_id, text, reply_markup=markup)

def _ui_title(semantic: str, title: str, fallback: str = "•") -> str:
    return f"{premium_system_icon(semantic, fallback)} <b>{html.escape(str(title or ''))}</b>"


def _ui_state(enabled: bool) -> str:
    return (premium_notice_green() + " 已开启") if enabled else (premium_notice_yellow() + " 已关闭")


def _ui_field(label: str, value: str, *, trusted: bool = False) -> str:
    val = str(value if value is not None else "-")
    if not trusted:
        val = html.escape(val)
    return f"{premium_notice_field()}{html.escape(str(label or '信息'))}：{val}"


def _ui_notice(state: str, title: str, detail: str = "", fields=None) -> str:
    """统一水杯短提示卡：与主机器人相同的 Premium notice banner/field 结构。"""
    st = str(state or "unknown").lower()
    if st not in {"safe", "risk", "unknown"}:
        st = "unknown"
    head = premium_notice_banner(st, str(title or "提示"))
    lines = [head]
    for label, value in (fields or []):
        lines.append(_ui_field(str(label), str(value)))
    if str(detail or "").strip():
        if len(lines) > 1:
            lines.append("")
        lines.append(html.escape(str(detail).strip()))
    lines.append(head)
    return "\n".join(lines)


def _tool_result_html(raw: str, semantic: str = "tools") -> str:
    """把只读工具的纯文本结果转换成与主机器人一致的安全 HTML 卡片。"""
    lines = str(raw or "").splitlines()
    if not lines:
        return _ui_title(semantic, "查询结果")
    first = lines[0].strip()
    first = re.sub(r"^[\s\u2600-\u27BF\U0001F000-\U0001FAFF\uFE0F\u200D]+", "", first).strip() or "查询结果"
    out = [_ui_title(semantic, first, "•"), ""]
    for line in lines[1:]:
        line = str(line or "").strip()
        if not line:
            out.append("")
            continue
        if line.endswith("："):
            out.append(f"<b>{html.escape(line[:-1])}</b>")
            continue
        if "：" in line:
            label, value = line.split("：", 1)
            out.append(_ui_field(label, value))
            continue
        out.append(html.escape(line))
    return "\n".join(out).strip()


# ShuiBei 的按钮高级 Emoji 统一从 premium_emoji_knowledge.json 解析。
# UI 代码只使用语义 key，不再散落 custom_emoji_id。


def _clean_button_text(text: str, icon_id: str = "") -> str:
    text = str(text or "").strip()
    if not icon_id:
        return text
    # 挂高级 Emoji 图标后移除最前面的普通 emoji，避免双图标。
    return re.sub(r"^[\s\u2600-\u27BF\U0001F000-\U0001FAFF\uFE0F\u200D]+\s*", "", text).strip() or text


def button(text: str, data: str, *, selected: bool = False, icon_custom_emoji_id: str = "", style: str = "") -> dict:
    icon = re.sub(r"\D", "", str(icon_custom_emoji_id or ""))
    out = {"text": _clean_button_text(text, icon), "callback_data": str(data)}
    # 与主机器人按钮语义一致：显式 danger/success/primary 优先；导航选中态默认绿色。
    style_norm = str(style or "").strip().lower()
    if style_norm in {"primary", "success", "danger"}:
        out["style"] = style_norm
    elif selected:
        out["style"] = "success"
    if icon:
        out["icon_custom_emoji_id"] = icon
    return out


def kb(rows):
    out = []
    for row in rows or []:
        new_row = []
        for item in row:
            if isinstance(item, dict):
                new_row.append(item)
                continue
            text, data = item[0], item[1]
            selected = bool(item[2]) if len(item) > 2 else False
            icon = str(item[3]) if len(item) > 3 else ""
            new_row.append(button(text, data, selected=selected, icon_custom_emoji_id=icon))
        if new_row:
            out.append(new_row)
    return {"inline_keyboard": out}


def _protection_enabled(owner_id: int) -> bool:
    st = get_settings(owner_id)
    return bool(int(st.get("anti_revoke_enabled", 0) or 0) and int(st.get("anti_edit_enabled", 0) or 0))


def _panel_tabs(active: str):
    tabs = [
        ("home", "首页", "menu:home", "home"),
        ("msg", "消息", "menu:section:msg", "messages"),
        ("finance", "财务", "menu:section:finance", "finance"),
        ("other", "其他", "menu:section:other", "more"),
    ]
    return [
        button(text, data, selected=(key == active), icon_custom_emoji_id=premium_icon(icon_key))
        for key, text, data, icon_key in tabs
    ]


def _panel_items(owner_id: int, active: str):
    protect_on = _protection_enabled(owner_id)
    sync_on = sync_enabled(owner_id)
    if active == "msg":
        return [
            [button("关键词回复", "menu:kw", icon_custom_emoji_id=premium_icon("keyword")),
             button("防撤回 / 防编辑", "menu:archive", selected=protect_on, icon_custom_emoji_id=premium_icon("protection"))],
            [button("FakeBot 检测", "menu:fakebot", icon_custom_emoji_id=premium_icon("fakebot"))],
        ]
    if active == "finance":
        return [
            [button("记账", "menu:ledger", icon_custom_emoji_id=premium_icon("ledger")),
             button("账本同步", "menu:sync", selected=sync_on, icon_custom_emoji_id=premium_icon("sync"))],
            [button("实时汇率", "tool:rate", icon_custom_emoji_id=premium_icon("rate")),
             button("链上工具", "menu:crypto", icon_custom_emoji_id=premium_icon("crypto"))],
        ]
    if active == "other":
        return [
            [button("墨清反诈", "menu:scam", icon_custom_emoji_id=premium_icon("risk")),
             button("FakeBot 检测", "menu:fakebot", icon_custom_emoji_id=premium_icon("fakebot"))],
            [button("防撤回 / 防编辑", "menu:archive", selected=protect_on, icon_custom_emoji_id=premium_icon("protection")),
             button("设置", "menu:settings", icon_custom_emoji_id=premium_icon("settings"))],
        ]
    return [
        [button("记账", "menu:ledger", icon_custom_emoji_id=premium_icon("ledger")),
         button("账本同步", "menu:sync", selected=sync_on, icon_custom_emoji_id=premium_icon("sync"))],
        [button("墨清反诈", "menu:scam", icon_custom_emoji_id=premium_icon("risk")),
         button("防撤回 / 防编辑", "menu:archive", selected=protect_on, icon_custom_emoji_id=premium_icon("protection"))],
        [button("设置", "menu:settings", icon_custom_emoji_id=premium_icon("settings"))],
    ]










def sync_keyboard(owner_id: int):
    enabled = sync_enabled(owner_id)
    rows = [[button("允许手动同步", "sync:toggle", selected=enabled, icon_custom_emoji_id=premium_icon("sync"))]]
    if enabled:
        rows.append([
            button("同步预览", "sync:preview", icon_custom_emoji_id=premium_icon("preview")),
            button("同步一次", "sync:confirm", icon_custom_emoji_id=premium_icon("sync")),
        ])
    rows.append([button("返回首页", "menu:home", icon_custom_emoji_id=premium_icon("back"))])
    return {"inline_keyboard": rows}





def protection_text(owner_id: int) -> str:
    enabled = _protection_enabled(owner_id)
    header = premium_notice_banner("safe" if enabled else "unknown", "防撤回 / 防编辑")
    return "\n".join([
        header,
        "",
        _ui_field("当前状态", _ui_state(enabled), trusted=True),
        _ui_field("作用范围", "已连接的 Telegram Business 会话"),
        "",
        "开启后，客户撤回或编辑消息时会通知你。",
        header,
    ])

def protection_keyboard(owner_id: int):
    enabled = _protection_enabled(owner_id)
    return {"inline_keyboard": [
        [button("关闭防撤回/防编辑" if enabled else "开启防撤回/防编辑", "protection:toggle", icon_custom_emoji_id=premium_icon("protection"), style=("danger" if enabled else "success"))],
        [button("返回首页", "menu:home", icon_custom_emoji_id=premium_icon("back"))],
    ]}


def _keyword_list(owner_id: int):
    conn = connect(APP_DB_PATH)
    try:
        rows = conn.execute("SELECT id,keyword,reply_text FROM keyword_replies WHERE owner_id=? ORDER BY id ASC", (int(owner_id),)).fetchall()
        return [dict(x) for x in rows]
    finally:
        conn.close()



def _kw_add(owner_id: int, keyword: str, reply: str) -> None:
    k, r = str(keyword or "").strip(), str(reply or "").strip()
    if not k or len(k) > 64 or not r or len(r) > 3500:
        raise ValueError("关键词需 1-64 字，回复需 1-3500 字")
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute("""
            INSERT INTO keyword_replies(owner_id,keyword,reply_text,created_at,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(owner_id,keyword) DO UPDATE SET reply_text=excluded.reply_text,updated_at=excluded.updated_at
        """, (int(owner_id), k, r, now, now))


def _kw_del(owner_id: int, keyword: str) -> bool:
    with tx(APP_DB_PATH, immediate=True) as conn:
        cur = conn.execute("DELETE FROM keyword_replies WHERE owner_id=? AND keyword=?", (int(owner_id), str(keyword or "").strip()))
        return int(cur.rowcount or 0) > 0


def _kw_match(owner_id: int, text: str):
    if not int(get_settings(owner_id).get("keyword_enabled", 1) or 0):
        return None
    source = str(text or "")
    rows = _keyword_list(owner_id)
    rows.sort(key=lambda x: len(str(x["keyword"])), reverse=True)
    low = source.lower()
    for row in rows:
        if str(row["keyword"]).lower() in low:
            return row
    return None


def _cooldown(owner_id: int, peer_id: int, key: str, seconds: int) -> bool:
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        row = conn.execute("SELECT alerted_at FROM risk_alert_state WHERE owner_id=? AND peer_id=? AND risk_key=?", (int(owner_id), int(peer_id), str(key))).fetchone()
        last = int(row[0] or 0) if row else 0
        if now - last < int(seconds):
            return False
        conn.execute("""
            INSERT INTO risk_alert_state(owner_id,peer_id,risk_key,alerted_at) VALUES(?,?,?,?)
            ON CONFLICT(owner_id,peer_id,risk_key) DO UPDATE SET alerted_at=excluded.alerted_at
        """, (int(owner_id), int(peer_id), str(key), now))
    return True






def _send_export(api: TelegramAPI, chat_id: int, owner_id: int):
    raw = export_text(owner_id).encode("utf-8")
    api.send_document_bytes(chat_id, f"shuibei_ledger_{owner_id}.txt", raw, "水杯记账账本导出")











# ================== v21：水杯记账统一体验层 ==================
# 目标：与墨清记账保持统一的记账/反诈/UI 体验，同时保持水杯的精简定位。
# 这里只改源码和新增兼容表；不重建、不删除、不覆盖任何现有数据库。
from features import (
    SECURITY_RISK, SECURITY_SAFE, SECURITY_UNKNOWN,
    ensure_feature_schema, onboarding_seen, onboarding_mark_seen,
    welcome_text as _v21_welcome_text, home_text as _v21_home_text,
    scam_result_html, fakebot_result_html, looks_like_scam_target,
    quick_list, quick_get, quick_upsert, quick_delete, quick_parse_keywords, quick_find_trigger, quick_set_delete_trigger, quick_update_reply, quick_update_keywords,
    offline_get, offline_save, offline_should_send, offline_mark_sent, offline_mark_owner_active,
    set_last_active_business, get_last_active_business,
    wait_set, wait_get, wait_clear,
    business_connection_upsert, try_calc_expression,
)
from ledger import bill_center_text, bill_list_text
from crypto_tools import convert_exchange_query
from fakebot import normalize_username


def link_button(text: str, url: str, *, icon_custom_emoji_id: str = "") -> dict:
    icon = re.sub(r"\D", "", str(icon_custom_emoji_id or ""))
    out = {"text": _clean_button_text(text, icon), "url": str(url)}
    if icon:
        out["icon_custom_emoji_id"] = icon
    return out


def start_text() -> str:
    return _v21_home_text()


def _v21_back_home():
    return button("返回首页", "menu:home", icon_custom_emoji_id=premium_icon("back"))


def _v21_panel_tabs(active: str):
    tabs = [
        ("home", "首页", "menu:home", "home"),
        ("msg", "消息", "menu:section:msg", "messages"),
        ("finance", "财务", "menu:section:finance", "finance"),
        ("other", "其他", "menu:section:other", "more"),
    ]
    return [button(t, d, selected=(k == active), icon_custom_emoji_id=premium_icon(i)) for k, t, d, i in tabs]


def _v21_panel_items(owner_id: int, active: str):
    # 首页/分区切换是最高频 UI 路径；同一份 owner_settings 一次读取即可，避免每次渲染重复查库。
    st = get_settings(owner_id)
    protect_on = bool(int(st.get("anti_revoke_enabled", 0) or 0) and int(st.get("anti_edit_enabled", 0) or 0))
    sync_on = bool(int(st.get("sync_enabled", 0) or 0))
    offline_on = bool(int(offline_get(owner_id).get("enabled") or 0))
    if active == "msg":
        return [
            [button("快捷消息", "menu:quick", icon_custom_emoji_id=premium_icon("keyword")),
             button("离线消息", "menu:offline", selected=offline_on, icon_custom_emoji_id=premium_icon("messages"))],
            [button("关键词回复", "menu:kw", icon_custom_emoji_id=premium_icon("keyword")),
             button("防撤回 / 防编辑", "menu:archive", selected=protect_on, icon_custom_emoji_id=premium_icon("protection"))],
        ]
    if active == "finance":
        return [
            [button("记账", "menu:ledger", icon_custom_emoji_id=premium_icon("ledger")),
             button("账本统计", "menu:bill", icon_custom_emoji_id=premium_icon("finance"))],
            [button("账本同步", "menu:sync", selected=sync_on, icon_custom_emoji_id=premium_icon("sync")),
             button("实时汇率", "tool:rate", icon_custom_emoji_id=premium_icon("rate"))],
            [button("计算器", "tool:calc", icon_custom_emoji_id=premium_icon("tools")),
             button("链上工具", "menu:crypto", icon_custom_emoji_id=premium_icon("crypto"))],
        ]
    if active == "other":
        return [
            [button("墨清反诈", "menu:antifraud", icon_custom_emoji_id=premium_icon("risk")),
             button("FakeBot 检测", "menu:fakebot", icon_custom_emoji_id=premium_icon("fakebot"))],
            [button("防撤回 / 防编辑", "menu:archive", selected=protect_on, icon_custom_emoji_id=premium_icon("protection")),
             button("设置", "menu:settings", icon_custom_emoji_id=premium_icon("settings"))],
            [link_button("完整功能：墨清记账", "https://t.me/JiZhangAide_bot", icon_custom_emoji_id=premium_icon("home"))],
        ]
    return [
        [button("墨清反诈", "menu:antifraud", icon_custom_emoji_id=premium_icon("risk")),
         button("防撤回 / 防编辑", "menu:archive", selected=protect_on, icon_custom_emoji_id=premium_icon("protection"))],
        [button("记账", "menu:ledger", icon_custom_emoji_id=premium_icon("ledger")),
         button("账本统计", "menu:bill", icon_custom_emoji_id=premium_icon("finance"))],
        [button("快捷消息", "menu:quick", icon_custom_emoji_id=premium_icon("keyword")),
         button("离线消息", "menu:offline", selected=offline_on, icon_custom_emoji_id=premium_icon("messages"))],
        [button("设置", "menu:settings", icon_custom_emoji_id=premium_icon("settings"))],
    ]


def main_keyboard(owner_id: int, active: str = "home"):
    active = active if active in {"home", "msg", "finance", "other"} else "home"
    rows = [_v21_panel_tabs(active)]
    rows.extend(_v21_panel_items(owner_id, active))
    return {"inline_keyboard": rows}



def section_text(section: str) -> str:
    if section == "msg":
        return "\n".join([
            _ui_title("messages", "消息助手", "💬"), "",
            _ui_field("快捷消息", "在 Business 私聊发送触发关键词，自动发送对应模板给当前聊天"),
            _ui_field("离线消息", "客户联系时按设置的最短间隔自动回复"),
            _ui_field("关键词回复", "客户消息包含关键词后自动回复"),
            _ui_field("防撤回 / 防编辑", "客户编辑或撤回消息时提醒你"),
        ])
    if section == "finance":
        return "\n".join([
            _ui_title("finance", "财务中心", "💎"), "",
            _ui_field("个人记账", "入金、出金、清账、撤销、指定消费与导出"),
            _ui_field("账单中心", "查看欠款、预付款和全部非零账单"),
            _ui_field("账本同步", "仅同步个人账本，需手动执行"),
                        _ui_field("工具", "计算器、实时汇率和链上查询"),
        ])
    if section == "other":
        return "\n".join([
            _ui_title("more", "工具中心", "⚙️"), "",
            _ui_field("墨清反诈", "诈骗记录查询与 Business 风险提醒"),
            _ui_field("FakeBot 检测", "机器人高仿风险检测"),
            _ui_field("Business 保护", "防撤回 / 防编辑与自动提醒"),
            _ui_field("设置", "记账单位与各功能开关"),
        ])
    return _v21_home_text()


def sync_menu_text(owner_id: int) -> str:
    enabled = sync_enabled(owner_id)
    return "\n".join([
        _ui_title("sync", "账本同步", "🔄"),
        "",
        _ui_field("当前状态", _ui_state(enabled), trusted=True),
        _ui_field("同步范围", "仅同步个人账本"),
        _ui_field("同步方式", "手动同步"),
        "",
        "需要时点击“同步一次”即可更新个人账本。",
    ])

def _v21_antifraud_text() -> str:
    return "\n".join([
        _ui_title("risk", "墨清反诈", "🛡️"),
        "",
        _ui_field("自动检测", "Business 会话自动检测诈骗风险并提醒"),
        _ui_field("手动查询", "直接发送 @username、username 或 Telegram 数字 ID"),
        _ui_field("查询结果", "命中记录时展示风险详情；未命中时明确告知"),
    ])


def _v21_ledger_text() -> str:
    return "\n".join([
        _ui_title("ledger", "记账", "📒"), "",
        "<code>+100</code>  入账",
        "<code>-20 午饭</code>  出账",
        "<code>/</code>  合集账本（普通记账 + 指定消费）",
        "<code>/balance</code>  当前余额",
        "<code>/undo</code>  撤销上一条",
        "<code>//</code>  清账",
        "<code>/export</code>  导出个人账本",
    ])


def _v21_bill_keyboard():
    return {"inline_keyboard": [
        [button("欠款账单", "bill:debt", icon_custom_emoji_id=premium_icon("finance"), style="danger")],
        [button("预付款账单", "bill:prepay", icon_custom_emoji_id=premium_icon("finance"), style="success")],
        [button("总账", "bill:all", icon_custom_emoji_id=premium_icon("finance"), style="primary")],
        [button("导出全部", "bill:export", icon_custom_emoji_id=premium_icon("export"), style="primary")],
        [_v21_back_home()],
    ]}


def _v21_quick_text(owner_id: int) -> str:
    rows = quick_list(owner_id)
    lines = [
        _ui_title("messages", "快捷消息", "⚡"),
        "",
        _ui_field("当前已设置", f"{len(rows)} 条"),
        "",
        "用法：你在任意 Business 私聊发送触发关键词，机器人会自动把对应模板发送给当前聊天。",
        "例如关键词 <code>u</code>，模板填写 USDT 地址。",
        "限制：最多 20 个触发关键词，单个关键词最多 80 字，回复模板最多 3000 字。",
    ]
    if rows:
        lines.extend(["", "<b>当前列表</b>"])
        for r in rows[:15]:
            kws = " / ".join(str(x) for x in (r.get("keywords") or []))
            body = str(r.get("reply_text") or "").replace("\n", " ").strip()
            body = body[:36] + ("…" if len(body) > 36 else "")
            lines.append(f"#{int(r.get('id') or 0)}｜{html.escape(kws)} → {html.escape(body)}")
    return "\n".join(lines)

def _v21_quick_keyboard(owner_id: int):
    rows = [[button("新增快捷消息", "quick:add", icon_custom_emoji_id=premium_icon("add"), style="primary")]]
    for r in quick_list(owner_id)[:10]:
        rid = int(r.get("id") or 0)
        label = " / ".join(str(x) for x in (r.get("keywords") or [])) or f"#{rid}"
        rows.append([button("管理 " + label[:36], f"quick:view:{rid}", icon_custom_emoji_id=premium_icon("preview"), style="primary")])
    rows.append([button("返回主面板", "menu:home", icon_custom_emoji_id=premium_icon("back"), style="primary")])
    return {"inline_keyboard": rows}



def _v21_quick_detail_text(record: dict) -> str:
    if not record:
        return _ui_notice("unknown", "快捷消息不存在")
    return "\n".join([
        _ui_title("messages", "快捷消息详情", "⚡"), "",
        _ui_field("关键词", html.escape(" / ".join(record.get("keywords") or [])), trusted=True),
        _ui_field("删除触发消息", "开启" if int(record.get("delete_trigger") or 0) else "关闭"),
        "", "<b>回复内容：</b>",
        f"<pre>{html.escape(str(record.get('reply_text') or ''))}</pre>",
    ])




def _v21_quick_detail_keyboard(record: dict):
    rid = int((record or {}).get("id") or 0)
    on = bool(int((record or {}).get("delete_trigger") or 0))
    return {"inline_keyboard": [
        [button("预览", f"quick:preview:{rid}", icon_custom_emoji_id=premium_icon("preview"), style="primary"),
         button("修改文本", f"quick:editreply:{rid}", icon_custom_emoji_id=premium_icon("edit"), style="primary")],
        [button("修改触发词", f"quick:editkw:{rid}", icon_custom_emoji_id=premium_icon("edit"), style="primary"),
         button("删除", f"quick:delask:{rid}", icon_custom_emoji_id=premium_icon("delete"), style="danger")],
        [button(("关闭" if on else "开启") + " 发送后删除触发消息", f"quick:toggledel:{rid}", selected=on, icon_custom_emoji_id=premium_icon("delete"))],
        [button("返回快捷消息", "menu:quick", icon_custom_emoji_id=premium_icon("back"), style="primary")],
    ]}



def _v21_offline_text(owner_id: int) -> str:
    st = offline_get(owner_id); enabled = bool(int(st.get("enabled") or 0)); skip_online = bool(int(st.get("skip_when_online", 1) or 0))
    return "\n".join([
        _ui_title("messages", "离线消息", "📴"), "",
        _ui_field("状态", _ui_state(enabled), trusted=True),
        _ui_field("同一用户触发间隔", f"{max(1, int(st.get('interval_seconds') or 3600)//60)} 分钟"),
        _ui_field("客服在线时", "不触发回复" if skip_online else "仍然触发回复"),
        "", "触发规则：客户向你的 Business 托管聊天发送消息时触发；默认关闭。",
        "在线策略：水杯会根据托管账号最近的主动 Business 发言判断在线状态。",
        "", "<b>当前模板：</b>", f"<pre>{html.escape(str(st.get('template_text') or ''))}</pre>",
    ])



def _v21_offline_keyboard(owner_id: int):
    st = offline_get(owner_id); enabled = bool(int(st.get("enabled") or 0)); skip_online = bool(int(st.get("skip_when_online", 1) or 0))
    return {"inline_keyboard": [
        [button("关闭功能" if enabled else "开启功能", "offline:toggle", selected=enabled, icon_custom_emoji_id=premium_icon("messages")),
         button("预览", "offline:preview", icon_custom_emoji_id=premium_icon("preview"), style="primary")],
        [button("修改模板", "offline:template", icon_custom_emoji_id=premium_icon("edit"), style="primary"),
         button("修改间隔", "offline:interval_menu", icon_custom_emoji_id=premium_icon("protection"), style="primary")],
        [button("在线时也触发" if skip_online else "在线时不触发", "offline:toggle_skip_online", selected=(not skip_online), icon_custom_emoji_id=premium_icon("messages"))],
        [button("返回消息助手", "menu:section:msg", icon_custom_emoji_id=premium_icon("back"), style="primary")],
    ]}



def _v21_offline_interval_keyboard(owner_id: int):
    interval = int(offline_get(owner_id).get("interval_seconds") or 3600)
    return {"inline_keyboard": [
        [button("5分钟", "offline:interval:300", selected=(interval == 300), icon_custom_emoji_id=premium_icon("protection")),
         button("30分钟", "offline:interval:1800", selected=(interval == 1800), icon_custom_emoji_id=premium_icon("protection"))],
        [button("1小时", "offline:interval:3600", selected=(interval == 3600), icon_custom_emoji_id=premium_icon("protection")),
         button("6小时", "offline:interval:21600", selected=(interval == 21600), icon_custom_emoji_id=premium_icon("protection")),
         button("24小时", "offline:interval:86400", selected=(interval == 86400), icon_custom_emoji_id=premium_icon("protection"))],
        [button("返回离线消息", "menu:offline", icon_custom_emoji_id=premium_icon("back"), style="primary")],
    ]}

def _v21_fakebot_manual(arg: str) -> str:
    u = normalize_username(arg)
    if not u:
        return "发送机器人用户名，例如：<code>@example_bot</code>"
    available, hit, official = find_suspect_state(u)
    return fakebot_result_html(u, hit, official=official, available=available)

def _v21_send_scam_result(api: TelegramAPI, chat_id: int, target: str) -> None:
    ok, rows = query_records(target)
    api.send_message(chat_id, scam_result_html(target, ok, rows))



def _v21_wait_input(api: TelegramAPI, owner_id: int, chat_id: int, text: str) -> bool:
    state = wait_get(owner_id)
    if not state: return False
    if text.lower() in {"/cancel", "取消"}:
        wait_clear(owner_id); api.send_message(chat_id, _ui_notice("safe", "已取消", "本次设置已取消。")); return True
    kind = str(state.get("kind") or ""); payload = state.get("payload") or {}
    if kind == "scam_query":
        if not looks_like_scam_target(text):
            api.send_message(chat_id, "请输入 Telegram 用户名或数字 ID，例如 <code>@username</code> 或 <code>123456789</code>。\n发送 <code>/cancel</code> 退出查询。"); return True
        _v21_send_scam_result(api, chat_id, text); return True
    if kind == "fakebot_query":
        u = normalize_username(text)
        if not u: api.send_message(chat_id, "请输入机器人用户名，例如 <code>@example_bot</code>。\n发送 <code>/cancel</code> 退出检测。"); return True
        api.send_message(chat_id, _v21_fakebot_manual(u)); return True
    if kind == "calculator":
        if text.startswith("/") and text not in {"/", "//"}: return False
        hit, result = try_calc_expression(text)
        if hit: api.send_message(chat_id, result or "计算失败。")
        else:
            head = premium_notice_banner("unknown", "计算格式错误")
            api.send_message(chat_id, "\n".join([head, "例如：<code>1-1+1*2</code>、<code>（2+3）*4</code>、<code>10÷2</code>、<code>2^3</code>。", "发送 <code>/cancel</code> 退出计算器。", head]))
        return True
    if kind in {"quick_new_title", "quick_edit_keywords"}:
        try: keywords = quick_parse_keywords(text)
        except Exception as exc: api.send_message(chat_id, _ui_notice("unknown", "触发关键词格式错误", str(exc))); return True
        if kind == "quick_edit_keywords":
            rid = int(payload.get("record_id") or 0)
            try:
                if not quick_update_keywords(owner_id, rid, keywords): raise ValueError("快捷消息不存在")
            except Exception as exc: api.send_message(chat_id, _ui_notice("unknown", "操作失败", str(exc))); return True
            wait_clear(owner_id); rec=quick_get(owner_id,rid); api.send_message(chat_id,_ui_notice("safe","触发关键词已更新"),reply_markup=_v21_quick_detail_keyboard(rec)); return True
        wait_set(owner_id, "quick_new_body", {"keywords": keywords})
        api.send_message(chat_id, "触发关键词：<b>"+html.escape(" / ".join(keywords))+"</b>\n\n现在发送快捷消息模板正文（1-3000 字）。"); return True
    if kind == "quick_new_body":
        try: rid=quick_upsert(owner_id,list(payload.get("keywords") or []),text)
        except Exception as exc: api.send_message(chat_id,_ui_notice("unknown","操作失败",str(exc))); return True
        wait_clear(owner_id); rec=quick_get(owner_id,rid); api.send_message(chat_id,_ui_notice("safe","快捷消息已保存"),reply_markup=_v21_quick_detail_keyboard(rec)); return True
    if kind == "quick_edit_reply_only":
        rid=int(payload.get("record_id") or 0)
        try:
            if not quick_update_reply(owner_id,rid,text): raise ValueError("快捷消息不存在")
        except Exception as exc: api.send_message(chat_id,_ui_notice("unknown","操作失败",str(exc))); return True
        wait_clear(owner_id); rec=quick_get(owner_id,rid); api.send_message(chat_id,_ui_notice("safe","快捷消息文本已更新"),reply_markup=_v21_quick_detail_keyboard(rec)); return True
    if kind == "kw_new_keyword":
        key=str(text or "").strip()
        if not key or len(key)>64: api.send_message(chat_id,_ui_notice("unknown","关键词格式错误","关键词需 1-64 字。")); return True
        wait_set(owner_id,"kw_new_reply",{"keyword":key}); api.send_message(chat_id,f"关键词：<code>{html.escape(key)}</code>\n\n现在发送回复正文（1-3500 字，支持 HTML 富文本）。"); return True
    if kind == "kw_new_reply":
        try: _kw_add(owner_id,str(payload.get("keyword") or ""),text)
        except Exception as exc: api.send_message(chat_id,_ui_notice("unknown","操作失败",str(exc))); return True
        wait_clear(owner_id); api.send_message(chat_id,_ui_notice("safe","关键词回复已保存"),reply_markup=_keyword_menu_keyboard(owner_id)); return True
    if kind == "kw_edit_reply":
        rid=int(payload.get("record_id") or 0)
        try:
            if not _keyword_update_reply(owner_id,rid,text): raise ValueError("关键词不存在")
        except Exception as exc: api.send_message(chat_id,_ui_notice("unknown","操作失败",str(exc))); return True
        wait_clear(owner_id); item=_keyword_get(owner_id,rid); api.send_message(chat_id,_ui_notice("safe","关键词回复已更新"),reply_markup=_keyword_detail_keyboard(item)); return True
    if kind == "kw_edit_keyword":
        rid=int(payload.get("record_id") or 0)
        try:
            if not _keyword_update_keyword(owner_id,rid,text): raise ValueError("关键词不存在")
        except Exception as exc: api.send_message(chat_id,_ui_notice("unknown","操作失败",str(exc))); return True
        wait_clear(owner_id); item=_keyword_get(owner_id,rid); api.send_message(chat_id,_ui_notice("safe","关键词已更新"),reply_markup=_keyword_detail_keyboard(item)); return True
    if kind == "offline_template":
        try: offline_save(owner_id, template_text=text)
        except Exception as exc: api.send_message(chat_id,_ui_notice("unknown","操作失败",str(exc))); return True
        wait_clear(owner_id); api.send_message(chat_id,_ui_notice("safe","离线消息已更新"),reply_markup=_v21_offline_keyboard(owner_id)); return True
    wait_clear(owner_id); return False



def _private_command(api: TelegramAPI, message: dict) -> bool:
    chat = message.get("chat") or {}
    if str(chat.get("type") or "") != "private":
        return False
    chat_id = int(chat.get("id") or 0)
    # Telegram private chat_id normally equals user_id, but equality is not a routing requirement.
    # Never silently drop /start just because an unusual update shape does not satisfy that invariant.
    owner_id = int((message.get("from") or {}).get("id") or chat_id or 0)
    text = str(message.get("text") or "").strip()
    if owner_id <= 0 or chat_id <= 0:
        return True
    user = message.get("from") or {}
    user_name = (str(user.get("first_name") or "") + (" " + str(user.get("last_name") or "") if user.get("last_name") else "")).strip() or "未知"

    if text.startswith("/start"):
        # /start 是健康检查级入口：数据库状态失败不能阻断，Premium UI 不兼容时必须有纯文本兜底。
        print("[ShuiBei] private /start received", flush=True)
        try:
            wait_clear(owner_id)
        except Exception:
            pass
        try:
            seen = bool(onboarding_seen(owner_id))
        except Exception as exc:
            print(f"[ShuiBei] onboarding read fallback: {type(exc).__name__}", flush=True)
            seen = False
        try:
            if not seen:
                markup = {"inline_keyboard": [[button("收到", "onboarding:ack", selected=True, icon_custom_emoji_id=premium_icon("confirm"))]]}
                api.send_message(chat_id, _v21_welcome_text(), reply_markup=markup)
            else:
                api.send_message(chat_id, _r29_home_text(owner_id), reply_markup=main_keyboard(owner_id, "home"))
            print("[ShuiBei] private /start responded with full UI", flush=True)
        except TelegramAPIError as exc:
            # Only representation incompatibility is safe for a transport retry. 401/403/network errors
            # still bubble up so a possibly-delivered POST is not blindly replayed.
            low = str(exc or "").lower()
            if any(k in low for k in ("bad request", "parse", "button", "entity", "emoji", "markup")):
                print(f"[ShuiBei] /start full UI rejected; plain fallback: {str(exc)[:180]}", flush=True)
                _send_start_plain_fallback(api, chat_id, first_time=(not seen))
                print("[ShuiBei] private /start responded with plain fallback", flush=True)
            else:
                raise
        except Exception as exc:
            # Local UI/settings/database failures happen before Telegram transport. /start must remain
            # usable even when SQLite is locked or an optional renderer/mapping is broken.
            print(f"[ShuiBei] /start local UI failure -> zero-DB fallback: {type(exc).__name__}: {str(exc)[:180]}", flush=True)
            _runtime_status("start_ui_fallback", detail=f"{type(exc).__name__}: {str(exc)[:260]}")
            _send_start_plain_fallback(api, chat_id, first_time=(not seen))
            print("[ShuiBei] private /start responded with zero-DB fallback", flush=True)
        return True
    if text == "/menu":
        wait_clear(owner_id)
        api.send_message(chat_id, _r29_home_text(owner_id), reply_markup=main_keyboard(owner_id, "home")); return True

    if _v21_wait_input(api, owner_id, chat_id, text):
        return True

    if text == "/sync":
        api.send_message(chat_id, sync_menu_text(owner_id), reply_markup=sync_keyboard(owner_id)); return True
    if text in {"/bill", "/bills", "/total"}:
        api.send_message(chat_id, bill_center_text(owner_id), reply_markup=_v21_bill_keyboard()); return True
    if text == "/quick":
        api.send_message(chat_id, _v21_quick_text(owner_id), reply_markup=_v21_quick_keyboard(owner_id)); return True
    if text == "/offline":
        api.send_message(chat_id, _v21_offline_text(owner_id), reply_markup=_v21_offline_keyboard(owner_id)); return True
    if text.startswith("/scam"):
        arg = text[len("/scam"):].strip()
        if not arg:
            api.send_message(chat_id, _v21_antifraud_text())
        else:
            _v21_send_scam_result(api, chat_id, arg)
        return True
    if text.startswith("/fakebot"):
        api.send_message(chat_id, _v21_fakebot_manual(text[len("/fakebot"):].strip())); return True
    if text.startswith("/usdt") or text.startswith("/tron"):
        arg = text.split(maxsplit=1)[1].strip() if " " in text else ""
        api.send_message(chat_id, _tool_result_html(query_tron(arg), "crypto")); return True
    if text.startswith("/ton"):
        arg = text.split(maxsplit=1)[1].strip() if " " in text else ""
        api.send_message(chat_id, _tool_result_html(query_ton(arg), "crypto")); return True
    if text in ("/rate", "/rates"):
        api.send_message(chat_id, _tool_result_html(exchange_rate_text(), "finance")); return True
    if text == "/kwlist":
        api.send_message(chat_id, keyword_menu_text(owner_id), reply_markup=kb([[_v21_back_home()]])); return True
    if text.startswith("/kwadd "):
        body = text[len("/kwadd "):].strip()
        if "=>" not in body:
            api.send_message(chat_id, "格式：<code>/kwadd 关键词 =&gt; 回复内容</code>")
        else:
            key, reply = body.split("=>", 1)
            try:
                _kw_add(owner_id, key, reply); api.send_message(chat_id, _ui_notice("safe", "关键词回复已保存"))
            except Exception as exc:

                api.send_message(chat_id, _ui_notice("unknown", "操作失败", str(exc)))
        return True
    if text.startswith("/kwdel "):
        key = text[len("/kwdel "):].strip()
        api.send_message(chat_id, _ui_notice("safe", "关键词已删除") if _kw_del(owner_id, key) else _ui_notice("unknown", "未找到关键词")); return True
    if text == "/archive":
        api.send_message(chat_id, protection_text(owner_id), reply_markup=protection_keyboard(owner_id)); return True

    if is_tron_address(text):
        api.send_message(chat_id, _tool_result_html(query_tron(text), "crypto")); return True
    if is_ton_target(text):
        api.send_message(chat_id, _tool_result_html(query_ton(text), "crypto")); return True

    calc_hit, calc_text = try_calc_expression(text)
    if calc_hit:
        api.send_message(chat_id, calc_text or "计算失败。"); return True
    rate_hit, rate_text = convert_exchange_query(text)
    if rate_hit:
        api.send_message(chat_id, rate_text or _ui_notice("unknown", "汇率暂不可用", "实时汇率暂时无法获取，请稍后重试。")); return True
    if looks_like_scam_target(text):
        _v21_send_scam_result(api, chat_id, text); return True

    handled, reply, export = handle_ledger_text(owner_id, 0, user_name, text)
    if handled:
        if export is not None:
            _send_export(api, chat_id, owner_id)
        elif reply is not None:
            # ledger.py 已经负责 HTML 转义/模板，不再二次 escape。
            api.send_message(chat_id, reply, parse_mode="HTML")
        return True
    return False


def _scam_auto_alert(api: TelegramAPI, owner_id: int, chat_id: int, sender: dict) -> None:
    """Business 自动反诈只认墨清 Developer API 返回的已收录精确结果。

    Telegram ID 存在时只按 ID 查询；绝不再合并历史 username 记录。只有事件没有 ID 时才按 username 精确查询。
    客户端不持有反诈数据库，API 不可用时 fail-closed：保持静默，不把“查不到”伪装成安全。
    """
    if not int(get_settings(owner_id).get("scam_detect_enabled", 1) or 0):
        return
    uid = int((sender or {}).get("id") or 0)
    username = str((sender or {}).get("username") or "").strip().lstrip("@")
    target = str(uid) if uid > 0 else (("@" + username) if username else "")
    if not target:
        return
    available, rows = query_records(target)
    # 自动提醒仅对数据库明确命中触发。数据库不可用/零记录均保持静默。
    if not available or not rows:
        return
    if not _cooldown(owner_id, chat_id, f"scam:{uid}:{username.lower()}", RISK_ALERT_COOLDOWN_SECONDS):
        return
    try:
        api.send_message(owner_id, scam_result_html(target, True, rows))
    except Exception:
        pass


def _fakebot_auto_alert(api: TelegramAPI, owner_id: int, chat_id: int, sender: dict, message: dict) -> None:
    if not int(get_settings(owner_id).get("fakebot_detect_enabled", 1) or 0):
        return
    via = message.get("via_bot") or message.get("viaBot")
    if not isinstance(via, dict):
        return
    suspect = normalize_username(str(via.get("username") or ""))
    if not suspect:
        return
    available, hit, official = find_suspect_state(suspect)
    # 自动检测仅在数据源可用且明确命中高仿时提醒；数据源故障不伪装成安全，也不制造误报。
    if not available or official or not hit:
        return
    sender_id = int((sender or {}).get("id") or 0)
    if not _cooldown(owner_id, chat_id, f"fakebot:{hit['suspect']}:{hit['official']}:{sender_id}", FAKEBOT_ALERT_COOLDOWN_SECONDS):
        return
    try:
        api.send_message(owner_id, fakebot_result_html(suspect, hit, official=False))
    except Exception:
        pass



def settings_text(owner_id: int) -> str:
    st=get_settings(owner_id); unit=str(st.get("ledger_currency") or "USDT").upper(); unit=unit if unit in ALLOWED_CURRENCIES else "USDT"
    return "\n".join([_ui_title("settings","会员设置中心 v2","💎"),"","<b>当前状态：</b>",f"反诈提醒：{'已开启' if int(st.get('scam_detect_enabled',1) or 0) else '已关闭'}",f"离线消息：{'已开启' if int(offline_get(owner_id).get('enabled') or 0) else '已关闭'}",f"快捷消息：{len(quick_list(owner_id))} 条｜关键词回复：{len(_keyword_list(owner_id))} 条",f"默认币种：{html.escape(unit)}","","请选择要设置的分类。常用功能放在最上面。"])

def settings_keyboard(owner_id: int):
    return {"inline_keyboard":[[button("常用设置","setv2:cat:common",icon_custom_emoji_id=premium_icon("settings"),style="primary")],[button("消息与回复","setv2:cat:message",icon_custom_emoji_id=premium_icon("messages"),style="primary")],[button("安全与反诈","setv2:cat:security",icon_custom_emoji_id=premium_icon("risk"),style="primary")],[button("记账与财务","setv2:cat:ledger",icon_custom_emoji_id=premium_icon("ledger"),style="primary")],[button("返回主面板","menu:home",icon_custom_emoji_id=premium_icon("back"),style="primary")]]}

def keyword_menu_text(owner_id: int) -> str:
    return "\n".join([_ui_title("keyword","关键词回复设置","💎"),f"当前已设置：{len(_keyword_list(owner_id))} 条","","客户给你发送的文本只要包含关键词，机器人会使用你设置的内容自动回复。","支持 HTML 富文本。","限制：关键词最多 64 字，回复正文最多 3500 字。"])

def _keyword_get(owner_id:int,record_id:int):
    conn=connect(APP_DB_PATH)
    try:
        row=conn.execute("SELECT id,keyword,reply_text FROM keyword_replies WHERE owner_id=? AND id=?",(int(owner_id),int(record_id))).fetchone(); return dict(row) if row else None
    finally: conn.close()

def _keyword_update_reply(owner_id:int,record_id:int,reply_text:str)->bool:
    reply=str(reply_text or "").strip()
    if not reply or len(reply)>3500: raise ValueError("回复正文需 1-3500 字")
    with tx(APP_DB_PATH,immediate=True) as conn: return int(conn.execute("UPDATE keyword_replies SET reply_text=?,updated_at=? WHERE owner_id=? AND id=?",(reply,int(time.time()),int(owner_id),int(record_id))).rowcount or 0)>0

def _keyword_update_keyword(owner_id:int,record_id:int,keyword:str)->bool:
    key=str(keyword or "").strip()
    if not key or len(key)>64: raise ValueError("关键词需 1-64 字")
    with tx(APP_DB_PATH,immediate=True) as conn:
        if conn.execute("SELECT 1 FROM keyword_replies WHERE owner_id=? AND keyword=? AND id<>? LIMIT 1",(int(owner_id),key,int(record_id))).fetchone(): raise ValueError("已有相同关键词")
        return int(conn.execute("UPDATE keyword_replies SET keyword=?,updated_at=? WHERE owner_id=? AND id=?",(key,int(time.time()),int(owner_id),int(record_id))).rowcount or 0)>0

def _keyword_delete_id(owner_id:int,record_id:int)->bool:
    with tx(APP_DB_PATH,immediate=True) as conn: return int(conn.execute("DELETE FROM keyword_replies WHERE owner_id=? AND id=?",(int(owner_id),int(record_id))).rowcount or 0)>0

def _keyword_menu_keyboard(owner_id:int):
    return {"inline_keyboard":[[button("设置关键词","kw:add",icon_custom_emoji_id=premium_icon("add"),style="success"),button("查看关键词","kw:list",icon_custom_emoji_id=premium_icon("preview"),style="primary")],[button("更改关键词","kw:choose_edit",icon_custom_emoji_id=premium_icon("edit"),style="primary"),button("删除关键词","kw:choose_delete",icon_custom_emoji_id=premium_icon("delete"),style="danger")],[button("返回主面板","menu:home",icon_custom_emoji_id=premium_icon("back"),style="primary")]]}

def _keyword_list_keyboard(owner_id:int,action:str="view"):
    prefix={"view":"kw:view:","edit":"kw:view:","delete":"kw:delask:"}.get(action,"kw:view:"); rows=[]
    for item in _keyword_list(owner_id)[:20]: rows.append([button(str(item.get("keyword") or "")[:42],prefix+str(int(item.get("id") or 0)),icon_custom_emoji_id=premium_icon("keyword"),style="primary")])
    rows.append([button("返回关键词设置","menu:kw",icon_custom_emoji_id=premium_icon("back"),style="primary")]); return {"inline_keyboard":rows}

def _keyword_detail_text(item):
    if not item: return _ui_notice("unknown","关键词不存在或已删除")
    return "\n".join([_ui_title("keyword","关键词详情","💎"),"",f"关键词：<code>{html.escape(str(item.get('keyword') or ''))}</code>","","回复内容：",f"<pre>{html.escape(str(item.get('reply_text') or ''))}</pre>"])

def _keyword_detail_keyboard(item):
    rid=int((item or {}).get("id") or 0); return {"inline_keyboard":[[button("预览",f"kw:preview:{rid}",icon_custom_emoji_id=premium_icon("preview"),style="primary"),button("修改回复",f"kw:editreply:{rid}",icon_custom_emoji_id=premium_icon("edit"),style="primary")],[button("修改关键词",f"kw:editkw:{rid}",icon_custom_emoji_id=premium_icon("edit"),style="primary"),button("删除",f"kw:delask:{rid}",icon_custom_emoji_id=premium_icon("delete"),style="danger")],[button("返回关键词设置","menu:kw",icon_custom_emoji_id=premium_icon("back"),style="primary")]]}

def settings_category_text(owner_id:int,cat:str)->str:
    st=get_settings(owner_id); cat=str(cat or "common"); unit=str(st.get("ledger_currency") or "USDT").upper()
    if cat=="common": return "\n".join([_ui_title("settings","常用设置","💎"),"",f"反诈提醒：{'已开启' if int(st.get('scam_detect_enabled',1) or 0) else '已关闭'}",f"快捷消息：{len(quick_list(owner_id))} 条",f"关键词回复：{len(_keyword_list(owner_id))} 条",f"离线消息：{'已开启' if int(offline_get(owner_id).get('enabled') or 0) else '已关闭'}",f"默认币种：{html.escape(unit)}","","这里放最常用、最容易改的入口。"])
    if cat=="message": return "\n".join([_ui_title("messages","消息与回复","💬"),"",f"快捷消息：{len(quick_list(owner_id))} 条",f"关键词回复：{len(_keyword_list(owner_id))} 条",f"离线消息：{'已开启' if int(offline_get(owner_id).get('enabled') or 0) else '已关闭'}","","用户消息、快捷消息、关键词回复、离线消息统一放这里。"])
    if cat=="security": return "\n".join([_ui_title("risk","安全与反诈","🛡️"),"",f"反诈风险提醒：{'已开启' if int(st.get('scam_detect_enabled',1) or 0) else '已关闭'}",f"FakeBot 高仿提醒：{'已开启' if int(st.get('fakebot_detect_enabled',1) or 0) else '已关闭'}",f"防撤回 / 防编辑：{'已开启' if _protection_enabled(owner_id) else '已关闭'}","","风险提醒、FakeBot 检测与消息保护统一放这里。"])
    if cat=="ledger": return "\n".join([_ui_title("ledger","记账与财务","📒"),"",f"默认币种：{html.escape(unit)}",f"指定消费账本：{'已开启' if int(st.get('item_ledger_enabled') or 0) else '已关闭'}","","账本、余额、总账单和汇率统一放这里。"])
    return settings_text(owner_id)

def settings_category_keyboard(owner_id:int,cat:str):
    st=get_settings(owner_id); cat=str(cat or "common"); rows=[]
    if cat=="common":
        on=bool(int(st.get("scam_detect_enabled",1) or 0)); rows=[[button(("关闭" if on else "开启")+" 反诈风险提醒","setv2:toggle:scam_detect_enabled:common",selected=on,icon_custom_emoji_id=premium_icon("risk"))],[button("快捷消息","menu:quick",icon_custom_emoji_id=premium_icon("messages"),style="primary")],[button("关键词回复","menu:kw",icon_custom_emoji_id=premium_icon("keyword"),style="primary")],[button("离线消息","menu:offline",icon_custom_emoji_id=premium_icon("messages"),style="primary")],[button("默认币种","settings:currency",icon_custom_emoji_id=premium_icon("finance"),style="primary")]]
    elif cat=="message": rows=[[button("快捷消息","menu:quick",icon_custom_emoji_id=premium_icon("messages"),style="primary")],[button("关键词回复","menu:kw",icon_custom_emoji_id=premium_icon("keyword"),style="primary")],[button("离线消息","menu:offline",icon_custom_emoji_id=premium_icon("messages"),style="primary")]]
    elif cat=="security":
        a=bool(int(st.get("scam_detect_enabled",1) or 0)); b=bool(int(st.get("fakebot_detect_enabled",1) or 0)); c=_protection_enabled(owner_id); rows=[[button(("关闭" if a else "开启")+" 反诈风险提醒","setv2:toggle:scam_detect_enabled:security",selected=a,icon_custom_emoji_id=premium_icon("risk"))],[button(("关闭" if b else "开启")+" FakeBot 高仿提醒","setv2:toggle:fakebot_detect_enabled:security",selected=b,icon_custom_emoji_id=premium_icon("fakebot"))],[button(("关闭" if c else "开启")+" 防撤回 / 防编辑","setv2:toggle:protection:security",selected=c,icon_custom_emoji_id=premium_icon("protection"))],[button("反诈查询","menu:antifraud",icon_custom_emoji_id=premium_icon("risk"),style="primary")]]
    elif cat=="ledger":
        on=bool(int(st.get("item_ledger_enabled") or 0)); unit=str(st.get("ledger_currency") or "USDT").upper(); rows=[[button(f"默认币种：{unit}","settings:currency",selected=True,icon_custom_emoji_id=premium_icon("finance"))],[button(("关闭" if on else "开启")+" 指定消费账本","setv2:toggle:item_ledger_enabled:ledger",selected=on,icon_custom_emoji_id=premium_icon("ledger"))],[button("查询账本","menu:ledger",icon_custom_emoji_id=premium_icon("ledger"),style="primary")],[button("账单 / 总账","menu:bill",icon_custom_emoji_id=premium_icon("finance"),style="primary")],[button("实时汇率","tool:rate",icon_custom_emoji_id=premium_icon("rate"),style="primary")]]
    rows.append([button("返回设置中心","menu:settings",icon_custom_emoji_id=premium_icon("back"),style="primary")]); return {"inline_keyboard":rows}


def _business_message(api: TelegramAPI, message: dict) -> None:
    # 消息保护是会员增值能力；基础 Business 记账/反诈不因会员到期而停止。
    if bool(message.get("_shuibei_protection_allowed", True)):
        try:
            cache_business_message(api, message)
        except Exception:
            pass
    bc_id = str(message.get("business_connection_id") or "")
    owner_id = business_owner_id(bc_id)
    if owner_id <= 0:
        return
    chat = message.get("chat") or {}
    chat_id = int(chat.get("id") or 0)
    sender = message.get("from") or {}
    sender_id = int(sender.get("id") or 0)
    text = str(message.get("text") or message.get("caption") or "").strip()

    if sender_id == owner_id:
        try:
            offline_mark_owner_active(owner_id)
        except Exception:
            pass
        peer_name = (str(chat.get("first_name") or "") + (" " + str(chat.get("last_name") or "") if chat.get("last_name") else "")).strip() or str(chat.get("title") or "") or str(chat_id)
        peer_username = str(chat.get("username") or "")
    else:
        peer_name = (str(sender.get("first_name") or "") + (" " + str(sender.get("last_name") or "") if sender.get("last_name") else "")).strip() or str(sender_id or chat_id)
        peer_username = str(sender.get("username") or "")
    if chat_id:
        try:
            set_last_active_business(owner_id, bc_id, chat_id, peer_name, peer_username)
        except Exception:
            pass
        try:
            touch_business_customer(owner_id, bc_id, chat_id, peer_name, peer_username, incoming=(sender_id != owner_id))
        except Exception:
            pass

    _fakebot_auto_alert(api, owner_id, chat_id, sender, message)
    _r29_welcome_sent = False
    if sender_id != owner_id:
        try:
            _r29_welcome_sent = _r29_maybe_send_welcome(api, owner_id, bc_id, chat_id, peer_name, peer_username)
        except Exception:
            _r29_welcome_sent = False
        _scam_auto_alert(api, owner_id, chat_id, sender)
        try:
            if (not _r29_welcome_sent) and offline_should_send(owner_id, chat_id):
                st = offline_get(owner_id)
                body = str(st.get("template_text") or "")[:3500]
                try:
                    result = api.send_message(chat_id, body, business_connection_id=bc_id, parse_mode="HTML")
                except Exception:
                    result = api.send_message(chat_id, re.sub(r"<[^>]+>", "", body), business_connection_id=bc_id, parse_mode=None)
                sent_mid = int((result or {}).get("message_id") or 0) if isinstance(result, dict) else 0
                offline_mark_sent(owner_id, chat_id, sent_mid)
        except Exception:
            pass
        if text:
            hit = _kw_match(owner_id, text)
            if hit:
                body = str(hit["reply_text"] or "")[:3500]
                try:
                    api.send_message(chat_id, body, business_connection_id=bc_id, parse_mode="HTML")
                except Exception:
                    try:
                        api.send_message(chat_id, re.sub(r"<[^>]+>", "", body), business_connection_id=bc_id, parse_mode=None)
                    except Exception:
                        pass
        return

    if text:
        # 与主机器人一致：老板在任意 Business 私聊发送完整触发关键词，自动发送快捷消息模板。
        qhit = quick_find_trigger(owner_id, text)
        if qhit and str(qhit.get("reply_text") or "").strip():
            try:
                api.send_message(chat_id, str(qhit.get("reply_text") or "")[:3000], business_connection_id=bc_id, parse_mode="HTML")
                if int(qhit.get("delete_trigger") or 0):
                    try:
                        api.delete_business_messages(bc_id, [int(message.get("message_id") or 0)])
                    except Exception:
                        pass
                return
            except Exception:
                pass
        name = peer_name or "未知"
        handled, reply, export = handle_ledger_text(owner_id, chat_id, name, text)
        if handled:
            if export is not None:
                try:
                    api.send_document_bytes(owner_id, f"shuibei_ledger_{owner_id}.txt", export.encode("utf-8"), "水杯记账账本导出")
                except Exception:
                    pass
            elif reply:
                try:
                    api.send_message(chat_id, reply, business_connection_id=bc_id, parse_mode="HTML")
                except Exception:
                    pass


def _callback(api: TelegramAPI, cq: dict) -> None:
    cid = str(cq.get("id") or "")
    user = cq.get("from") or {}
    owner_id = int(user.get("id") or 0)
    msg = cq.get("message") or {}
    chat_id = int(((msg.get("chat") or {}).get("id")) or owner_id)
    message_id = int(msg.get("message_id") or 0)
    data = str(cq.get("data") or "")
    if owner_id <= 0:
        return
    try:
        api.answer_callback(cid)
    except Exception:
        pass

    def edit(text, markup=None):
        if message_id and chat_id:
            try:
                api.edit_message_text(chat_id, message_id, text, markup)
                return
            except Exception:
                pass
        api.send_message(owner_id, text, reply_markup=markup)

    # 离开计算器页面时只清理 calculator 状态；快捷/离线输入状态保持原逻辑。
    try:
        _wait = wait_get(owner_id)
        if data != "tool:calc" and str((_wait or {}).get("kind") or "") == "calculator":
            wait_clear(owner_id)
    except Exception:
        pass

    if data == "onboarding:ack":
        # 标记失败最多导致下次再次展示欢迎页，不应阻断当前进入主页。
        try:
            onboarding_mark_seen(owner_id)
        except Exception:
            pass
        try:
            edit(_r29_home_text(owner_id), main_keyboard(owner_id, "home"))
        except Exception as exc:
            print(f"[ShuiBei] onboarding home render fallback: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            _send_start_plain_fallback(api, chat_id, first_time=False)
        return
    if data == "menu:home":
        try:
            edit(_r29_home_text(owner_id), main_keyboard(owner_id, "home"))
        except Exception as exc:
            print(f"[ShuiBei] home render fallback: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            _send_start_plain_fallback(api, chat_id, first_time=False)
        return
    if data.startswith("menu:section:"):
        sec = data.rsplit(":", 1)[1]
        if sec in {"msg", "finance", "other"}:
            edit(section_text(sec), main_keyboard(owner_id, sec)); return
    if data == "menu:antifraud" or data == "menu:scam":
        edit(_v21_antifraud_text(), {"inline_keyboard": [
            [button("诈骗记录查询", "antifraud:query", icon_custom_emoji_id=premium_icon("risk")),
             button("FakeBot 检测", "menu:fakebot", icon_custom_emoji_id=premium_icon("fakebot"))],
            [_v21_back_home()],
        ]}); return
    if data == "antifraud:query":
        wait_set(owner_id, "scam_query", {})
        edit("\n".join([_ui_title("risk", "诈骗记录查询", "🛡️"), "", "直接发送 Telegram 用户名或数字 ID。", "发送 <code>/cancel</code> 退出查询。"]), {"inline_keyboard": [[button("取消", "antifraud:cancel", icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data == "antifraud:cancel":
        wait_clear(owner_id); edit(_v21_antifraud_text(), {"inline_keyboard": [[button("诈骗记录查询", "antifraud:query", icon_custom_emoji_id=premium_icon("risk")), button("FakeBot 检测", "menu:fakebot", icon_custom_emoji_id=premium_icon("fakebot"))], [_v21_back_home()]]}); return
    if data == "menu:fakebot":
        st = get_settings(owner_id); on = bool(int(st.get("fakebot_detect_enabled", 1) or 0))
        edit("\n".join([_ui_title("fakebot", "FakeBot 检测", "🤖"), "", _ui_field("状态", "开启" if on else "关闭"), _ui_field("提醒位置", "仅机器人私信"), "", "Telegram 官方 fake/scam 标记只作为弱风险信号，不会单独触发严重处罚。"]), {"inline_keyboard": [[button(("关闭" if on else "开启") + " FakeBot 检测", "fakebot:toggle", selected=on, icon_custom_emoji_id=premium_icon("fakebot"))], [button("手动检测", "fakebot:query", icon_custom_emoji_id=premium_icon("preview"), style="primary")], [button("返回墨清反诈", "menu:antifraud", icon_custom_emoji_id=premium_icon("back"), style="primary")]]}); return
    if data == "fakebot:toggle":
        st = get_settings(owner_id); set_setting(owner_id, "fakebot_detect_enabled", 0 if int(st.get("fakebot_detect_enabled",1) or 0) else 1)
        st = get_settings(owner_id); on = bool(int(st.get("fakebot_detect_enabled",1) or 0))
        edit("\n".join([_ui_title("fakebot", "FakeBot 检测", "🤖"), "", _ui_field("状态", "开启" if on else "关闭"), _ui_field("提醒位置", "仅机器人私信"), "", "Telegram 官方 fake/scam 标记只作为弱风险信号，不会单独触发严重处罚。"]), {"inline_keyboard": [[button(("关闭" if on else "开启") + " FakeBot 检测", "fakebot:toggle", selected=on, icon_custom_emoji_id=premium_icon("fakebot"))], [button("手动检测", "fakebot:query", icon_custom_emoji_id=premium_icon("preview"), style="primary")], [button("返回墨清反诈", "menu:antifraud", icon_custom_emoji_id=premium_icon("back"), style="primary")]]}); return
    if data == "fakebot:query":
        wait_set(owner_id, "fakebot_query", {})
        edit("\n".join([_ui_title("fakebot", "FakeBot 检测", "🤖"), "", "直接发送机器人用户名，例如 <code>@example_bot</code>。", "发送 <code>/cancel</code> 退出检测。"]), {"inline_keyboard": [[button("取消", "menu:fakebot", icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data == "menu:ledger":
        edit(_v21_ledger_text(), {"inline_keyboard": [[button("账本统计", "menu:bill", icon_custom_emoji_id=premium_icon("finance"))], [_v21_back_home()]]}); return
    if data == "menu:bill":
        edit(bill_center_text(owner_id), _v21_bill_keyboard()); return
    if data.startswith("bill:"):
        mode = data.split(":", 1)[1]
        if mode in {"all", "debt", "prepay"}:
            edit(bill_list_text(owner_id, mode), {"inline_keyboard": [[button("返回账单中心", "menu:bill", icon_custom_emoji_id=premium_icon("back"))]]}); return
        if mode == "export":
            try:
                _send_export(api, owner_id, owner_id)
                api.send_message(owner_id, _ui_notice("safe", "总账已导出"))
            except Exception:
                api.send_message(owner_id, _ui_notice("unknown", "导出失败", "请稍后重试。"))
            return
    if data == "menu:quick":
        edit(_v21_quick_text(owner_id), _v21_quick_keyboard(owner_id)); return
    if data == "quick:add":
        wait_set(owner_id, "quick_new_title")
        edit("\n".join([_ui_title("messages", "设置快捷消息", "⚡"), "", _ui_field("第一步", "发送触发关键词；多个关键词可用逗号或换行分隔"), _ui_field("限制", "最多20个，单个最多80字"), _ui_field("取消", "发送 /cancel")]), {"inline_keyboard": [[button("取消", "quick:cancel", icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data == "quick:cancel":
        wait_clear(owner_id); edit(_v21_quick_text(owner_id), _v21_quick_keyboard(owner_id)); return
    if data.startswith("quick:view:"):
        try:
            rid = int(data.rsplit(":", 1)[1]); record = quick_get(owner_id, rid)
        except Exception:
            record = None
        edit(_v21_quick_detail_text(record), _v21_quick_detail_keyboard(record) if record else _v21_quick_keyboard(owner_id)); return
    if data.startswith("quick:preview:"):
        try: rid=int(data.rsplit(":",1)[1]); record=quick_get(owner_id,rid)
        except Exception: record=None
        if not record: edit(_ui_notice("unknown","快捷消息不存在"),_v21_quick_keyboard(owner_id)); return
        body=str(record.get("reply_text") or "")[:3000]
        try: api.send_message(owner_id,body,parse_mode="HTML")
        except Exception: api.send_message(owner_id,re.sub(r"<[^>]+>","",body),parse_mode=None)
        try: api.answer_callback(cid,"已发送预览")
        except Exception: pass
        return
    if data.startswith("quick:editreply:"):
        try: rid=int(data.rsplit(":",1)[1]); record=quick_get(owner_id,rid)
        except Exception: record=None; rid=0
        if not record: edit(_ui_notice("unknown","快捷消息不存在"),_v21_quick_keyboard(owner_id)); return
        wait_set(owner_id,"quick_edit_reply_only",{"record_id":rid}); edit(_ui_title("messages","修改快捷消息文本","⚡")+"\n\n发送新的回复正文（1-3000 字，支持 HTML 富文本）。\n发送 <code>/cancel</code> 取消。", {"inline_keyboard":[[button("取消",f"quick:view:{rid}",icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data.startswith("quick:editkw:"):
        try: rid=int(data.rsplit(":",1)[1]); record=quick_get(owner_id,rid)
        except Exception: record=None; rid=0
        if not record: edit(_ui_notice("unknown","快捷消息不存在"),_v21_quick_keyboard(owner_id)); return
        wait_set(owner_id,"quick_edit_keywords",{"record_id":rid}); edit("\n".join([_ui_title("messages","修改快捷消息触发词","⚡"),"",_ui_field("当前关键词",html.escape(" / ".join(record.get("keywords") or [])),trusted=True),"发送新的触发关键词；多个关键词可用逗号或换行分隔。","发送 <code>/cancel</code> 取消。"]), {"inline_keyboard":[[button("取消",f"quick:view:{rid}",icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data.startswith("quick:toggledel:"):
        try:
            rid = int(data.rsplit(":", 1)[1]); record = quick_get(owner_id, rid)
            if record:
                quick_set_delete_trigger(owner_id, rid, not bool(int(record.get("delete_trigger") or 0)))
            record = quick_get(owner_id, rid)
        except Exception:
            record = None
        edit(_v21_quick_detail_text(record), _v21_quick_detail_keyboard(record) if record else _v21_quick_keyboard(owner_id)); return
    if data.startswith("quick:edit:"):
        try: rid=int(data.rsplit(":",1)[1]); record=quick_get(owner_id,rid)
        except Exception: record=None; rid=0
        if not record: edit(_ui_notice("unknown","快捷消息不存在"),_v21_quick_keyboard(owner_id)); return
        wait_set(owner_id,"quick_edit_keywords",{"record_id":rid}); edit("\n".join([_ui_title("messages","修改快捷消息触发词","⚡"),"",_ui_field("当前关键词",html.escape(" / ".join(record.get("keywords") or [])),trusted=True),"发送新的触发关键词；多个关键词可用逗号或换行分隔。","发送 <code>/cancel</code> 取消。"]), {"inline_keyboard":[[button("取消",f"quick:view:{rid}",icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data.startswith("quick:delask:"):
        try:
            rid = int(data.rsplit(":", 1)[1]); record = quick_get(owner_id, rid)
        except Exception:
            record = None; rid = 0
        if not record:
            edit(_ui_notice("unknown", "快捷消息不存在"), _v21_quick_keyboard(owner_id)); return
        edit(_ui_title("messages", "确认删除快捷消息", "⚡") + "\n\n删除后无法恢复。", {"inline_keyboard": [[button("确认删除", f"quick:delete:{rid}", icon_custom_emoji_id=premium_icon("delete"), style="danger")], [button("取消", f"quick:view:{rid}", icon_custom_emoji_id=premium_icon("back"), style="primary")]]}); return
    if data.startswith("quick:delete:"):
        try:
            quick_delete(owner_id, int(data.rsplit(":", 1)[1]))
        except Exception:
            pass
        edit(_v21_quick_text(owner_id), _v21_quick_keyboard(owner_id)); return
    if data == "menu:offline":
        edit(_v21_offline_text(owner_id), _v21_offline_keyboard(owner_id)); return
    if data == "offline:toggle":
        st = offline_get(owner_id); offline_save(owner_id, enabled=not bool(int(st.get("enabled") or 0)))
        edit(_v21_offline_text(owner_id), _v21_offline_keyboard(owner_id)); return
    if data == "offline:toggle_skip_online":
        st=offline_get(owner_id); offline_save(owner_id,skip_when_online=not bool(int(st.get("skip_when_online",1) or 0)))
        edit(_v21_offline_text(owner_id),_v21_offline_keyboard(owner_id)); return
    if data == "offline:preview":
        st=offline_get(owner_id); body=str(st.get("template_text") or "")[:3500]
        try: api.send_message(owner_id,body,parse_mode="HTML")
        except Exception: api.send_message(owner_id,re.sub(r"<[^>]+>","",body),parse_mode=None)
        try: api.answer_callback(cid,"已发送预览")
        except Exception: pass
        return
    if data == "offline:interval_menu":
        edit(_ui_title("messages", "修改离线消息间隔", "📴") + "\n\n选择同一客户的最短触发间隔。", _v21_offline_interval_keyboard(owner_id)); return
    if data == "offline:template":
        wait_set(owner_id, "offline_template")
        edit("\n".join([_ui_title("messages", "修改离线消息", "📴"), "", _ui_field("输入内容", "直接发送新的离线消息模板（1-3500 字）"), _ui_field("取消", "发送 /cancel")]), {"inline_keyboard": [[button("取消", "offline:cancel", icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data == "offline:cancel":
        wait_clear(owner_id); edit(_v21_offline_text(owner_id), _v21_offline_keyboard(owner_id)); return
    if data.startswith("offline:interval:"):
        try:
            seconds = int(data.rsplit(":", 1)[1]); offline_save(owner_id, interval_seconds=seconds)
        except Exception:
            pass
        edit(_v21_offline_text(owner_id), _v21_offline_keyboard(owner_id)); return
    if data == "menu:sync":
        edit(sync_menu_text(owner_id), sync_keyboard(owner_id)); return
    if data == "sync:toggle":
        set_sync_enabled(owner_id, not sync_enabled(owner_id)); edit(sync_menu_text(owner_id), sync_keyboard(owner_id)); return
    if data == "sync:preview":
        text = "\n".join([
            _ui_title("preview", "同步预览", "🔍"), "",
            _ui_field("同步范围", "仅同步个人账本"),
            _ui_field("同步方式", "手动同步一次"),
            "",
            "确认后会同步一次个人账本。",
        ])
        edit(text, {"inline_keyboard": [[button("同步一次", "sync:confirm", icon_custom_emoji_id=premium_icon("sync"))], [button("返回", "menu:sync", icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data == "sync:confirm":
        if not sync_enabled(owner_id):
            edit(sync_menu_text(owner_id), sync_keyboard(owner_id)); return
        _head = premium_notice_banner("unknown", "确认同步个人账本")
        edit("\n".join([_head, "", _ui_field("同步范围", "仅同步个人账本"), "", "确认后同步一次。", _head]), {"inline_keyboard": [[button("确认同步一次", "sync:run", selected=True, icon_custom_emoji_id=premium_icon("sync"))], [button("取消", "menu:sync", icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data == "sync:run":
        try:
            result = sync_once(owner_id)
            added = int(result.get("new_from_main") or 0) + int(result.get("new_from_water") or 0)
            total = int(result.get("merged_total") or 0)
            _head = premium_notice_banner("safe", "个人账本同步完成")
            text = "\n".join([
                _head, "",
                _ui_field("新增记录", f"{added} 条"),
                _ui_field("当前个人账本", f"{total} 条"),
                "", _head,
            ])
        except SyncDisabled:
            text = _ui_notice("unknown", "账本同步已关闭")
        except MainLedgerUnavailable:
            text = _ui_notice("unknown", "账本暂不可用", "请稍后重试。")
        except Exception:
            text = _ui_notice("unknown", "同步失败", "请稍后重试。")
        edit(text, sync_keyboard(owner_id)); return
    if data == "menu:crypto":
        edit("\n".join([_ui_title("crypto", "链上工具", "⛓"), "", _ui_field("TRON / USDT", "发送 /usdt TRON地址"), _ui_field("TON", "发送 /ton TON地址或域名"), "", "链上查询只读，不会发起转账、授权或签名。"]), {"inline_keyboard": [[button("实时汇率", "tool:rate", icon_custom_emoji_id=premium_icon("rate"))], [_v21_back_home()]]}); return
    if data == "tool:rate":
        edit(_tool_result_html(exchange_rate_text(), "finance"), {"inline_keyboard": [[button("刷新", "tool:rate", icon_custom_emoji_id=premium_icon("refresh")), _v21_back_home()]]}); return
    if data == "tool:calc":
        wait_set(owner_id, "calculator", {})
        edit("\n".join([_ui_title("tools", "计算器", "🧮"), "", _ui_field("当前模式", "私聊计算器"), "直接发送算式即可：", "<code>1-1+1*2</code>", "<code>（2+3）*4</code>", "<code>10÷2</code>", "<code>2^3</code>", "", "发送 <code>/cancel</code> 退出计算器。"]), {"inline_keyboard": [[_v21_back_home()]]}); return
    if data == "menu:kw":
        edit(keyword_menu_text(owner_id), _keyword_menu_keyboard(owner_id)); return
    if data == "kw:add":
        wait_set(owner_id,"kw_new_keyword",{}); edit(_ui_title("keyword","设置关键词","💎")+"\n\n发送关键词（1-64 字）。\n发送 <code>/cancel</code> 取消。", {"inline_keyboard":[[button("取消","menu:kw",icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data == "kw:list":
        rows=_keyword_list(owner_id); edit(_ui_title("keyword","查看关键词","💎")+"\n\n"+(f"当前共有 <b>{len(rows)}</b> 条，点击下方关键词查看详情。" if rows else "暂无关键词回复。"),_keyword_list_keyboard(owner_id,"view")); return
    if data == "kw:choose_edit":
        rows=_keyword_list(owner_id); edit(_ui_title("keyword","更改关键词","💎")+"\n\n"+("选择要管理的关键词。" if rows else "暂无关键词回复。"),_keyword_list_keyboard(owner_id,"edit")); return
    if data == "kw:choose_delete":
        rows=_keyword_list(owner_id); edit(_ui_title("keyword","删除关键词","💎")+"\n\n"+("选择要删除的关键词。" if rows else "暂无关键词回复。"),_keyword_list_keyboard(owner_id,"delete")); return
    if data.startswith("kw:view:"):
        try: rid=int(data.rsplit(":",1)[1]); item=_keyword_get(owner_id,rid)
        except Exception: item=None
        edit(_keyword_detail_text(item),_keyword_detail_keyboard(item) if item else _keyword_menu_keyboard(owner_id)); return
    if data.startswith("kw:preview:"):
        try: rid=int(data.rsplit(":",1)[1]); item=_keyword_get(owner_id,rid)
        except Exception: item=None
        if not item: edit(_ui_notice("unknown","关键词不存在或已删除"),_keyword_menu_keyboard(owner_id)); return
        body=str(item.get("reply_text") or "")[:3500]
        try: api.send_message(owner_id,body,parse_mode="HTML")
        except Exception: api.send_message(owner_id,re.sub(r"<[^>]+>","",body),parse_mode=None)
        try: api.answer_callback(cid,"已发送预览")
        except Exception: pass
        return
    if data.startswith("kw:editreply:"):
        try: rid=int(data.rsplit(":",1)[1]); item=_keyword_get(owner_id,rid)
        except Exception: item=None; rid=0
        if not item: edit(_ui_notice("unknown","关键词不存在或已删除"),_keyword_menu_keyboard(owner_id)); return
        wait_set(owner_id,"kw_edit_reply",{"record_id":rid}); edit(_ui_title("keyword","修改回复","💎")+"\n\n发送新的回复正文（1-3500 字，支持 HTML 富文本）。\n发送 <code>/cancel</code> 取消。", {"inline_keyboard":[[button("取消",f"kw:view:{rid}",icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data.startswith("kw:editkw:"):
        try: rid=int(data.rsplit(":",1)[1]); item=_keyword_get(owner_id,rid)
        except Exception: item=None; rid=0
        if not item: edit(_ui_notice("unknown","关键词不存在或已删除"),_keyword_menu_keyboard(owner_id)); return
        wait_set(owner_id,"kw_edit_keyword",{"record_id":rid}); edit(_ui_title("keyword","修改关键词","💎")+f"\n\n当前关键词：<code>{html.escape(str(item.get('keyword') or ''))}</code>\n\n发送新的关键词（1-64 字）。\n发送 <code>/cancel</code> 取消。", {"inline_keyboard":[[button("取消",f"kw:view:{rid}",icon_custom_emoji_id=premium_icon("back"))]]}); return
    if data.startswith("kw:delask:"):
        try: rid=int(data.rsplit(":",1)[1]); item=_keyword_get(owner_id,rid)
        except Exception: item=None; rid=0
        if not item: edit(_ui_notice("unknown","关键词不存在或已删除"),_keyword_menu_keyboard(owner_id)); return
        edit(_ui_title("keyword","确认删除关键词","💎")+f"\n\n关键词：<code>{html.escape(str(item.get('keyword') or ''))}</code>\n\n删除后无法恢复。", {"inline_keyboard":[[button("确认删除",f"kw:delete:{rid}",icon_custom_emoji_id=premium_icon("delete"),style="danger")],[button("取消",f"kw:view:{rid}",icon_custom_emoji_id=premium_icon("back"),style="primary")]]}); return
    if data.startswith("kw:delete:"):
        try: _keyword_delete_id(owner_id,int(data.rsplit(":",1)[1]))
        except Exception: pass
        edit(keyword_menu_text(owner_id),_keyword_menu_keyboard(owner_id)); return
    if data == "menu:archive":
        edit(protection_text(owner_id), protection_keyboard(owner_id)); return
    if data == "protection:toggle":
        enabled = _protection_enabled(owner_id); set_protection_enabled(owner_id, not enabled)
        edit(protection_text(owner_id), protection_keyboard(owner_id)); return
    if data == "menu:settings":
        edit(settings_text(owner_id), settings_keyboard(owner_id)); return
    if data.startswith("setv2:cat:"):
        cat=data.rsplit(":",1)[1]
        if cat in {"common","message","security","ledger"}: edit(settings_category_text(owner_id,cat),settings_category_keyboard(owner_id,cat)); return
    if data.startswith("setv2:toggle:"):
        parts=data.split(":"); key=parts[2] if len(parts)>2 else ""; cat=parts[3] if len(parts)>3 else "common"
        if key=="protection": set_protection_enabled(owner_id,not _protection_enabled(owner_id))
        elif key in {"item_ledger_enabled","scam_detect_enabled","fakebot_detect_enabled","keyword_enabled"}:
            st=get_settings(owner_id); set_setting(owner_id,key,0 if int(st.get(key,0) or 0) else 1)
        edit(settings_category_text(owner_id,cat),settings_category_keyboard(owner_id,cat)); return
    if data.startswith("settings:toggle:"):
        key=data.rsplit(":",1)[1]; st=get_settings(owner_id)
        if key in {"item_ledger_enabled","scam_detect_enabled","fakebot_detect_enabled","keyword_enabled"}: set_setting(owner_id,key,0 if int(st.get(key,0) or 0) else 1)
        edit(settings_text(owner_id),settings_keyboard(owner_id)); return
    if data == "settings:currency":
        rows=[]; cur=[]; current=ledger_currency(owner_id).upper()
        for c in ALLOWED_CURRENCIES:
            cur.append(button(c,f"currency:{c}",selected=(c.upper()==current),icon_custom_emoji_id=premium_currency_icon(c)))
            if len(cur)==3: rows.append(cur); cur=[]
        if cur: rows.append(cur)
        rows.append([button("返回设置","menu:settings",icon_custom_emoji_id=premium_icon("back"))])
        edit("\n".join([_ui_title("finance","记账单位","💎"),"",_ui_field("当前单位",current),"","选择下方单位即可立即生效。"]),{"inline_keyboard":rows}); return
    if data.startswith("currency:"):
        try: set_currency(owner_id,data.split(":",1)[1])
        except Exception: pass
        edit(settings_text(owner_id),settings_keyboard(owner_id)); return


def _v21_business_connection_notice(api: TelegramAPI, bc: dict) -> None:
    owner = bc.get("user") or {}
    change = business_connection_upsert(
        str(bc.get("id") or ""), owner, bool(bc.get("is_enabled", True)), int(bc.get("user_chat_id") or 0),
    )
    if change.get("conflict"):
        print(f"[ShuiBei] blocked Business owner conflict for connection {str(bc.get('id') or '')[:32]}", flush=True)
        return
    if not change.get("changed"):
        return
    target = int(change.get("user_chat_id") or change.get("owner_id") or 0)
    if target <= 0:
        return
    if change.get("enabled"):
        header = premium_notice_banner("safe", "Business 自动化机器人已连接")
        text = "\n".join([
            header, "",
            _ui_field("机器人", "水杯记账"),
            _ui_field("已启用能力", "墨清反诈、FakeBot 检测、关键词回复、快捷消息、离线消息、防撤回 / 防编辑"),
            _ui_field("账本同步", "可在财务页手动同步个人账本"),
            "", header,
        ])
    else:
        header = premium_notice_banner("unknown", "Business 自动化机器人已取消")
        text = "\n".join([
            header, "",
            _ui_field("已暂停能力", "自动反诈、关键词回复、离线消息和 Business 消息保护"),
            "", "个人记账等非 Business 功能仍可继续使用。",
            "重新连接水杯记账后可继续使用 Business 自动化能力。", header,
        ])
    try:
        api.send_message(target, text)
    except Exception:
        pass


def process_update(api: TelegramAPI, upd: dict) -> None:
    if not isinstance(upd, dict):
        return
    if isinstance(upd.get("business_connection"), dict):
        _v21_business_connection_notice(api, upd["business_connection"]); return
    if isinstance(upd.get("deleted_business_messages"), dict):
        deleted = upd["deleted_business_messages"]
        if bool(deleted.get("_shuibei_protection_allowed", True)):
            handle_deleted_business_messages(api, deleted)
        return
    if isinstance(upd.get("edited_business_message"), dict):
        edited = upd["edited_business_message"]
        if bool(edited.get("_shuibei_protection_allowed", True)):
            handle_edited_business_message(api, edited)
        return
    if isinstance(upd.get("business_message"), dict):
        _business_message(api, upd["business_message"]); return
    if isinstance(upd.get("callback_query"), dict):
        _callback(api, upd["callback_query"]); return
    if isinstance(upd.get("message"), dict):
        _private_command(api, upd["message"]); return


def run(entitlement_provider=None) -> None:
    require_moqing_runtime(entitlement_provider)
    _runtime_clear_ready()
    _runtime_status("initializing_databases", detail="starting local schema checks")
    try:
        init_all()
        ensure_feature_schema()
        bootstrap_customer_index()
    except Exception as exc:
        _runtime_status("database_init_failed", detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        raise

    _runtime_status("telegram_getme", detail="verifying bot identity")
    api = TelegramAPI()
    try:
        me = api.get_me() or {}
    except Exception as exc:
        _runtime_status("telegram_getme_failed", detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        raise
    bot_id = int(me.get("id") or 0)
    actual_username = str(me.get("username") or "").strip()
    username = "@" + actual_username if actual_username else ""
    expected_username = str(BOT_USERNAME or "").strip().lstrip("@").lower()
    if expected_username and actual_username.lower() != expected_username:
        detail = f"Bot identity mismatch: expected @{expected_username}, got @{actual_username or '<none>'} bot_id={bot_id}"
        _runtime_status("bot_identity_mismatch", bot_id=bot_id, username=username, detail=detail)
        print(f"[ShuiBei] {detail}", flush=True)
        raise SystemExit(78)
    print(f"[ShuiBei] identity verified {username or BOT_USERNAME} bot_id={bot_id}", flush=True)

    _runtime_status("webhook_check", bot_id=bot_id, username=username)
    try:
        wh = api.get_webhook_info() or {}
        if str(wh.get("url") or "").strip():
            api.delete_webhook(drop_pending_updates=False)
            print("[ShuiBei] removed stale webhook; long polling enabled", flush=True)
    except TelegramAPIError as exc:
        _runtime_status("webhook_check_failed", bot_id=bot_id, username=username, detail=str(exc)[:300])
        print(f"[ShuiBei] webhook check warning: {exc}", flush=True)

    offset = load_offset(bot_id)
    try:
        save_offset(offset, bot_id)
    except Exception as exc:
        print(f"[ShuiBei] offset save warning: {type(exc).__name__}", flush=True)
    allowed = list(ALLOWED_UPDATE_TYPES)

    # Real readiness probe. The manager must not call this process healthy before getUpdates itself succeeds.
    _runtime_status("poll_probe", bot_id=bot_id, username=username, detail=f"offset={offset}")
    try:
        api.get_updates(offset, 0, allowed, limit=1)
    except TelegramAPIError as exc:
        if _is_poll_conflict(exc):
            _runtime_status(
                "poll_conflict",
                bot_id=bot_id,
                username=username,
                detail="another ShuiBei/getUpdates consumer is already using this bot token",
            )
            print("[ShuiBei] getUpdates conflict: another bot instance is already consuming updates", flush=True)
        else:
            _runtime_status("poll_probe_failed", bot_id=bot_id, username=username, detail=f"TelegramAPIError: {str(exc)[:300]}")
        raise
    except Exception as exc:
        _runtime_status("poll_probe_failed", bot_id=bot_id, username=username, detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        raise
    _runtime_status("ready", bot_id=bot_id, username=username, detail=f"offset={offset}", ready=True)
    print(f"[ShuiBei] READY {username or BOT_USERNAME} bot_id={bot_id} offset={offset}", flush=True)

    start_failures = {}
    last_heartbeat = 0.0
    poll_conflict_active = False
    while True:
        try:
            updates = api.get_updates(offset, POLL_TIMEOUT, allowed)
            updates = apply_access_control(api, updates)
            now_mono = time.monotonic()
            if poll_conflict_active:
                poll_conflict_active = False
                _runtime_status("ready", bot_id=bot_id, username=username, detail=f"offset={offset}; poll conflict recovered", ready=True)
                last_heartbeat = now_mono
                print("[ShuiBei] getUpdates conflict recovered; polling is active again", flush=True)
            elif now_mono - last_heartbeat >= 15.0:
                _runtime_status("ready", bot_id=bot_id, username=username, detail=f"offset={offset}", ready=True)
                last_heartbeat = now_mono
            for upd in updates:
                uid = int(upd.get("update_id") or 0)
                is_start = _is_start_update(upd)
                processed_ok = False
                try:
                    process_update(api, upd)
                    processed_ok = True
                    start_failures.pop(uid, None)
                except Exception as exc:
                    print(f"[ShuiBei] internal error: {type(exc).__name__}", flush=True)
                    if is_start:
                        n = int(start_failures.get(uid, 0)) + 1
                        start_failures[uid] = n
                        _runtime_status("start_handler_failed", bot_id=bot_id, username=username, detail=f"update_id={uid} attempt={n} {type(exc).__name__}: {str(exc)[:220]}")
                        # A /start response is safe to retry. Do not acknowledge it on the first two failures.
                        if n < 3:
                            print(f"[ShuiBei] /start update {uid} failed attempt={n}; keeping offset for retry", flush=True)
                            time.sleep(1.0)
                            break
                        print(f"[ShuiBei] /start update {uid} failed 3 times; advancing to avoid poison queue", flush=True)
                    else:
                        _runtime_status("update_handler_failed", bot_id=bot_id, username=username, detail=f"update_id={uid} {type(exc).__name__}: {str(exc)[:220]}")
                # Old behavior for non-/start remains at-most-once; /start advances only after success or bounded retries.
                if processed_ok or not is_start or int(start_failures.get(uid, 0)) >= 3:
                    offset = max(offset, uid + 1)
                    save_offset(offset, bot_id)
                else:
                    break
        except KeyboardInterrupt:
            raise
        except TelegramAPIError as exc:
            if _is_poll_conflict(exc):
                poll_conflict_active = True
                _runtime_status(
                    "poll_conflict",
                    bot_id=bot_id,
                    username=username,
                    detail="another ShuiBei/getUpdates consumer is already using this bot token",
                )
                print("[ShuiBei] getUpdates conflict: another bot instance is consuming updates", flush=True)
            else:
                _runtime_status("poll_api_error", bot_id=bot_id, username=username, detail=str(exc)[:300])
                print(f"[ShuiBei] Telegram API error: {exc}", flush=True)
            time.sleep(POLL_RETRY_SECONDS)
        except Exception as exc:
            _runtime_status("poll_error", bot_id=bot_id, username=username, detail=f"{type(exc).__name__}: {str(exc)[:300]}")
            print(f"[ShuiBei] internal error: {type(exc).__name__}", flush=True)
            time.sleep(POLL_RETRY_SECONDS)

# ================== v21：水杯记账统一体验层结束 ==================

# ================== r29：轻量商户客户账本 / 客户中心 ==================
# 2026-09-16。账本金额仍只以 ledger.db 为真值；本层只增加客户索引、轻量 CRM 与 UI。
from customers import (
    touch_business_customer, list_customers, customer_detail, set_customer_meta,
    mark_collection_reminded, merchant_summary, bootstrap_customer_index,
)
from features import (
    welcome_get, welcome_save, welcome_claim, welcome_commit, welcome_release, welcome_render,
)
from ledger import get_balance as _r29_get_balance, micro_to_str as _r29_micro_to_str

_SHUIBEI_RUNTIME_BUILD = "r30-20260918-membership-privacy"


def _r29_money(owner_id: int, amount_micro: int) -> str:
    return f"{_r29_micro_to_str(int(amount_micro or 0))}{ledger_currency(int(owner_id))}"


def _r29_time(ts: int) -> str:
    if int(ts or 0) <= 0:
        return "暂无记录"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return "暂无记录"


def _r29_home_text(owner_id: int) -> str:
    try:
        sm = merchant_summary(int(owner_id))
    except Exception:
        sm = {"customer_count":0,"debt_count":0,"debt_amount_micro":0,"prepay_count":0,"prepay_amount_micro":0,"recent_count":0}
    return "\n".join([
        _ui_title("home", "水杯记账", "🥤"), "",
        _ui_field("待收", f"{_r29_money(owner_id, int(sm.get('debt_amount_micro') or 0))} · {int(sm.get('debt_count') or 0)} 位"),
        _ui_field("预付款", f"{_r29_money(owner_id, int(sm.get('prepay_amount_micro') or 0))} · {int(sm.get('prepay_count') or 0)} 位"),
        _ui_field("客户", f"{int(sm.get('customer_count') or 0)} 位 · 近 7 天活跃 {int(sm.get('recent_count') or 0)} 位"),
        "",
        "客户往来点「客户」，记账与报表点「财务」。",
    ])


def _plain_home_keyboard() -> dict:
    return {"inline_keyboard": [
        [
            {"text":"首页","callback_data":"menu:home"},
            {"text":"客户","callback_data":"menu:section:customers"},
            {"text":"财务","callback_data":"menu:section:finance"},
            {"text":"消息","callback_data":"menu:section:msg"},
        ],
        [{"text":"客户中心","callback_data":"menu:section:customers"},{"text":"记账","callback_data":"menu:ledger"}],
        [{"text":"快捷消息","callback_data":"menu:quick"},{"text":"设置","callback_data":"menu:settings"}],
    ]}


def _v21_panel_tabs(active: str):
    tabs = [
        ("home", "首页", "menu:home", "home"),
        ("customers", "客户", "menu:section:customers", "user"),
        ("finance", "财务", "menu:section:finance", "finance"),
        ("msg", "消息", "menu:section:msg", "messages"),
    ]
    return [button(t, d, selected=(k == active), icon_custom_emoji_id=premium_icon(i)) for k, t, d, i in tabs]


def _v21_panel_items(owner_id: int, active: str):
    st = get_settings(owner_id)
    protect_on = bool(int(st.get("anti_revoke_enabled", 0) or 0) and int(st.get("anti_edit_enabled", 0) or 0))
    sync_on = bool(int(st.get("sync_enabled", 0) or 0))
    offline_on = bool(int(offline_get(owner_id).get("enabled") or 0))
    welcome_on = bool(int(welcome_get(owner_id).get("enabled") or 0))
    if active == "customers":
        return [
            [button("全部客户", "customer:list:all", icon_custom_emoji_id=premium_icon("user"), style="primary"),
             button("欠款客户", "customer:list:debt", icon_custom_emoji_id=premium_icon("finance"), style="danger")],
            [button("预付款客户", "customer:list:prepay", icon_custom_emoji_id=premium_icon("finance"), style="success"),
             button("最近活跃", "customer:list:recent", icon_custom_emoji_id=premium_icon("refresh"), style="primary")],
            [button("久未联系", "customer:list:inactive", icon_custom_emoji_id=premium_icon("notification"), style="primary")],
        ]
    if active == "finance":
        return [
            [button("记账", "menu:ledger", icon_custom_emoji_id=premium_icon("ledger")),
             button("账单中心", "menu:bill", icon_custom_emoji_id=premium_icon("finance"))],
            [button("账本同步", "menu:sync", selected=sync_on, icon_custom_emoji_id=premium_icon("sync")),
             button("实时汇率", "tool:rate", icon_custom_emoji_id=premium_icon("rate"))],
            [button("计算器", "tool:calc", icon_custom_emoji_id=premium_icon("tools")),
             button("链上工具", "menu:crypto", icon_custom_emoji_id=premium_icon("crypto"))],
        ]
    if active == "msg":
        return [
            [button("欢迎消息", "menu:welcome", selected=welcome_on, icon_custom_emoji_id=premium_icon("messages")),
             button("快捷消息", "menu:quick", icon_custom_emoji_id=premium_icon("messages"))],
            [button("关键词回复", "menu:kw", icon_custom_emoji_id=premium_icon("keyword")),
             button("离线消息", "menu:offline", selected=offline_on, icon_custom_emoji_id=premium_icon("messages"))],
            [button("防撤回 / 防编辑", "menu:archive", selected=protect_on, icon_custom_emoji_id=premium_icon("protection"))],
        ]
    return [
        [button("客户中心", "menu:section:customers", icon_custom_emoji_id=premium_icon("user"), style="primary"),
         button("快速记账", "menu:ledger", icon_custom_emoji_id=premium_icon("ledger"), style="primary")],
        [button("快捷消息", "menu:quick", icon_custom_emoji_id=premium_icon("messages"), style="primary"),
         button("安全工具", "menu:antifraud", icon_custom_emoji_id=premium_icon("risk"), style="primary")],
        [button("设置", "menu:settings", icon_custom_emoji_id=premium_icon("settings"), style="primary")],
    ]


def main_keyboard(owner_id: int, active: str = "home"):
    active = active if active in {"home", "customers", "finance", "msg"} else "home"
    return {"inline_keyboard": [_v21_panel_tabs(active), *_v21_panel_items(owner_id, active)]}


def section_text(section: str, owner_id: int = 0) -> str:
    if section == "customers":
        return _r29_customer_center_text(int(owner_id))
    if section == "finance":
        return "\n".join([
            _ui_title("finance", "财务", "💰"), "",
            _ui_field("账务", "记账、账单、日 / 周 / 月报"),
            _ui_field("同步", "按需同步个人账本"),
            _ui_field("工具", "汇率、计算器、链上查询"),
        ])
    if section == "msg":
        return "\n".join([
            _ui_title("messages", "消息", "💬"), "",
            _ui_field("自动回复", "欢迎消息、关键词、离线消息"),
            _ui_field("快捷消息", "在 Business 会话快速发送模板"),
            _ui_field("消息保护", "防撤回 / 防编辑提醒"),
        ])
    return _r29_home_text(int(owner_id))


def _r29_customer_center_text(owner_id: int) -> str:
    sm=merchant_summary(int(owner_id))
    return "\n".join([_ui_title("user", "客户中心", "👤"), "",
        _ui_field("全部客户", f"{sm['customer_count']} 位"),
        _ui_field("欠款", f"{sm['debt_count']} 位 · {_r29_money(owner_id,sm['debt_amount_micro'])}"),
        _ui_field("预付款", f"{sm['prepay_count']} 位 · {_r29_money(owner_id,sm['prepay_amount_micro'])}"),
        _ui_field("近 7 天活跃", f"{sm['recent_count']} 位"), "",
        "统一查看客户账务、联系情况、标签和备注。"])


def _r29_customer_list_text(owner_id: int, mode: str) -> str:
    rows=list_customers(owner_id,mode,limit=30)
    title={"all":"全部客户","debt":"欠款客户","prepay":"预付款客户","recent":"最近活跃","inactive":"久未联系"}.get(mode,"客户")
    lines=[_ui_title("user",title,"👤"),""]
    if not rows: lines.append("暂无符合条件的客户。")
    for r in rows[:20]:
        bal=int(r.get("balance_micro") or 0)
        if bal<0: state=f"欠 {_r29_money(owner_id,abs(bal))}"
        elif bal>0: state=f"预付 {_r29_money(owner_id,bal)}"
        else: state="已结清"
        uname=("@"+str(r.get("username"))) if r.get("username") else ""
        lines.append(f"• <b>{html.escape(str(r.get('name') or '客户'))}</b>{' · '+html.escape(uname) if uname else ''}｜{html.escape(state)}｜{html.escape(_r29_time(int(r.get('last_contact_at') or 0)))}")
    return "\n".join(lines)


def _r29_customer_list_keyboard(owner_id: int, mode: str):
    rows=[]
    for r in list_customers(owner_id,mode,limit=20):
        pid=int(r.get("peer_id") or 0); bal=int(r.get("balance_micro") or 0)

        suffix=("欠 "+_r29_money(owner_id,abs(bal))) if bal<0 else (("预付 "+_r29_money(owner_id,bal)) if bal>0 else "已结清")
        rows.append([button(f"{str(r.get('name') or '客户')[:28]} · {suffix}",f"customer:detail:{pid}",icon_custom_emoji_id=premium_icon("user"),style=("danger" if bal<0 else "primary"))])
    rows.append([button("返回客户中心","menu:section:customers",icon_custom_emoji_id=premium_icon("back"),style="primary")])
    return {"inline_keyboard":rows}


def _r29_customer_detail_text(owner_id: int, peer_id: int) -> str:
    d=customer_detail(owner_id,peer_id)
    if not d: return _ui_notice("unknown","客户不存在","没有找到这个客户。")
    bal=int(d.get("balance_micro") or 0)
    state=("客户欠款 "+_r29_money(owner_id,abs(bal))) if bal<0 else (("客户预付款 "+_r29_money(owner_id,bal)) if bal>0 else "账目已结清")
    lines=[_ui_title("user","客户档案","👤"),"",
        _ui_field("客户",str(d.get("name") or "客户")),
        _ui_field("用户名",("@"+str(d.get("username"))) if d.get("username") else "-"),
        _ui_field("Telegram ID",str(int(d.get("peer_id") or 0))),
        _ui_field("账务",state),
        _ui_field("最后联系",_r29_time(int(d.get("last_contact_at") or 0))),
        _ui_field("标签",str(d.get("label") or "未设置")),
        _ui_field("内部备注",str(d.get("note") or "未设置")),
        _ui_field("已催款",f"{int(d.get('remind_count') or 0)} 次")]
    hist=list(d.get("recent_ledger") or [])
    if hist:
        lines += ["","<b>最近流水</b>"]
        for r in hist[:5]:
            lines.append(f"• {html.escape(str(r.get('action') or ''))} {_r29_money(owner_id,abs(int(r.get('amount_micro') or 0)))}｜余额 {_r29_money(owner_id,int(r.get('balance_micro') or 0))}｜{html.escape(str(r.get('remark') or ''))}")
    return "\n".join(lines)


def _r29_customer_detail_keyboard(owner_id: int, peer_id: int):
    d=customer_detail(owner_id,peer_id) or {}; bal=int(d.get("balance_micro") or 0)
    rows=[[button("设置标签",f"customer:label:{peer_id}",icon_custom_emoji_id=premium_icon("edit"),style="primary"),
           button("内部备注",f"customer:note:{peer_id}",icon_custom_emoji_id=premium_icon("edit"),style="primary")]]
    if bal<0 and d.get("has_business"):
        rows.append([button("催款",f"customer:remindask:{peer_id}",icon_custom_emoji_id=premium_icon("notification"),style="danger")])
    rows += [[button("查看账本",f"customer:ledger:{peer_id}",icon_custom_emoji_id=premium_icon("ledger"),style="primary")],
             [button("返回客户中心","menu:section:customers",icon_custom_emoji_id=premium_icon("back"),style="primary")]]
    return {"inline_keyboard":rows}


def _r29_bill_customer_keyboard(owner_id: int, mode: str):
    rows=[]
    for r in list_customers(owner_id,mode if mode in {"debt","prepay"} else "all",limit=15):
        if mode=="all" and int(r.get("balance_micro") or 0)==0: continue
        rows.append([button(str(r.get("name") or "客户")[:40],f"customer:detail:{int(r.get('peer_id') or 0)}",icon_custom_emoji_id=premium_icon("user"),style="primary")])
    rows.append([button("返回账单中心","menu:bill",icon_custom_emoji_id=premium_icon("back"),style="primary")])
    return {"inline_keyboard":rows}


def _r29_active_business_connection(owner_id: int, connection_id: str) -> bool:
    conn=connect(APP_DB_PATH)
    try:
        return conn.execute("SELECT 1 FROM business_connections WHERE connection_id=? AND owner_id=? AND is_enabled=1 LIMIT 1",(str(connection_id or ""),int(owner_id))).fetchone() is not None
    finally: conn.close()


def _r29_maybe_send_welcome(api: TelegramAPI, owner_id: int, bc_id: str, peer_id: int, peer_name: str, peer_username: str) -> bool:
    if not _r29_active_business_connection(owner_id,bc_id): return False
    st=welcome_get(owner_id)
    if not int(st.get("enabled") or 0) or not str(st.get("template_text") or "").strip(): return False
    claimed_at=int(time.time())
    if not welcome_claim(owner_id,peer_id,claimed_at): return False
    try:
        bal=_r29_get_balance(owner_id,peer_id)
        text=welcome_render(str(st.get("template_text") or ""),{"name":peer_name or "客户","username":str(peer_username or "").lstrip("@"),"balance":_r29_money(owner_id,bal)})
        api.send_message(peer_id,text,business_connection_id=bc_id,parse_mode="HTML")
        welcome_commit(owner_id,peer_id,claimed_at)
        return True
    except Exception:
        try: welcome_release(owner_id,peer_id,claimed_at)
        except Exception: pass
        return False


def _r29_send_reminder(api: TelegramAPI, owner_id: int, peer_id: int) -> tuple[bool,str]:
    d=customer_detail(owner_id,peer_id)
    if not d or int(d.get("balance_micro") or 0)>=0: return False,"该客户当前没有欠款。"
    cid=str(d.get("connection_id") or "")
    if not cid or not _r29_active_business_connection(owner_id,cid): return False,"当前 Business 会话不可用。"
    amount=_r29_money(owner_id,abs(int(d.get("balance_micro") or 0)))
    body=f"您好 {html.escape(str(d.get('name') or ''))}，您当前还有 <b>{html.escape(amount)}</b> 待结清，请及时处理，谢谢。"
    try:
        api.send_message(int(peer_id),body,business_connection_id=cid,parse_mode="HTML")
    except Exception:
        return False,"催款发送失败，请稍后重试。"
    try: mark_collection_reminded(owner_id,peer_id)
    except Exception: pass
    return True,"催款提醒已发送。"


def _r29_welcome_menu_text(owner_id: int) -> str:
    st=welcome_get(owner_id); cooldown=int(st.get("cooldown_seconds") or 0)
    rule="仅首次联系" if cooldown==0 else f"每 {max(1,cooldown//3600)} 小时最多一次"
    return "\n".join([_ui_title("messages","欢迎消息","💬"),"",
        _ui_field("状态","已开启" if int(st.get("enabled") or 0) else "已关闭"),
        _ui_field("触发规则",rule),"","<b>当前模板</b>",f"<pre>{html.escape(str(st.get('template_text') or '尚未设置'))}</pre>","","变量：<code>{name}</code> / <code>{username}</code> / <code>{balance}</code>"])


def _r29_welcome_menu_kb(owner_id: int):
    st=welcome_get(owner_id); on=bool(int(st.get("enabled") or 0))
    return {"inline_keyboard":[
        [button("关闭欢迎消息" if on else "开启欢迎消息","welcome:toggle",selected=on,icon_custom_emoji_id=premium_icon("messages"),style=("danger" if on else "success"))],
        [button("修改模板","welcome:edit",icon_custom_emoji_id=premium_icon("edit"),style="primary"),button("触发间隔","welcome:cooldown_menu",icon_custom_emoji_id=premium_icon("refresh"),style="primary")],
        [button("返回消息","menu:section:msg",icon_custom_emoji_id=premium_icon("back"),style="primary")]]}


def _r29_welcome_cooldown_kb(owner_id: int):
    cur=int(welcome_get(owner_id).get("cooldown_seconds") or 0)
    opts=[("仅首次",0),("1小时",3600),("24小时",86400),("7天",604800)]
    rows=[]
    for i in range(0,len(opts),2):
        row=[]
        for label,sec in opts[i:i+2]: row.append(button(label,f"welcome:cooldown:{sec}",selected=(cur==sec),icon_custom_emoji_id=premium_icon("refresh"),style="primary"))
        rows.append(row)
    rows.append([button("返回欢迎消息","menu:welcome",icon_custom_emoji_id=premium_icon("back"),style="primary")]); return {"inline_keyboard":rows}


_R29_WAIT_BEFORE = _v21_wait_input
def _v21_wait_input(api: TelegramAPI, owner_id: int, chat_id: int, text: str) -> bool:
    state=wait_get(owner_id)
    kind=str((state or {}).get("kind") or ""); payload=(state or {}).get("payload") or {}
    if kind in {"customer_label_r29","customer_note_r29","welcome_edit_r29"}:
        if str(text or "").strip().lower() in {"/cancel","取消"}:
            wait_clear(owner_id); api.send_message(chat_id,_ui_notice("safe","已取消")); return True
        if kind=="welcome_edit_r29":
            body=str(text or "").strip()
            if not body or len(body)>3500: api.send_message(chat_id,_ui_notice("unknown","模板格式错误","请输入 1-3500 字。")); return True
            old=welcome_get(owner_id); welcome_save(owner_id,enabled=(True if payload.get("enable_after") else bool(int(old.get("enabled") or 0))),template_text=body)
            wait_clear(owner_id); api.send_message(chat_id,_r29_welcome_menu_text(owner_id),reply_markup=_r29_welcome_menu_kb(owner_id)); return True
        peer=int(payload.get("peer_id") or 0)
        try:
            if kind=="customer_label_r29": set_customer_meta(owner_id,peer,label=str(text or "").strip())
            else: set_customer_meta(owner_id,peer,note=str(text or "").strip())
        except Exception as exc:
            api.send_message(chat_id,_ui_notice("unknown","保存失败",str(exc))); return True
        wait_clear(owner_id); api.send_message(chat_id,_r29_customer_detail_text(owner_id,peer),reply_markup=_r29_customer_detail_keyboard(owner_id,peer)); return True
    return _R29_WAIT_BEFORE(api,owner_id,chat_id,text)


_R29_CALLBACK_BEFORE = _callback
def _callback(api: TelegramAPI, cq: dict) -> None:
    data=str(cq.get("data") or ""); user=cq.get("from") or {}; owner_id=int(user.get("id") or 0); msg=cq.get("message") or {}; chat_id=int(((msg.get("chat") or {}).get("id")) or owner_id); mid=int(msg.get("message_id") or 0); cid=str(cq.get("id") or "")
    if not owner_id: return
    def edit(text,markup=None):
        if mid and chat_id:
            try: api.edit_message_text(chat_id,mid,text,markup); return
            except Exception: pass
        api.send_message(owner_id,text,reply_markup=markup)
    if data.startswith("customer:") or data in {"menu:section:customers","menu:welcome"} or data.startswith("welcome:") or data in {"bill:all","bill:debt","bill:prepay"}:
        try: api.answer_callback(cid)
        except Exception: pass
        if data=="menu:section:customers": edit(_r29_customer_center_text(owner_id),main_keyboard(owner_id,"customers")); return
        if data.startswith("customer:list:"):
            mode=data.rsplit(":",1)[1]; edit(_r29_customer_list_text(owner_id,mode),_r29_customer_list_keyboard(owner_id,mode)); return
        if data.startswith("customer:detail:"):
            peer=int(data.rsplit(":",1)[1]); edit(_r29_customer_detail_text(owner_id,peer),_r29_customer_detail_keyboard(owner_id,peer)); return
        if data.startswith("customer:ledger:"):
            peer=int(data.rsplit(":",1)[1]); edit(query_text(owner_id,peer),_r29_customer_detail_keyboard(owner_id,peer)); return
        if data.startswith("customer:label:"):
            peer=int(data.rsplit(":",1)[1]); wait_set(owner_id,"customer_label_r29",{"peer_id":peer}); edit("发送新的客户标签（最多 40 字）；发送 <code>/cancel</code> 取消。",{"inline_keyboard":[[button("取消",f"customer:detail:{peer}",icon_custom_emoji_id=premium_icon("back"))]]}); return
        if data.startswith("customer:note:"):
            peer=int(data.rsplit(":",1)[1]); wait_set(owner_id,"customer_note_r29",{"peer_id":peer}); edit("发送内部备注（最多 1000 字，仅你自己看到）；发送 <code>/cancel</code> 取消。",{"inline_keyboard":[[button("取消",f"customer:detail:{peer}",icon_custom_emoji_id=premium_icon("back"))]]}); return
        if data.startswith("customer:remindask:"):
            peer=int(data.rsplit(":",1)[1]); d=customer_detail(owner_id,peer)
            if not d or int(d.get("balance_micro") or 0)>=0: edit(_ui_notice("unknown","无需催款","该客户当前没有欠款。"),_r29_customer_detail_keyboard(owner_id,peer)); return
            amount=_r29_money(owner_id,abs(int(d.get("balance_micro") or 0)))
            edit(f"<b>确认催款？</b>\n\n客户：{html.escape(str(d.get('name') or '客户'))}\n待收：<b>{html.escape(amount)}</b>\n\n确认后会立即通过当前 Telegram Business 会话发送。",{"inline_keyboard":[[button("确认催款",f"customer:remind:{peer}",icon_custom_emoji_id=premium_icon("notification"),style="danger")],[button("取消",f"customer:detail:{peer}",icon_custom_emoji_id=premium_icon("back"),style="primary")]]}); return
        if data.startswith("customer:remind:"):
            peer=int(data.rsplit(":",1)[1]); ok,txt=_r29_send_reminder(api,owner_id,peer); edit(_r29_customer_detail_text(owner_id,peer)+"\n\n"+html.escape(txt),_r29_customer_detail_keyboard(owner_id,peer)); return
        if data=="menu:welcome": edit(_r29_welcome_menu_text(owner_id),_r29_welcome_menu_kb(owner_id)); return
        if data=="welcome:toggle":
            st=welcome_get(owner_id)
            if int(st.get("enabled") or 0): welcome_save(owner_id,enabled=False); edit(_r29_welcome_menu_text(owner_id),_r29_welcome_menu_kb(owner_id)); return
            if not str(st.get("template_text") or "").strip(): wait_set(owner_id,"welcome_edit_r29",{"enable_after":True}); edit("发送欢迎消息模板。变量：<code>{name}</code> / <code>{username}</code> / <code>{balance}</code>\n发送 <code>/cancel</code> 取消。",{"inline_keyboard":[[button("取消","menu:welcome",icon_custom_emoji_id=premium_icon("back"))]]}); return
            welcome_save(owner_id,enabled=True); edit(_r29_welcome_menu_text(owner_id),_r29_welcome_menu_kb(owner_id)); return
        if data=="welcome:edit": wait_set(owner_id,"welcome_edit_r29",{}); edit("发送新的欢迎消息模板（1-3500 字）。\n变量：<code>{name}</code> / <code>{username}</code> / <code>{balance}</code>\n发送 <code>/cancel</code> 取消。",{"inline_keyboard":[[button("取消","menu:welcome",icon_custom_emoji_id=premium_icon("back"))]]}); return
        if data=="welcome:cooldown_menu": edit("<b>欢迎消息触发间隔</b>\n\n“仅首次”表示每位客户只发送一次。",_r29_welcome_cooldown_kb(owner_id)); return
        if data.startswith("welcome:cooldown:"):
            sec=max(0,min(365*86400,int(data.rsplit(":",1)[1]))); welcome_save(owner_id,cooldown_seconds=sec); edit(_r29_welcome_menu_text(owner_id),_r29_welcome_menu_kb(owner_id)); return
        if data in {"bill:all","bill:debt","bill:prepay"}:
            mode=data.split(":",1)[1]; edit(bill_list_text(owner_id,mode),_r29_bill_customer_keyboard(owner_id,mode)); return
    return _R29_CALLBACK_BEFORE(api,cq)

# ================== r29 结束 ==================

# ================== r31：应收到期 / 收款结清 / 对账单 / 经营报表 / 精简 Mini App 入口 ==================
from datetime import datetime as _r31_datetime, timedelta as _r31_timedelta
from ledger import parse_amount_to_micro as _r31_parse_amount
from receivables import (
    get_due as _r31_get_due,
    set_due as _r31_set_due,
    decorate_customer as _r31_decorate_customer,
    due_customers as _r31_due_customers,
    settle_customer as _r31_settle_customer,
    statement_html as _r31_statement_html,
    report_html as _r31_report_html,
    report_range_html as _r33_report_range_html,
)

_SHUIBEI_RUNTIME_BUILD = "r35-20260927-security"
def _r31_webapp_button(text: str = "打开精简 Mini App") -> dict:
    # Production launch location is resolved only by the private MoQing adapter.
    return {"text": str(text), "web_app": {"url": current_gateway().miniapp_url()}}



def _r31_due_label(owner_id: int, peer_id: int, balance_micro: int) -> str:
    if int(balance_micro or 0) >= 0:
        return "无需设置"
    meta = _r31_get_due(owner_id, peer_id)
    due = int(meta.get("due_at") or 0)
    if due <= 0:
        return "未设置"
    day = time.strftime("%Y-%m-%d", time.localtime(due))
    if str(meta.get("status") or "open") != "open":
        return day + " · 已结束"
    now = int(time.time())
    if due < now:
        days = max(1, (now - due + 86399) // 86400)
        return f"{day} · 已逾期 {days} 天"
    left = max(0, (due - now + 86399) // 86400)
    return f"{day} · 剩 {left} 天"


_R31_CUSTOMER_DETAIL_TEXT_BEFORE = _r29_customer_detail_text
def _r29_customer_detail_text(owner_id: int, peer_id: int) -> str:
    d = customer_detail(owner_id, peer_id)
    if not d:
        return _ui_notice("unknown", "客户不存在", "没有找到这个客户。")
    bal = int(d.get("balance_micro") or 0)
    if bal < 0:
        state = "待收 " + _r29_money(owner_id, abs(bal))
    elif bal > 0:
        state = "预付款 " + _r29_money(owner_id, bal)
    else:
        state = "已结清"
    identity = ("@" + str(d.get("username"))) if d.get("username") else f"ID {int(d.get('peer_id') or 0)}"
    due_line = _r31_due_label(owner_id, peer_id, bal)
    lines = [
        _ui_title("user", str(d.get("name") or "客户"), "👤"), "",
        _ui_field("账务", state),
        _ui_field("到期", due_line),
        _ui_field("联系", f"{identity} · {_r29_time(int(d.get('last_contact_at') or 0))}"),
    ]
    label = str(d.get("label") or "").strip()
    note = str(d.get("note") or "").strip()
    if label:
        lines.append(_ui_field("标签", label))
    if note:
        lines.append(_ui_field("备注", note))
    hist = list(d.get("recent_ledger") or [])
    if hist:
        lines += ["", "<b>最近流水</b>"]
        for r in hist[:3]:
            action = html.escape(str(r.get("action") or ""))
            remark = html.escape(str(r.get("remark") or "").strip())
            tail = f" · {remark}" if remark else ""
            lines.append(f"• {action} {_r29_money(owner_id, abs(int(r.get('amount_micro') or 0)))}{tail}")
    return "\n".join(lines)


_R31_CUSTOMER_DETAIL_KB_BEFORE = _r29_customer_detail_keyboard
def _r29_customer_detail_keyboard(owner_id: int, peer_id: int):
    d = customer_detail(owner_id, peer_id) or {}
    bal = int(d.get("balance_micro") or 0)
    has_business = bool(d.get("has_business"))
    rows = []
    if bal < 0:
        rows.append([
            button("收款 / 结清", f"customer:payask:{peer_id}", icon_custom_emoji_id=premium_icon("finance"), style="success"),
            button("设置到期", f"customer:due_menu:{peer_id}", icon_custom_emoji_id=premium_icon("refresh"), style="primary"),
        ])
        second = [button("对账单", f"customer:statementask:{peer_id}", icon_custom_emoji_id=premium_icon("ledger"), style="primary")]
        if has_business:
            second.append(button("催款", f"customer:remindask:{peer_id}", icon_custom_emoji_id=premium_icon("notification"), style="danger"))
        rows.append(second)
    rows.append([
        button("查看流水", f"customer:ledger:{peer_id}", icon_custom_emoji_id=premium_icon("ledger"), style="primary"),
        button("设置标签", f"customer:label:{peer_id}", icon_custom_emoji_id=premium_icon("edit"), style="primary"),
    ])
    rows.append([
        button("内部备注", f"customer:note:{peer_id}", icon_custom_emoji_id=premium_icon("edit"), style="primary"),
        button("返回客户", "menu:section:customers", icon_custom_emoji_id=premium_icon("back"), style="primary"),
    ])
    return {"inline_keyboard": rows}


_R31_PANEL_ITEMS_BEFORE = _v21_panel_items
def _v21_panel_items(owner_id: int, active: str):
    st = get_settings(owner_id)
    protect_on = bool(int(st.get("anti_revoke_enabled", 0) or 0) and int(st.get("anti_edit_enabled", 0) or 0))
    sync_on = bool(int(st.get("sync_enabled", 0) or 0))
    offline_on = bool(int(offline_get(owner_id).get("enabled") or 0))
    welcome_on = bool(int(welcome_get(owner_id).get("enabled") or 0))
    if active == "customers":
        return [
            [button("全部客户", "customer:list:all", icon_custom_emoji_id=premium_icon("user"), style="primary"),
             button("待收", "customer:list:debt", icon_custom_emoji_id=premium_icon("finance"), style="danger")],
            [button("今日到期", "customer:list:today", icon_custom_emoji_id=premium_icon("notification"), style="primary"),
             button("已逾期", "customer:list:overdue", icon_custom_emoji_id=premium_icon("notification"), style="danger")],
            [button("预付款", "customer:list:prepay", icon_custom_emoji_id=premium_icon("finance"), style="success"),
             button("最近活跃", "customer:list:recent", icon_custom_emoji_id=premium_icon("refresh"), style="primary")],
            [button("久未联系", "customer:list:inactive", icon_custom_emoji_id=premium_icon("notification"), style="primary")],
        ]
    if active == "finance":
        return [
            [button("快速记账", "menu:ledger", icon_custom_emoji_id=premium_icon("ledger"), style="primary"),
             button("账单", "menu:bill", icon_custom_emoji_id=premium_icon("finance"), style="primary")],
            [button("经营报表", "report:menu", icon_custom_emoji_id=premium_icon("finance"), style="primary"),
             button("账本同步", "menu:sync", selected=sync_on, icon_custom_emoji_id=premium_icon("sync"))],
            [button("汇率", "tool:rate", icon_custom_emoji_id=premium_icon("rate")),
             button("计算器", "tool:calc", icon_custom_emoji_id=premium_icon("tools"))],
            [button("链上工具", "menu:crypto", icon_custom_emoji_id=premium_icon("crypto"))],
        ]
    if active == "msg":
        return [
            [button("欢迎消息", "menu:welcome", selected=welcome_on, icon_custom_emoji_id=premium_icon("messages")),
             button("快捷消息", "menu:quick", icon_custom_emoji_id=premium_icon("messages"))],
            [button("关键词回复", "menu:kw", icon_custom_emoji_id=premium_icon("keyword")),
             button("离线消息", "menu:offline", selected=offline_on, icon_custom_emoji_id=premium_icon("messages"))],
            [button("消息保护", "menu:archive", selected=protect_on, icon_custom_emoji_id=premium_icon("protection"))],
        ]
    return [
        [_r31_webapp_button("打开 Mini App")],
        [button("快速记账", "menu:ledger", icon_custom_emoji_id=premium_icon("ledger"), style="primary"),
         button("账单 / 报表", "menu:section:finance", icon_custom_emoji_id=premium_icon("finance"), style="primary")],
        [button("反诈查询", "menu:antifraud", icon_custom_emoji_id=premium_icon("risk"), style="primary"),
         button("设置", "menu:settings", icon_custom_emoji_id=premium_icon("settings"), style="primary")],
    ]


_R31_CUSTOMER_LIST_TEXT_BEFORE = _r29_customer_list_text
def _r29_customer_list_text(owner_id: int, mode: str) -> str:
    if mode not in {"today", "overdue", "week", "due"}:
        return _R31_CUSTOMER_LIST_TEXT_BEFORE(owner_id, mode)
    rows = _r31_due_customers(owner_id, mode, limit=30)
    title = {"today": "今日到期", "overdue": "已逾期", "week": "7天内到期", "due": "已设置到期"}[mode]
    lines = [_ui_title("user", title, "👤"), ""]
    if not rows:
        lines.append("暂无符合条件的客户。")
        return "\n".join(lines)
    for r in rows[:20]:
        bal = int(r.get("balance_micro") or 0)
        due = int(r.get("due_at") or 0)
        due_s = time.strftime("%Y-%m-%d", time.localtime(due)) if due else "-"
        state = f"欠 {_r29_money(owner_id, abs(bal))}"
        if r.get("overdue"):
            state += " · 已逾期"
        lines.append(
            f"• <b>{html.escape(str(r.get('name') or '客户'))}</b>｜{html.escape(state)}｜到期 {html.escape(due_s)}"
        )
    return "\n".join(lines)


_R31_CUSTOMER_LIST_KB_BEFORE = _r29_customer_list_keyboard
def _r29_customer_list_keyboard(owner_id: int, mode: str):
    if mode not in {"today", "overdue", "week", "due"}:
        return _R31_CUSTOMER_LIST_KB_BEFORE(owner_id, mode)
    rows = []
    for r in _r31_due_customers(owner_id, mode, limit=20):
        pid = int(r.get("peer_id") or 0)
        bal = abs(int(r.get("balance_micro") or 0))
        rows.append([button(
            f"{str(r.get('name') or '客户')[:26]} · 欠 {_r29_money(owner_id, bal)}",
            f"customer:detail:{pid}",
            icon_custom_emoji_id=premium_icon("user"),
            style="danger" if r.get("overdue") else "primary",
        )])
    rows.append([button("返回客户中心", "menu:section:customers", icon_custom_emoji_id=premium_icon("back"), style="primary")])
    return {"inline_keyboard": rows}


def _r31_report_menu_kb(owner_id: int):
    return {"inline_keyboard": [
        [
            button("今日", "report:day", icon_custom_emoji_id=premium_icon("finance"), style="primary"),
            button("本周", "report:week", icon_custom_emoji_id=premium_icon("finance"), style="primary"),
            button("本月", "report:month", icon_custom_emoji_id=premium_icon("finance"), style="primary"),
        ],
        [button("自定义时间", "report:custom", icon_custom_emoji_id=premium_icon("refresh"), style="primary")],
        [button("返回财务", "menu:section:finance", icon_custom_emoji_id=premium_icon("back"), style="primary")],
    ]}


def _r31_due_menu_kb(peer_id: int):
    return {"inline_keyboard": [
        [
            button("今天", f"customer:due:0:{peer_id}", style="primary"),
            button("3天", f"customer:due:3:{peer_id}", style="primary"),
            button("7天", f"customer:due:7:{peer_id}", style="primary"),
        ],
        [
            button("30天", f"customer:due:30:{peer_id}", style="primary"),
            button("自定义", f"customer:due_custom:{peer_id}", style="primary"),
        ],
        [button("清除到期日", f"customer:due_clear:{peer_id}", style="danger")],
        [button("返回客户", f"customer:detail:{peer_id}", icon_custom_emoji_id=premium_icon("back"), style="primary")],
    ]}


_R31_WAIT_BEFORE = _v21_wait_input
def _v21_wait_input(api: TelegramAPI, owner_id: int, chat_id: int, text: str) -> bool:
    state = wait_get(owner_id)
    kind = str((state or {}).get("kind") or "")
    payload = (state or {}).get("payload") or {}
    if kind in {"r31_due_custom", "r31_partial", "r33_report_custom"}:
        if str(text or "").strip().lower() in {"/cancel", "取消"}:
            wait_clear(owner_id)
            api.send_message(chat_id, _ui_notice("safe", "已取消"))
            return True
        try:
            if kind == "r33_report_custom":
                raw = str(text or "").strip()
                parts = [x.strip() for x in re.split(r"(?:~|至|到)", raw) if x.strip()]
                if len(parts) != 2:
                    raise ValueError("请输入两个日期")
                start_dt = _r31_datetime.strptime(parts[0], "%Y-%m-%d")
                end_dt = _r31_datetime.strptime(parts[1], "%Y-%m-%d")
                start_ts = int(_r31_datetime(start_dt.year, start_dt.month, start_dt.day, 0, 0, 0).timestamp())
                end_ts = int(_r31_datetime(end_dt.year, end_dt.month, end_dt.day, 23, 59, 59).timestamp())
                body = _r33_report_range_html(owner_id, start_ts, end_ts)
                wait_clear(owner_id)
                api.send_message(chat_id, body, reply_markup=_r31_report_menu_kb(owner_id))
                return True
            peer = int(payload.get("peer_id") or 0)
            if kind == "r31_due_custom":
                raw = str(text or "").strip()
                dt = _r31_datetime.strptime(raw, "%Y-%m-%d")
                due_at = int(_r31_datetime(dt.year, dt.month, dt.day, 23, 59, 59).timestamp())
                _r31_set_due(owner_id, peer, due_at)
                wait_clear(owner_id)
                api.send_message(chat_id, _r29_customer_detail_text(owner_id, peer), reply_markup=_r29_customer_detail_keyboard(owner_id, peer))
                return True
            amount = _r31_parse_amount(str(text or "").strip())
            operation_key = str(payload.get("settlement_key") or "")
            if not operation_key:
                operation_key = "botwait:" + os.urandom(16).hex()
                payload["settlement_key"] = operation_key
                wait_set(owner_id, kind, payload)
            result = _r31_settle_customer(owner_id, peer, mode="partial", amount_micro=amount, idempotency_key=operation_key)
            wait_clear(owner_id)
            api.send_message(
                chat_id,
                _r29_customer_detail_text(owner_id, peer) + "\n\n" +
                _ui_notice("safe", "部分收款已记账", f"本次 {_r29_money(owner_id, int(result['amount_micro']))}"),
                reply_markup=_r29_customer_detail_keyboard(owner_id, peer),
            )
            return True
        except Exception:
            if kind == "r31_due_custom":
                hint = "请输入 YYYY-MM-DD，例如 2026-09-30。"
            elif kind == "r33_report_custom":
                hint = "请输入 YYYY-MM-DD ~ YYYY-MM-DD，例如 2026-09-01 ~ 2026-09-26。"
            else:
                hint = "请输入收款金额，例如 100 或 25.5。"
            api.send_message(chat_id, _ui_notice("unknown", "输入无效", hint))
            return True
    return _R31_WAIT_BEFORE(api, owner_id, chat_id, text)


_R31_CALLBACK_BEFORE = _callback
def _callback(api: TelegramAPI, cq: dict) -> None:
    data = str(cq.get("data") or "")
    user = cq.get("from") or {}
    owner_id = int(user.get("id") or 0)
    msg = cq.get("message") or {}
    chat_id = int(((msg.get("chat") or {}).get("id")) or owner_id)
    mid = int(msg.get("message_id") or 0)
    cid = str(cq.get("id") or "")
    if not owner_id:
        return

    def edit(text, markup=None):
        if mid and chat_id:
            try:
                api.edit_message_text(chat_id, mid, text, markup)
                return
            except Exception:
                pass
        api.send_message(owner_id, text, reply_markup=markup)

    r31_hit = (
        data.startswith("customer:payask:")
        or data.startswith("customer:settle:")
        or data.startswith("customer:settle_partial:")
        or data.startswith("customer:waiveask:")
        or data.startswith("customer:due_menu:")
        or data.startswith("customer:due:")
        or data.startswith("customer:due_custom:")
        or data.startswith("customer:due_clear:")
        or data.startswith("customer:statementask:")
        or data.startswith("customer:statement:")
        or data.startswith("report:")
        or data in {"customer:list:today", "customer:list:overdue", "customer:list:week", "customer:list:due"}
    )
    if not r31_hit:
        return _R31_CALLBACK_BEFORE(api, cq)
    try:
        api.answer_callback(cid)
    except Exception:
        pass

    try:
        if data.startswith("customer:list:"):
            mode = data.rsplit(":", 1)[1]
            edit(_r29_customer_list_text(owner_id, mode), _r29_customer_list_keyboard(owner_id, mode))
            return
        if data == "report:menu":
            edit(_r31_report_html(owner_id, "day"), _r31_report_menu_kb(owner_id))
            return
        if data == "report:custom":
            wait_set(owner_id, "r33_report_custom", {})
            edit(
                "<b>自定义报表时间</b>\n\n"
                "发送日期范围：<code>YYYY-MM-DD ~ YYYY-MM-DD</code>\n"
                "例如 <code>2026-09-01 ~ 2026-09-26</code>。\n"
                "发送 <code>/cancel</code> 取消。"
            )
            return
        if data.startswith("report:"):
            mode = data.split(":", 1)[1]
            if mode not in {"day", "week", "month"}:
                mode = "day"
            edit(_r31_report_html(owner_id, mode), _r31_report_menu_kb(owner_id))
            return
        if data.startswith("customer:due_menu:"):
            peer = int(data.rsplit(":", 1)[1])
            edit("<b>设置应收到期日</b>\n\n选择一个时间，或自定义日期。", _r31_due_menu_kb(peer))
            return
        if data.startswith("customer:due_custom:"):
            peer = int(data.rsplit(":", 1)[1])
            wait_set(owner_id, "r31_due_custom", {"peer_id": peer})
            edit("发送到期日期，格式：<code>YYYY-MM-DD</code>\n例如 <code>2026-09-30</code>。\n发送 <code>/cancel</code> 取消。")
            return
        if data.startswith("customer:due_clear:"):
            peer = int(data.rsplit(":", 1)[1])
            _r31_set_due(owner_id, peer, 0)
            edit(_r29_customer_detail_text(owner_id, peer), _r29_customer_detail_keyboard(owner_id, peer))
            return
        if data.startswith("customer:due:"):
            _, _, days_s, peer_s = data.split(":", 3)
            peer = int(peer_s)
            days = max(0, min(3650, int(days_s)))
            now_dt = _r31_datetime.now()
            target = now_dt + _r31_timedelta(days=days)
            due_at = int(_r31_datetime(target.year, target.month, target.day, 23, 59, 59).timestamp())
            _r31_set_due(owner_id, peer, due_at)
            edit(_r29_customer_detail_text(owner_id, peer), _r29_customer_detail_keyboard(owner_id, peer))
            return
        if data.startswith("customer:payask:"):
            peer = int(data.rsplit(":", 1)[1])
            d = customer_detail(owner_id, peer)
            if not d or int(d.get("balance_micro") or 0) >= 0:
                edit(_ui_notice("unknown", "无需收款", "该客户当前没有欠款。"), _r29_customer_detail_keyboard(owner_id, peer))
                return
            amount = _r29_money(owner_id, abs(int(d.get("balance_micro") or 0)))
            edit(
                f"<b>收款 / 结清</b>\n\n客户：{html.escape(str(d.get('name') or '客户'))}\n当前待收：<b>{html.escape(amount)}</b>",
                {"inline_keyboard": [
                    [button("收到全款", f"customer:settle:full:{peer}", style="success")],
                    [button("收到部分款", f"customer:settle_partial:{peer}", style="primary")],
                    [button("免除欠款", f"customer:waiveask:{peer}", style="danger")],
                    [button("返回客户", f"customer:detail:{peer}", icon_custom_emoji_id=premium_icon("back"), style="primary")],
                ]},
            )
            return
        if data.startswith("customer:settle_partial:"):
            peer = int(data.rsplit(":", 1)[1])
            wait_set(owner_id, "r31_partial", {"peer_id": peer, "settlement_key": "botwait:" + os.urandom(16).hex()})
            edit("发送本次收到的金额，例如 <code>100</code> 或 <code>25.5</code>。\n发送 <code>/cancel</code> 取消。")
            return
        if data.startswith("customer:waiveask:"):
            peer = int(data.rsplit(":", 1)[1])
            d = customer_detail(owner_id, peer)
            amount = abs(int((d or {}).get("balance_micro") or 0))
            edit(
                "<b>确认免除欠款？</b>\n\n"
                + f"金额：<b>{html.escape(_r29_money(owner_id, amount))}</b>\n"
                + "这会把当前欠款直接归零，并记录为“减免”，不会记成真实收款。",
                {"inline_keyboard": [
                    [button("确认免除", f"customer:settle:waive:{peer}", style="danger")],
                    [button("取消", f"customer:detail:{peer}", icon_custom_emoji_id=premium_icon("back"), style="primary")],
                ]},
            )
            return
        if data.startswith("customer:settle:"):
            parts = data.split(":")
            mode = parts[2]
            peer = int(parts[3])
            result = _r31_settle_customer(owner_id, peer, mode=mode, idempotency_key="botcb:" + cid)
            title = "已收到全款" if mode == "full" else "欠款已减免"
            edit(
                _r29_customer_detail_text(owner_id, peer) + "\n\n" +
                _ui_notice("safe", title, f"处理金额 {_r29_money(owner_id, int(result['amount_micro']))}"),
                _r29_customer_detail_keyboard(owner_id, peer),
            )
            return
        if data.startswith("customer:statementask:"):
            peer = int(data.rsplit(":", 1)[1])
            d = customer_detail(owner_id, peer) or {}
            body = _r31_statement_html(owner_id, peer)
            rows = []
            if d.get("has_business"):
                rows.append([button("发送给客户", f"customer:statement:{peer}", style="success")])
            rows.append([button("返回客户", f"customer:detail:{peer}", icon_custom_emoji_id=premium_icon("back"), style="primary")])
            edit(body + "\n\n<i>这是发送前预览。</i>", {"inline_keyboard": rows})
            return
        if data.startswith("customer:statement:"):
            peer = int(data.rsplit(":", 1)[1])
            d = customer_detail(owner_id, peer)
            if not d:
                edit(_ui_notice("unknown", "客户不存在"))
                return
            connection_id = str(d.get("connection_id") or "")
            if not connection_id or not _r29_active_business_connection(owner_id, connection_id):
                edit(_ui_notice("unknown", "无法发送", "当前 Telegram Business 会话不可用。"), _r29_customer_detail_keyboard(owner_id, peer))
                return
            api.send_message(int(peer), _r31_statement_html(owner_id, peer), business_connection_id=connection_id, parse_mode="HTML")
            edit(_r29_customer_detail_text(owner_id, peer) + "\n\n" + _ui_notice("safe", "对账单已发送"), _r29_customer_detail_keyboard(owner_id, peer))
            return
    except ValueError as exc:
        edit(_ui_notice("unknown", "操作失败", str(exc)[:180]))
        return
    except Exception:
        edit(_ui_notice("unknown", "操作失败", "系统暂时无法完成这个操作，请稍后重试。"))
        return

    return _R31_CALLBACK_BEFORE(api, cq)

# ================== r31 结束 ==================
