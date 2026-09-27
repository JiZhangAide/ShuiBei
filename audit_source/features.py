# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import html
import json
import re
import time
import threading
from decimal import Decimal

from config import APP_DB_PATH
from db import connect, tx
from emoji_knowledge import notice_banner, notice_field, notice_green, notice_yellow, notice_red, system_icon

SECURITY_RISK = "risk"
SECURITY_SAFE = "safe"
SECURITY_UNKNOWN = "unknown"

TARGET_RE = re.compile(r"^(?:@[A-Za-z0-9_]{3,32}|[A-Za-z][A-Za-z0-9_]{2,31}|\d{4,20})$")
CALC_RE = re.compile(r"^[0-9\.\s+\-*/×÷%＾^()（）xX＋－＊／％]+$")
WAIT_STATE_TTL_SECONDS = 300  # 与主机器人 PANEL_WAIT_TTL 保持一致，避免旧输入状态长期劫持后续私聊。
_FEATURE_SCHEMA_LOCK = threading.RLock()
_FEATURE_SCHEMA_READY = False


def ensure_feature_schema() -> None:
    global _FEATURE_SCHEMA_READY
    if _FEATURE_SCHEMA_READY:
        return
    with _FEATURE_SCHEMA_LOCK:
        if _FEATURE_SCHEMA_READY:
            return
        now = int(time.time())
        with tx(APP_DB_PATH, immediate=True) as conn:
            # business_connections 是旧表，只做兼容增列，不重建、不清数据。
            cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(business_connections)").fetchall()}
            if cols and "user_chat_id" not in cols:
                conn.execute("ALTER TABLE business_connections ADD COLUMN user_chat_id INTEGER NOT NULL DEFAULT 0")

            conn.executescript("""
            -- Feature schema must also be safe when invoked before db.init_all().
            -- Normal startup still calls init_all first, but private handlers/tests should not
            -- depend on an unrelated initializer having created this bookkeeping table.
            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_onboarding (
                owner_id INTEGER PRIMARY KEY,
                seen_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS quick_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                reply_text TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(owner_id, title)
            );
            CREATE INDEX IF NOT EXISTS idx_quick_replies_owner ON quick_replies(owner_id,id);

            -- r27: 快捷消息与主机器人语义统一；旧 title 仍保留作兼容显示/唯一键。

            CREATE TABLE IF NOT EXISTS offline_reply_settings (
                owner_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                template_text TEXT NOT NULL DEFAULT '您好，我现在暂时无法及时回复，看到消息后会尽快联系您。',
                interval_seconds INTEGER NOT NULL DEFAULT 3600,
                skip_when_online INTEGER NOT NULL DEFAULT 1,
                last_owner_activity_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS offline_reply_log (
                owner_id INTEGER NOT NULL,
                peer_id INTEGER NOT NULL,
                last_sent_at INTEGER NOT NULL DEFAULT 0,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(owner_id,peer_id)
            );

            CREATE TABLE IF NOT EXISTS last_active_business (
                owner_id INTEGER PRIMARY KEY,
                connection_id TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL DEFAULT 0,
                peer_name TEXT NOT NULL DEFAULT '',
                peer_username TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ui_wait_state (
                owner_id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL
            );
            """)
            qcols = {str(r[1]) for r in conn.execute("PRAGMA table_info(quick_replies)").fetchall()}
            if "keywords_json" not in qcols:
                conn.execute("ALTER TABLE quick_replies ADD COLUMN keywords_json TEXT NOT NULL DEFAULT ''")
            if "delete_trigger" not in qcols:
                conn.execute("ALTER TABLE quick_replies ADD COLUMN delete_trigger INTEGER NOT NULL DEFAULT 0")

            ocols = {str(r[1]) for r in conn.execute("PRAGMA table_info(offline_reply_settings)").fetchall()}
            if "skip_when_online" not in ocols:
                conn.execute("ALTER TABLE offline_reply_settings ADD COLUMN skip_when_online INTEGER NOT NULL DEFAULT 1")
            if "last_owner_activity_at" not in ocols:
                conn.execute("ALTER TABLE offline_reply_settings ADD COLUMN last_owner_activity_at INTEGER NOT NULL DEFAULT 0")

            conn.execute(
                "INSERT INTO runtime_state(key,value,updated_at) VALUES('feature_schema','v23',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (now,),
            )
        _FEATURE_SCHEMA_READY = True

