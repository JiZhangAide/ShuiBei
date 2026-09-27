# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict

LOCAL_KNOWLEDGE_PATH = Path(__file__).with_name("premium_emoji_knowledge.json")
CANONICAL_KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent / "premium_emoji_knowledge.json"
KNOWLEDGE_PATH = CANONICAL_KNOWLEDGE_PATH if CANONICAL_KNOWLEDGE_PATH.is_file() else LOCAL_KNOWLEDGE_PATH

# ShuiBei 只写业务语义，不在 UI 代码里散落 custom_emoji_id。
# 映射到知识库的 name_cn；实际 ID 始终从 premium_emoji_knowledge.json 解析。
SEMANTIC_NAMES = {
    "home": "商店",
    "messages": "消息气泡",
    "finance": "钱包",
    "more": "竖向更多",
    "settings": "设置齿轮",
    "ledger": "文档列表",
    "sync": "同步",
    "protection": "历史记录",
    "risk": "盾牌警告",
    "fakebot": "机器人增强",
    "keyword": "文本/笔记",
    "rate": "美元实心圆",
    "crypto": "节点网络",
    "back": "左箭头",
    "preview": "查看/眼睛",
    "refresh": "刷新",
    "confirm": "成功按钮",
    "cancel": "取消按钮",
    "currency_default": "钱包",
    "currency_USDT": "USDT币种图标",
    "currency_USDC": "USDC币种图标",
    "currency_TRX": "TRX币种图标",
    "currency_TON": "TON币种图标",
    # v3 公共扩展：只有缺少墨清主映射时才作为语义 fallback 使用。
    "customer": "用户",
    "customer_tag": "公开扩展·客户标签",
    "customer_note": "公开扩展·笔记",
    "customer_activity": "公开扩展·客户足迹",
    "business": "公开扩展·业务公文包",
    "merchant": "公开扩展·商店",
    "due_reminder": "公开扩展·到期闹钟",
    "inactive": "公开扩展·休眠",
    "deal": "公开扩展·握手",
    "new_customer": "公开扩展·新增",
    "hint": "公开扩展·自适应灵感",
    "gift": "礼物盒子",
    "premium": "皇冠",
    "link": "链接",
    "welcome": "笑脸",
}


@lru_cache(maxsize=1)
def _index_by_name() -> Dict[str, str]:
    try:
        data = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    result: Dict[str, str] = {}
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name_cn") or "").strip()
        emoji_id = "".join(ch for ch in str(entry.get("custom_emoji_id") or "") if ch.isdigit())
        if name and emoji_id and name not in result:
            result[name] = emoji_id
    return result


def icon(semantic: str) -> str:
    name = SEMANTIC_NAMES.get(str(semantic or ""), "")
    if not name:
        return ""
    return _index_by_name().get(name, "")


def currency_icon(currency: str) -> str:
    key = f"currency_{str(currency or '').strip().upper()}"
    return icon(key) or icon("currency_default")


# ================== r22：与墨清记账 v158/v20 UI 精确对齐 ==================
# 这些 ID 直接来自主机器人当前最终 UI 层。水杯不再依赖模糊语义搜索来决定核心系统图标。
MAIN_UI_IDS = {
    "home": "5927169041595634481",
    "messages": "5884510167986343350",
    "finance": "5811989245761426317",
    "more": "5877219383691972108",
    "settings": "5877260593903177342",
    "ledger": "5951665890079544884",
    "sync": "6005843436479975944",
    "protection": "5778202206922608769",
    "risk": "5775887550262546277",
    "security_ok": "5931409969613116639",
    "fakebot": "5931614414351372818",
    "keyword": "5877396173135811032",
    "rate": "5811989245761426317",
    "crypto": "5877316724830768997",
    "back": "5877629862306385808",
    "preview": "5960714428394507968",
    "refresh": "6005843436479975944",
    "confirm": "5985596818912712352",
    "cancel": "5985346521103604145",
    "add": "5775937998948404844",
    "delete": "5879915802815107172",
    "edit": "5879841310902324730",
    "export": "5886666250158870040",
    "tools": "5877316724830768997",
    "user": "5879770735999717115",
    "notification": "5909201569898827582",
    "database": "5877485980901971030",
    "warning": "5881702736843511327",
    "on": "5776375003280838798",
    "off": "5778527486270770928",
    "welcome": "5985780596268339498",
    "gift": "6327615982500061650",
    "premium": "6028530359975548369",
    "link": "5877465816030515018",
    "business": "5983276784953594276",
}