def onboarding_seen(owner_id: int) -> bool:
    ensure_feature_schema()
    conn = connect(APP_DB_PATH)
    try:
        return conn.execute("SELECT 1 FROM user_onboarding WHERE owner_id=?", (int(owner_id),)).fetchone() is not None
    finally:
        conn.close()


def onboarding_mark_seen(owner_id: int) -> None:
    ensure_feature_schema()
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "INSERT INTO user_onboarding(owner_id,seen_at) VALUES(?,?) "
            "ON CONFLICT(owner_id) DO UPDATE SET seen_at=excluded.seen_at",
            (int(owner_id), int(time.time())),
        )


def welcome_text() -> str:
    return (
        f"{system_icon('messages','👋🏻')} 您好，我是<b>水杯记账</b>，很高兴认识您！\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "<b>水杯记账是墨清记账的精简版本。</b>\n"
        "如果您追求极致简约，可以一直使用本机器人；如果您希望体验更多功能和可玩性，推荐使用 "
        "<a href=\"https://t.me/JiZhangAide_bot\">墨清记账 @JiZhangAide_bot</a>。\n\n"
        "<b>使用水杯记账，您将获得以下能力：</b>\n\n"
        f" {system_icon('risk','🛡️')} 墨清反诈检测：直接发送 Telegram 用户名或数字 ID 即可查询诈骗记录\n\n"
        f" {system_icon('protection','🕘')} Business 文本防编辑 / 防撤回：会员功能，消息文本加密并仅短期缓存\n\n"
        f" {system_icon('ledger','📒')} 您能想到的高频记账能力：入账、出账、清账、撤销、总账、指定消费与导出\n\n"
        f" {system_icon('messages','💬')} 快捷消息 / 离线消息：减少重复客服操作\n\n"
        f" {system_icon('tools','🧰')} 计算器 / 实时汇率 / 常用链上查询\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "水杯只保留高频能力，常用功能的使用方式与墨清记账保持一致。"
    )


def home_text() -> str:
    return (
        f"{system_icon('home','🥤')} <b>水杯记账</b>\n\n"
        "墨清记账的精简版本，保留记账、墨清反诈、Business 消息保护和高频工具。\n\n"
        f"{system_icon('risk','🛡️')} 直接发送 <code>@username</code> 或数字 ID：查询诈骗记录\n"
        f"{system_icon('ledger','📒')} 发送 <code>+100</code> / <code>-20 午饭</code>：快速记账\n"
        f"{system_icon('tools','🧮')} 发送 <code>1-1+1*2</code>：计算器\n"
        f"{system_icon('finance','💰')} 发送 <code>100RMB</code> / <code>10TRX</code> / <code>2TON</code>：汇率换算\n\n"
        "需要完整功能时可使用 @JiZhangAide_bot。"
    )


def security_banner(state: str) -> str:
    st = str(state or "").lower()
    if st == SECURITY_RISK:
        return notice_banner("risk", "墨清交易风险提醒")
    if st == SECURITY_SAFE:
        return notice_banner("safe", "墨清安全检测结果")
    return notice_banner("unknown", "墨清风险状态暂时无法确认")


def security_label(state: str) -> str:
    st = str(state or "").lower()
    if st == SECURITY_RISK:
        return f"{notice_red()} 高风险"
    if st == SECURITY_SAFE:
        return f"{notice_green()} 未命中已知风险"
    return f"{notice_yellow()} 暂时无法确认风险"


def security_notice_html(state: str, title: str, fields=None, reasons=None, advice: str = "", source_note: str = "", extra_html_lines=None) -> str:
    """与主机器人 r20 moqing_security_notice_html 同一结构。

    fields 支持 (label, value) / (label, value, trusted_html)。用户输入默认转义。
    """
    st = state if state in {SECURITY_RISK, SECURITY_SAFE, SECURITY_UNKNOWN} else SECURITY_UNKNOWN
    banner = security_banner(st)
    lines = [banner, f"<b>{html.escape(str(title or '风险检测'))}</b>", ""]
    lines.append(f"{notice_field()}风险状态：{security_label(st)}")
    for item in fields or []:
        try:
            if len(item) >= 3:
                label, value, trusted = item[0], item[1], bool(item[2])
            else:
                label, value, trusted = item[0], item[1], False
            val = str(value if value is not None else "-")
            if not trusted:
                val = html.escape(val)
            lines.append(f"{notice_field()}{html.escape(str(label))}：{val}")
        except Exception:
            continue
    clean = [str(x or "").strip() for x in (reasons or []) if str(x or "").strip()]
    if clean:
        lines.extend(["", "<b>风险依据</b>"])
        lines.extend("• " + html.escape(x) for x in clean[:10])
    for raw in extra_html_lines or []:
        if str(raw or "").strip():
            lines.append(str(raw))
    if advice:
        lines.extend(["", "<b>建议</b>", html.escape(str(advice))])
    if source_note:
        lines.extend(["", f"<i>{html.escape(str(source_note))}</i>"])
    lines.append(banner)
    return "\n".join(lines)


def scam_result_html(target: str, available: bool, rows: list[dict]) -> str:
    """只展示 scam.py 从 config.SCAM_DB_PATH/scam_records 返回的真实结果，不做任何派生风险。"""
    t = str(target or "").strip()
    if not available:
        return security_notice_html(
            SECURITY_UNKNOWN,
            "诈骗记录查询",
            fields=[("查询目标", t)],
            reasons=["当前暂时无法完成诈骗记录查询"],
            advice="请稍后重试；在查询恢复前，请不要把本次结果当作安全结论。",
        )
    if not rows:
        return security_notice_html(
            SECURITY_SAFE,
            "诈骗记录查询",
            fields=[("查询目标", t), ("查询结果", "未发现已收录诈骗记录")],
            # 未命中时绝不生成“风险依据/已验证记录列表”。
            advice="未命中不等于绝对安全，重要交易仍建议独立核实身份。",
        )
    evidence = ["", "<b>已验证记录</b>"]
    for idx, row in enumerate(rows[:10], 1):
        stamp = html.escape(str((row or {}).get("time") or "未知"))
        link = str((row or {}).get("link") or "").strip()
        channel = html.escape(str((row or {}).get("channel") or ""))
        line = f"{idx}. {stamp}"
        if channel:
            line += f" · {channel}"
        evidence.append(line)
        if link:
            evidence.append(html.escape(link))
    if len(rows) > 10:
        evidence.append(f"其余 {len(rows) - 10} 条未展开。")
    return security_notice_html(
        SECURITY_RISK,
        "已验证诈骗风险",
        fields=[("查询目标", t), ("已收录记录", f"{len(rows)} 条")],
        reasons=["墨清反诈已验证存在相关风险记录"],
        extra_html_lines=evidence,
        advice="为了保障资金安全，请谨慎交易，并通过独立可信渠道再次核实身份。",
    )