# 主机器人 v20 风险提示专用会员 Emoji。
NOTICE_GREEN_ID = "6327748817248591655"
NOTICE_YELLOW_ID = "6311844463157190852"
NOTICE_RED_ID = "6314065987746405766"
NOTICE_FIELD_ID = "6312354778286399321"
CHECK_GREEN_ID = "6328057165835673404"
CROSS_ID = "6327886848907549687"


def custom_emoji(emoji_id: str, fallback: str) -> str:
    eid = "".join(ch for ch in str(emoji_id or "") if ch.isdigit())
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>' if eid else fallback


def system_icon(semantic: str, fallback: str = "•") -> str:
    eid = MAIN_UI_IDS.get(str(semantic or ""), "") or icon(semantic)
    return custom_emoji(eid, fallback)


def notice_green() -> str:
    return custom_emoji(NOTICE_GREEN_ID, "🟢")


def notice_yellow() -> str:
    return custom_emoji(NOTICE_YELLOW_ID, "🟡")


def notice_red() -> str:
    return custom_emoji(NOTICE_RED_ID, "🔴")


def notice_field() -> str:
    return custom_emoji(NOTICE_FIELD_ID, "▪️")


def notice_check() -> str:
    return custom_emoji(CHECK_GREEN_ID, "✅")


def notice_cross() -> str:
    return custom_emoji(CROSS_ID, "❌")


def notice_banner(state: str, title: str) -> str:
    st = str(state or "").strip().lower()
    if st == "risk":
        x = notice_red() * 3
    elif st == "safe":
        x = notice_green() * 3
    else:
        x = notice_yellow() * 2
    import html as _html
    return f"{x} <b>{_html.escape(str(title or '风险检测'))}</b> {x}"


# 只替换系统文本每一行最开头的普通 Emoji；不碰 blockquote/code 中的用户原文。
_LINE_ICON_MAP = (
    ("👋🏻", "messages", "👋🏻"), ("👋", "messages", "👋"),
    ("🥤", "home", "🥤"), ("🏠", "home", "🏠"),
    ("💬", "messages", "💬"), ("📒", "ledger", "📒"),
    ("💎", "finance", "💎"), ("💰", "finance", "💰"),
    ("⚙️", "settings", "⚙️"), ("⚙", "settings", "⚙"),
    ("🛡️", "security_ok", "🛡️"), ("🛡", "security_ok", "🛡"),
    ("🤖", "fakebot", "🤖"), ("🔄", "sync", "🔄"),
    ("🌙", "messages", "🌙"), ("🧮", "tools", "🧮"),
    ("⛓", "crypto", "⛓"), ("🔔", "notification", "🔔"),
    ("✏️", "edit", "✏️"), ("✏", "edit", "✏"),
    ("🗑️", "delete", "🗑️"), ("🗑", "delete", "🗑"),
    ("📤", "export", "📤"), ("🔍", "preview", "🔍"),
    ("🔎", "preview", "🔎"), ("ℹ️", "messages", "ℹ️"),
    ("✅", "confirm", "✅"), ("❌", "cancel", "❌"),
    ("⚠️", "warning", "⚠️"), ("⚠", "warning", "⚠"),
    ("🎉", "confirm", "🎉"), ("🎁", "gift", "🎁"),
    ("👑", "premium", "👑"), ("🔗", "link", "🔗"),
    ("🛠️", "tools", "🛠️"), ("🛠", "tools", "🛠"),
    ("💼", "business", "💼"), ("💱", "rate", "💱"),
)


def premiumize_html_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    out = []
    for line in raw.splitlines():
        # 已经由主模板输出 Premium Emoji 的行保持不动。
        if "<tg-emoji" in line:
            out.append(line)
            continue
        lead_len = len(line) - len(line.lstrip(" "))
        prefix = line[:lead_len]
        body = line[lead_len:]
        replaced = False
        # 风险状态圆点与主机器人 v20 完全使用同一组提示会员 Emoji。
        if body.startswith("🔴"):
            body = notice_red() + body[len("🔴"):]
            replaced = True
        elif body.startswith("🟡"):
            body = notice_yellow() + body[len("🟡"):]
            replaced = True
        elif body.startswith("🟢"):
            body = notice_green() + body[len("🟢"):]
            replaced = True
        if not replaced:
            for token, semantic, fallback in _LINE_ICON_MAP:
                if body.startswith(token):
                    body = system_icon(semantic, fallback) + body[len(token):]
                    replaced = True
                    break
        out.append(prefix + body)
    return "\n".join(out)


# 核心按钮图标必须与主机器人精确一致；币种仍优先使用专用币种图标。
_icon_before_r22 = icon

def icon(semantic: str) -> str:
    key = str(semantic or "")
    return MAIN_UI_IDS.get(key, "") or _icon_before_r22(key)