def fakebot_result_html(target: str, hit: dict | None, official: bool = False, available: bool = True) -> str:
    t = str(target or "").strip().lstrip("@")
    if not available:
        return security_notice_html(
            SECURITY_UNKNOWN, "FakeBot / 高仿机器人检测",
            fields=[("机器人", ("@" + t) if t else "未提供")],
            reasons=["当前暂时无法完成高仿机器人检测"],
            advice="请稍后重试；在检测恢复前，请不要把本次结果当作安全结论。",
        )
    if official:
        return security_notice_html(
            SECURITY_SAFE, "FakeBot / 高仿机器人检测",
            fields=[("机器人", "@" + t), ("检测结果", "已登记正版机器人")],
            advice="请仍从可信官方入口打开机器人，避免点击陌生转发或仿冒链接。",
        )
    if not hit:
        return security_notice_html(
            SECURITY_SAFE, "FakeBot / 高仿机器人检测",
            fields=[("机器人", "@" + t), ("检测结果", "未命中当前高仿规则")],
            advice="未命中不代表绝对安全，请同时核对用户名、简介、官方入口和交易地址。",
        )
    return security_notice_html(
        SECURITY_RISK, "FakeBot / 高仿机器人风险",
        fields=[
            ("疑似高仿机器人", "@" + str(hit.get("suspect") or t)),
            ("已登记正版机器人", "@" + str(hit.get("official") or "未知")),
            ("相似度", f"{float(hit.get('ratio') or 0)*100:.1f}%"),
        ],
        reasons=["当前使用的机器人用户名/资料与已登记正版机器人高度相似，但并非正版账号"],
        advice="请勿通过疑似高仿钱包或机器人进行转账、授权、登录或交易；请从可信官方入口重新打开正版机器人。",
    )


def looks_like_scam_target(text: str) -> bool:
    s = str(text or "").strip()
    return bool(TARGET_RE.fullmatch(s))


QUICK_MESSAGE_MAX_KEYWORDS = 20
QUICK_MESSAGE_MAX_KEYWORD_CHARS = 80
QUICK_MESSAGE_MAX_REPLY_CHARS = 3000


def quick_parse_keywords(text: str) -> list[str]:
    parts: list[str] = []
    for raw in re.split(r"[\n,，、;；|]+", str(text or "")):
        kw = re.sub(r"\s+", " ", raw.strip())
        if not kw:
            continue
        if len(kw) > QUICK_MESSAGE_MAX_KEYWORD_CHARS:
            raise ValueError(f"单个触发关键词不能超过 {QUICK_MESSAGE_MAX_KEYWORD_CHARS} 字")
        if kw.lower() not in {x.lower() for x in parts}:
            parts.append(kw)
        if len(parts) > QUICK_MESSAGE_MAX_KEYWORDS:
            raise ValueError(f"每条快捷消息最多 {QUICK_MESSAGE_MAX_KEYWORDS} 个触发关键词")
    if not parts:
        raise ValueError("至少需要 1 个触发关键词")
    return parts


def _quick_row_to_dict(row) -> dict:
    d = dict(row)
    raw = str(d.get("keywords_json") or "").strip()
    try:
        kws = [str(x).strip() for x in json.loads(raw)] if raw else []
    except Exception:
        kws = []
    kws = [x for x in kws if x]
    if not kws:
        # r21-r26 旧数据：title 原本是手动按钮名；升级后安全视为单个触发关键词，不丢用户配置。
        old = str(d.get("title") or "").strip()
        kws = [old] if old else []
    d["keywords"] = kws
    return d


def quick_list(owner_id: int) -> list[dict]:
    ensure_feature_schema()
    conn = connect(APP_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id,title,reply_text,keywords_json,delete_trigger FROM quick_replies WHERE owner_id=? ORDER BY id ASC",
            (int(owner_id),),
        ).fetchall()
        return [_quick_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def quick_get(owner_id: int, record_id: int) -> dict | None:
    ensure_feature_schema()
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute(
            "SELECT id,title,reply_text,keywords_json,delete_trigger FROM quick_replies WHERE owner_id=? AND id=?",
            (int(owner_id), int(record_id)),
        ).fetchone()
        return _quick_row_to_dict(row) if row else None
    finally:
        conn.close()


def quick_upsert(owner_id: int, keywords, reply_text: str) -> int:
    ensure_feature_schema()
    if isinstance(keywords, str):
        kws = quick_parse_keywords(keywords)
    else:
        kws = quick_parse_keywords("\n".join(str(x) for x in (keywords or [])))
    reply = str(reply_text or "").strip()
    if not (1 <= len(reply) <= QUICK_MESSAGE_MAX_REPLY_CHARS):
        raise ValueError(f"快捷消息内容需 1-{QUICK_MESSAGE_MAX_REPLY_CHARS} 字")
    title = " / ".join(kws)
    # 兼容旧 UNIQUE(owner_id,title)；超长展示键压缩但 keywords_json 保留完整触发词。
    if len(title) > 240:
        title = title[:220] + f"…#{abs(hash(tuple(x.lower() for x in kws))) % 100000000:08d}"
    now = int(time.time())
    payload = json.dumps(kws, ensure_ascii=False, separators=(",", ":"))
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "INSERT INTO quick_replies(owner_id,title,reply_text,created_at,updated_at,keywords_json) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(owner_id,title) DO UPDATE SET reply_text=excluded.reply_text,keywords_json=excluded.keywords_json,updated_at=excluded.updated_at",
            (int(owner_id), title, reply, now, now, payload),
        )
        row = conn.execute("SELECT id FROM quick_replies WHERE owner_id=? AND title=?", (int(owner_id), title)).fetchone()
        return int(row[0]) if row else 0


def quick_delete(owner_id: int, record_id: int) -> bool:
    with tx(APP_DB_PATH, immediate=True) as conn:
        return int(conn.execute("DELETE FROM quick_replies WHERE owner_id=? AND id=?", (int(owner_id), int(record_id))).rowcount or 0) > 0


def quick_find_trigger(owner_id: int, text: str) -> dict | None:
    trigger = str(text or "").strip().lower()
    if not trigger:
        return None
    for row in quick_list(owner_id):
        for kw in row.get("keywords") or []:
            if trigger == str(kw or "").strip().lower():
                return row
    return None

def quick_set_delete_trigger(owner_id: int, record_id: int, enabled: bool) -> bool:
    with tx(APP_DB_PATH, immediate=True) as conn:
        cur = conn.execute(
            "UPDATE quick_replies SET delete_trigger=?,updated_at=? WHERE owner_id=? AND id=?",
            (1 if enabled else 0, int(time.time()), int(owner_id), int(record_id)),
        )
        return int(cur.rowcount or 0) > 0


def quick_update_reply(owner_id: int, record_id: int, reply_text: str) -> bool:
    reply = str(reply_text or "").strip()
    if not (1 <= len(reply) <= QUICK_MESSAGE_MAX_REPLY_CHARS):
        raise ValueError(f"快捷消息内容需 1-{QUICK_MESSAGE_MAX_REPLY_CHARS} 字")
    with tx(APP_DB_PATH, immediate=True) as conn:
        cur = conn.execute(
            "UPDATE quick_replies SET reply_text=?,updated_at=? WHERE owner_id=? AND id=?",
            (reply, int(time.time()), int(owner_id), int(record_id)),
        )
        return int(cur.rowcount or 0) > 0


def quick_update_keywords(owner_id: int, record_id: int, keywords) -> bool:
    if isinstance(keywords, str):
        kws = quick_parse_keywords(keywords)
    else:
        kws = quick_parse_keywords("\n".join(str(x) for x in (keywords or [])))
    title = " / ".join(kws)
    if len(title) > 240:
        title = title[:220] + f"…#{abs(hash(tuple(x.lower() for x in kws))) % 100000000:08d}"
    payload = json.dumps(kws, ensure_ascii=False, separators=(",", ":"))
    with tx(APP_DB_PATH, immediate=True) as conn:
        conflict = conn.execute(
            "SELECT id FROM quick_replies WHERE owner_id=? AND title=? AND id<>? LIMIT 1",
            (int(owner_id), title, int(record_id)),
        ).fetchone()
        if conflict:
            raise ValueError("已有相同触发关键词的快捷消息")
        cur = conn.execute(
            "UPDATE quick_replies SET title=?,keywords_json=?,updated_at=? WHERE owner_id=? AND id=?",
            (title, payload, int(time.time()), int(owner_id), int(record_id)),
        )
        return int(cur.rowcount or 0) > 0


def offline_get(owner_id: int) -> dict:
    ensure_feature_schema()
    oid = int(owner_id)
    if oid <= 0:
        return {"owner_id": oid, "enabled": 0, "template_text": "", "interval_seconds": 3600}
    # 绝大多数调用只是读取 UI/判断是否应回复；已有行时不再抢 BEGIN IMMEDIATE 写锁。
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute("SELECT * FROM offline_reply_settings WHERE owner_id=?", (oid,)).fetchone()
    finally:
        conn.close()
    if row:
        return dict(row)
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute("INSERT OR IGNORE INTO offline_reply_settings(owner_id,updated_at) VALUES(?,?)", (oid, now))
        row = conn.execute("SELECT * FROM offline_reply_settings WHERE owner_id=?", (oid,)).fetchone()
        return dict(row) if row else {"owner_id": oid, "enabled": 0, "template_text": "", "interval_seconds": 3600}


def offline_save(owner_id: int, *, enabled=None, template_text=None, interval_seconds=None, skip_when_online=None) -> None:
    cur = offline_get(owner_id)
    enabled_v = int(cur.get("enabled") or 0) if enabled is None else (1 if bool(enabled) else 0)
    text_v = str(cur.get("template_text") or "") if template_text is None else str(template_text or "").strip()
    interval_v = int(cur.get("interval_seconds") or 3600) if interval_seconds is None else int(interval_seconds)
    skip_v = int(cur.get("skip_when_online", 1) or 0) if skip_when_online is None else (1 if bool(skip_when_online) else 0)
    if not text_v or len(text_v) > 3500:
        raise ValueError("离线消息内容需 1-3500 字")
    interval_v = max(60, min(interval_v, 7 * 24 * 3600))
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "UPDATE offline_reply_settings SET enabled=?,template_text=?,interval_seconds=?,skip_when_online=?,updated_at=? WHERE owner_id=?",
            (enabled_v, text_v, interval_v, skip_v, int(time.time()), int(owner_id)),
        )


def offline_mark_owner_active(owner_id: int) -> None:
    ensure_feature_schema()
    oid = int(owner_id)
    if oid <= 0:
        return
    # 仅记录托管账号刚刚主动发过 Business 消息的时间，用于“在线时不触发”的轻量判定。
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO offline_reply_settings(owner_id,updated_at) VALUES(?,?)",
            (oid, int(time.time())),
        )
        conn.execute(
            "UPDATE offline_reply_settings SET last_owner_activity_at=?,updated_at=? WHERE owner_id=?",
            (int(time.time()), int(time.time()), oid),
        )


def offline_should_send(owner_id: int, peer_id: int) -> bool:
    st = offline_get(owner_id)
    if not int(st.get("enabled") or 0):
        return False
    now = int(time.time())
    # 与主机器人“客服在线时不触发”语义对齐。水杯采用最近主动 Business 发言 5 分钟作为在线窗口。
    if int(st.get("skip_when_online", 1) or 0):
        last_owner = int(st.get("last_owner_activity_at") or 0)
        if last_owner and now - last_owner <= 300:
            return False
    interval = max(60, int(st.get("interval_seconds") or 3600))
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute("SELECT last_sent_at FROM offline_reply_log WHERE owner_id=? AND peer_id=?", (int(owner_id), int(peer_id))).fetchone()
        last = int(row[0] or 0) if row else 0
        return now - last >= interval
    finally:
        conn.close()


def offline_mark_sent(owner_id: int, peer_id: int, message_id: int = 0) -> None:
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "INSERT INTO offline_reply_log(owner_id,peer_id,last_sent_at,last_message_id) VALUES(?,?,?,?) "
            "ON CONFLICT(owner_id,peer_id) DO UPDATE SET last_sent_at=excluded.last_sent_at,last_message_id=excluded.last_message_id",
            (int(owner_id), int(peer_id), int(time.time()), int(message_id or 0)),
        )


def set_last_active_business(owner_id: int, connection_id: str, chat_id: int, peer_name: str = "", peer_username: str = "") -> None:
    ensure_feature_schema()
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "INSERT INTO last_active_business(owner_id,connection_id,chat_id,peer_name,peer_username,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(owner_id) DO UPDATE SET connection_id=excluded.connection_id,chat_id=excluded.chat_id,peer_name=excluded.peer_name,peer_username=excluded.peer_username,updated_at=excluded.updated_at",
            (int(owner_id), str(connection_id or ""), int(chat_id), str(peer_name or "")[:150], str(peer_username or "")[:64], int(time.time())),
        )


def get_last_active_business(owner_id: int) -> dict | None:
    ensure_feature_schema()
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT l.* FROM last_active_business l
            JOIN business_connections b ON b.connection_id=l.connection_id
            WHERE l.owner_id=? AND b.owner_id=l.owner_id AND b.is_enabled=1
            """,
            (int(owner_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def wait_set(owner_id: int, kind: str, payload: dict | None = None) -> None:
    ensure_feature_schema()
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "INSERT INTO ui_wait_state(owner_id,kind,payload_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(owner_id) DO UPDATE SET kind=excluded.kind,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (int(owner_id), str(kind or "")[:64], json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")), int(time.time())),
        )


def wait_get(owner_id: int) -> dict | None:
    ensure_feature_schema()
    oid = int(owner_id)
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute("SELECT kind,payload_json,updated_at FROM ui_wait_state WHERE owner_id=?", (oid,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    updated_at = int(row["updated_at"] or 0)
    # 与主机器人 5 分钟临时输入状态一致。重启/忘记取消后不会无限期吞掉下一条私聊。
    if updated_at <= 0 or int(time.time()) - updated_at > WAIT_STATE_TTL_SECONDS:
        try:
            with tx(APP_DB_PATH, immediate=True) as c:
                c.execute("DELETE FROM ui_wait_state WHERE owner_id=? AND updated_at=?", (oid, updated_at))
        except Exception:
            pass
        return None
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except Exception:
        payload = {}
    return {"kind": str(row["kind"] or ""), "payload": payload, "updated_at": updated_at}


def wait_clear(owner_id: int) -> None:
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute("DELETE FROM ui_wait_state WHERE owner_id=?", (int(owner_id),))


def business_connection_upsert(connection_id: str, owner: dict, enabled: bool, user_chat_id: int = 0) -> dict:
    ensure_feature_schema()
    cid = str(connection_id or "").strip()
    owner = owner or {}
    oid = int(owner.get("id") or 0)
    if not cid or oid <= 0:
        return {"changed": False, "owner_id": oid, "enabled": bool(enabled), "user_chat_id": int(user_chat_id or 0)}
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        old = conn.execute("SELECT owner_id,is_enabled,user_chat_id FROM business_connections WHERE connection_id=?", (cid,)).fetchone()
        old_owner = int(old["owner_id"] or 0) if old else 0
        old_enabled = int(old["is_enabled"] or 0) if old else None
        if old and old_owner not in (0, oid):
            # Business connection ID 属于安全边界；一旦绑定非零 owner，不允许静默跨租户改写。
            return {
                "changed": False, "conflict": True, "owner_id": old_owner,
                "requested_owner_id": oid, "enabled": bool(old_enabled),
                "user_chat_id": int(old["user_chat_id"] or 0), "connection_id": cid,
            }
        conn.execute(
            "INSERT INTO business_connections(connection_id,owner_id,username,first_name,is_enabled,updated_at,user_chat_id) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(connection_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,"
            "is_enabled=excluded.is_enabled,updated_at=excluded.updated_at,"
            "user_chat_id=CASE WHEN excluded.user_chat_id>0 THEN excluded.user_chat_id ELSE business_connections.user_chat_id END",
            (cid, oid, str(owner.get("username") or ""), str(owner.get("first_name") or ""), 1 if enabled else 0, now, int(user_chat_id or 0)),
        )
        row = conn.execute("SELECT owner_id,is_enabled,user_chat_id FROM business_connections WHERE connection_id=?", (cid,)).fetchone()
        if not enabled:
            conn.execute("DELETE FROM last_active_business WHERE owner_id=? AND connection_id=?", (oid, cid))
    new_enabled = bool(int(row["is_enabled"] or 0)) if row else bool(enabled)
    return {
        "changed": old is None or old_owner != oid or old_enabled != (1 if enabled else 0),
        "previous_enabled": None if old_enabled is None else bool(old_enabled),
        "owner_id": int(row["owner_id"] or oid) if row else oid,
        "enabled": new_enabled,
        "user_chat_id": int(row["user_chat_id"] or user_chat_id or 0) if row else int(user_chat_id or 0),
        "connection_id": cid,
    }

def business_owner_any_id(connection_id: str) -> int:
    ensure_feature_schema()
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute("SELECT owner_id FROM business_connections WHERE connection_id=?", (str(connection_id or ""),)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _calc_normalize(expr: str) -> str:
    s = str(expr or "").strip()
    return (s.replace("（", "(").replace("）", ")").replace("＋", "+").replace("－", "-")
             .replace("＊", "*").replace("×", "*").replace("✕", "*").replace("x", "*").replace("X", "*")
             .replace("／", "/").replace("÷", "/").replace("％", "%").replace("＾", "^").replace("^", "**"))


def _calc_eval(node):
    if isinstance(node, ast.Expression):
        return _calc_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp):
        v = _calc_eval(node.operand)
        if isinstance(node.op, ast.UAdd): return v
        if isinstance(node.op, ast.USub): return -v
        raise ValueError("bad unary")
    if isinstance(node, ast.BinOp):
        a, b = _calc_eval(node.left), _calc_eval(node.right)
        if isinstance(node.op, ast.Add): return a + b
        if isinstance(node.op, ast.Sub): return a - b
        if isinstance(node.op, ast.Mult): return a * b
        if isinstance(node.op, ast.Div):
            if b == 0: raise ZeroDivisionError
            return a / b
        if isinstance(node.op, ast.Mod):
            if b == 0: raise ZeroDivisionError
            return a % b
        if isinstance(node.op, ast.Pow):
            if abs(b) > 20 or b != b.to_integral_value():
                raise ValueError("bad exponent")
            return a ** int(b)
    raise ValueError("bad expression")


def _decimal_text(value: Decimal) -> str:
    s = format(value.quantize(Decimal("0.00000001")), "f").rstrip("0").rstrip(".")
    return s or "0"


def try_calc_expression(raw: str) -> tuple[bool, str | None]:
    src = str(raw or "").strip()
    if not src or src in {"/", "//"} or not CALC_RE.fullmatch(src):
        return False, None
    normalized = _calc_normalize(src)
    if len(normalized) > 120 or not re.search(r"\d\s*(?:\+|\-|\*|/|%|\*\*)\s*[+-]?\s*\d", normalized):
        return False, None
    try:
        result = _calc_eval(ast.parse(normalized, mode="eval"))
        return True, f"{html.escape(src)}=<code>{html.escape(_decimal_text(result))}</code>"
    except ZeroDivisionError:
        return True, f"{html.escape(src)}=除数不能为0"
    except Exception:
        return True, "计算格式错误，例如：<code>1-1+1*2</code>、<code>（2+3）*4</code>、<code>10÷2</code>、<code>2^3</code>"
