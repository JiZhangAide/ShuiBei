# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import html
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

from config import ALLOWED_CURRENCIES, LEDGER_DB_PATH, MAX_DECIMALS, SCALE
from db import connect, get_settings, init_ledger_db, set_setting, tx
from emoji_knowledge import system_icon as premium_system_icon

LEDGER_RE = re.compile(r"^([+-])(\d+(?:\.\d{1,4})?)(?:\s+(.+))?$")
ITEM_LEDGER_RE = re.compile(r"^([+-])(\d+(?:\.\d{1,4})?)([^\d\s+\-*/\()（）.,，。:：;；][^\s+\-*/\()（）.,，。:：;；]{0,31})$")
BUSINESS_CATEGORIES = ("商品", "服务", "广告", "服务器", "手续费", "人工", "其他")


def normalize_category(value: str) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    if v not in BUSINESS_CATEGORIES:
        raise ValueError("不支持的账目分类")
    return v


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_amount_to_micro(value: str) -> int:
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("金额格式错误") from exc
    if d < 0:
        raise ValueError("金额不能为负")
    q = d.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return int(q * SCALE)


def micro_to_str(value: int) -> str:
    d = Decimal(int(value or 0)) / Decimal(SCALE)
    s = format(d, "f").rstrip("0").rstrip(".")
    return s or "0"


def currency(owner_id: int) -> str:
    c = str(get_settings(owner_id).get("ledger_currency") or "USDT").upper()
    return c if c in ALLOWED_CURRENCIES else "USDT"


def set_currency(owner_id: int, value: str) -> str:
    c = str(value or "").upper().strip()
    if c not in ALLOWED_CURRENCIES:
        raise ValueError("不支持的记账单位")
    set_setting(owner_id, "ledger_currency", c)
    return c


def get_balance(owner_id: int, peer_id: int = 0) -> int:
    init_ledger_db()
    conn = connect(LEDGER_DB_PATH)
    try:
        row = conn.execute(
            "SELECT balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1",
            (int(owner_id), int(peer_id)),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        conn.close()


def add_record(
    owner_id: int,
    peer_id: int,
    user_name: str,
    action: str,
    amount_micro: int,
    balance_micro: int,
    remark: str = "",
    *,
    category: str = "",
    cost_micro: int = 0,
) -> int:
    init_ledger_db()
    cat = normalize_category(category)
    cost = max(0, int(cost_micro or 0))
    with tx(LEDGER_DB_PATH, immediate=True) as conn:
        cur = conn.execute("""
            INSERT INTO ledger(owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time,category,cost_micro)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            int(owner_id), int(peer_id), str(user_name or "未知"), str(action or ""),
            int(amount_micro), int(balance_micro), str(remark or ""), now_str(), cat, cost,
        ))
        return int(cur.lastrowid)


def clear_ledger(owner_id: int, peer_id: int, user_name: str) -> int:
    return add_record(owner_id, peer_id, user_name, "清账", 0, 0, "清账")


def undo(owner_id: int, peer_id: int = 0) -> tuple[bool, int]:
    with tx(LEDGER_DB_PATH, immediate=True) as conn:
        row = conn.execute(
            "SELECT id FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1",
            (int(owner_id), int(peer_id)),
        ).fetchone()
        if not row:
            return False, 0
        conn.execute("DELETE FROM ledger WHERE id=? AND owner_id=?", (int(row[0]), int(owner_id)))
        row2 = conn.execute(
            "SELECT balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1",
            (int(owner_id), int(peer_id)),
        ).fetchone()
        return True, int(row2[0]) if row2 and row2[0] is not None else 0


def recent(owner_id: int, peer_id: int = 0, limit: int = 12):
    conn = connect(LEDGER_DB_PATH)
    try:
        return conn.execute("""
            SELECT id,time,user_name,action,amount_micro,balance_micro,remark,category,cost_micro
            FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT ?
        """, (int(owner_id), int(peer_id), int(limit))).fetchall()
    finally:
        conn.close()


def query_text(owner_id: int, peer_id: int = 0) -> str:
    rows = recent(owner_id, peer_id, 12)
    unit = currency(owner_id)
    if not rows:
        return f"📒 暂无账本记录\n当前余额：0 {unit}"
    lines = [f"📒 最近 {len(rows)} 条记录"]
    for row in reversed(rows):
        action = str(row["action"] or "")
        amount = micro_to_str(int(row["amount_micro"] or 0))
        remark = str(row["remark"] or "").strip()
        if action == "清账":
            body = "清账"
        else:
            sign = "+" if action in ("入", "收入", "+") else "-" if action in ("出", "支出", "-") else action
            body = f"{sign}{amount}"
        if remark and remark != "清账":
            body += f"  {remark}"
        lines.append(f"{row['time']}｜{body}")
    lines.append(f"\n当前余额：{micro_to_str(get_balance(owner_id, peer_id))} {unit}")
    return "\n".join(lines)


def export_text(owner_id: int) -> str:
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute("""
            SELECT id,peer_id,time,user_name,action,amount_micro,balance_micro,remark
            FROM ledger WHERE owner_id=? ORDER BY peer_id ASC,id ASC
        """, (int(owner_id),)).fetchall()
    finally:
        conn.close()
    unit = currency(owner_id)
    lines = ["record_id\tpeer_id\ttime\tuser\taction\tamount\tbalance\tcurrency\tremark"]
    for r in rows:
        lines.append("\t".join([
            str(r["id"]), str(r["peer_id"]), str(r["time"] or ""), str(r["user_name"] or ""), str(r["action"] or ""),
            micro_to_str(int(r["amount_micro"] or 0)), micro_to_str(int(r["balance_micro"] or 0)), unit, str(r["remark"] or ""),
        ]))
    return "\n".join(lines) + "\n"


def _item_balance(owner_id: int, peer_id: int, item_name: str) -> int:
    conn = connect(LEDGER_DB_PATH)
    try:
        row = conn.execute("""
            SELECT balance_micro FROM item_ledger WHERE owner_id=? AND peer_id=? AND item_name=? ORDER BY id DESC LIMIT 1
        """, (int(owner_id), int(peer_id), str(item_name))).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _item_add(owner_id: int, peer_id: int, user_name: str, item_name: str, action: str, amount_micro: int, balance_micro: int):
    with tx(LEDGER_DB_PATH, immediate=True) as conn:
        conn.execute("""
            INSERT INTO item_ledger(owner_id,peer_id,item_name,user_name,action,amount_micro,balance_micro,remark,time)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (int(owner_id), int(peer_id), str(item_name), str(user_name or "未知"), str(action), int(amount_micro), int(balance_micro), "", now_str()))


def handle_text(owner_id: int, peer_id: int, user_name: str, text: str) -> tuple[bool, str | None, str | None]:
    """返回 handled, reply_text, export_content。只处理个人/Business 账本，不含任何群账本。"""
    raw = str(text or "").strip()
    if not raw:
        return False, None, None
    st = get_settings(owner_id)
    unit = currency(owner_id)

    if raw in ("开启指定消费记账", "启用指定消费记账"):
        set_setting(owner_id, "item_ledger_enabled", 1)
        return True, "✅ 已开启指定消费记账。", None
    if raw in ("关闭指定消费记账", "停用指定消费记账"):
        set_setting(owner_id, "item_ledger_enabled", 0)
        return True, "✅ 已关闭指定消费记账。", None

    im = ITEM_LEDGER_RE.fullmatch(raw)
    if im and int(st.get("item_ledger_enabled") or 0):
        sign, amount_s, item = im.groups()
        amount = parse_amount_to_micro(amount_s)
        before = _item_balance(owner_id, peer_id, item)
        after = before + amount if sign == "+" else before - amount
        _item_add(owner_id, peer_id, user_name, item, "入" if sign == "+" else "出", amount, after)
        return True, f"✅ {item}：{'+' if sign == '+' else '-'}{micro_to_str(amount)}\n当前：{micro_to_str(after)}", None

    m = LEDGER_RE.fullmatch(raw)
    if m:
        sign, amount_s, remark = m.groups()
        amount = parse_amount_to_micro(amount_s)
        before = get_balance(owner_id, peer_id)
        after = before + amount if sign == "+" else before - amount
        add_record(owner_id, peer_id, user_name, "入" if sign == "+" else "出", amount, after, remark or "")
        return True, f"✅ {'入金' if sign == '+' else '出金'} {micro_to_str(amount)} {unit}\n当前余额：{micro_to_str(after)} {unit}" + (f"\n备注：{remark}" if remark else ""), None

    lo = raw.lower()
    if raw == "//":
        clear_ledger(owner_id, peer_id, user_name)
        return True, f"✅ 已清账\n当前余额：0 {unit}", None
    if raw == "/" or lo == "/balance":
        return True, query_text(owner_id, peer_id) if raw == "/" else f"当前余额：{micro_to_str(get_balance(owner_id, peer_id))} {unit}", None
    if lo == "/undo":
        ok, bal = undo(owner_id, peer_id)
        return True, (f"✅ 已撤销上一条记录。\n当前余额：{micro_to_str(bal)} {unit}" if ok else "没有可撤销的记录。"), None
    if lo == "/export":
        return True, None, export_text(owner_id)
    return False, None, None

# ================== v21：与墨清记账统一的默认记账模板 / 总账 ==================
import html as _html_v21


def ledger_format_success_html(action_text: str, amount_text: str, balance_text: str, remark: str = "", title: str = "") -> str:
    lines = []
    if str(title or "").strip():
        lines.append(_html_v21.escape(str(title).strip()))
    lines.append(f"{_html_v21.escape(str(action_text))} {_html_v21.escape(str(amount_text))}，剩余 {_html_v21.escape(str(balance_text))}")
    if str(remark or "").strip():
        lines.append(f"<blockquote>{_html_v21.escape(str(remark).strip())}</blockquote>")
    return "\n".join(lines)


def _normal_query_html(owner_id: int, peer_id: int = 0) -> str:
    conn = connect(LEDGER_DB_PATH)
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM ledger WHERE owner_id=? AND peer_id=?", (int(owner_id), int(peer_id))).fetchone()[0] or 0)
    finally:
        conn.close()
    unit = currency(owner_id)
    rows = recent(owner_id, peer_id, 5)
    lines = [f"共 {total} 条记录，剩余 {html.escape(micro_to_str(get_balance(owner_id, peer_id)))} {html.escape(unit)}"]
    for row in rows:
        action = str(row["action"] or "")
        user_name = html.escape(str(row["user_name"] or "未知"))
        stamp = html.escape(str(row["time"] or ""))
        remark = html.escape(str(row["remark"] or "").strip())
        if action == "清账":
            lines.append(f"{stamp} {user_name} 清账 -&gt; 0 {html.escape(unit)}")
            continue
        amount = html.escape(micro_to_str(int(row["amount_micro"] or 0)))
        balance = html.escape(micro_to_str(int(row["balance_micro"] or 0)))
        action_show = html.escape(action)
        body = f"{stamp} {user_name} {action_show} {amount} {html.escape(unit)}"
        if remark and remark != "清账":
            body += f"（{remark}）"
        body += f" -&gt; {balance} {html.escape(unit)}"
        lines.append(body)
    return "\n".join(lines)


def _item_query_html(owner_id: int, peer_id: int = 0) -> str:
    conn = connect(LEDGER_DB_PATH)
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM item_ledger WHERE owner_id=? AND peer_id=?", (int(owner_id), int(peer_id))).fetchone()[0] or 0)
        rows = conn.execute(
            "SELECT time,user_name,action,amount_micro,item_name,balance_micro,remark FROM item_ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 10",
            (int(owner_id), int(peer_id)),
        ).fetchall()
    finally:
        conn.close()
    if total <= 0:
        return "暂无指定消费记账。"
    lines = [f"共 {total} 条记录"]
    for row in rows:
        stamp = html.escape(str(row["time"] or ""))
        user_name = html.escape(str(row["user_name"] or "未知"))
        action = html.escape(str(row["action"] or ""))
        item = html.escape(str(row["item_name"] or ""))
        amount = html.escape(micro_to_str(int(row["amount_micro"] or 0)))
        balance = html.escape(micro_to_str(int(row["balance_micro"] or 0)))
        remark = html.escape(str(row["remark"] or "").strip())
        line = f"{stamp} {user_name} {action} {amount} {item} -&gt; {balance} {item}"
        if remark:
            line += f"（{remark}）"
        lines.append(line)
    return "\n".join(lines)


def query_text(owner_id: int, peer_id: int = 0) -> str:
    normal = _normal_query_html(owner_id, peer_id)
    item = _item_query_html(owner_id, peer_id)
    return "\n".join([
        "<b>【合集账本】</b>",
        "",
        "<b>【普通记账】</b>",
        normal,
        "",
        "<b>【指定消费记账】</b>",
        item,
    ])

def _bill_latest_rows(owner_id: int):
    conn = connect(LEDGER_DB_PATH)
    try:
        return conn.execute("""
            WITH latest AS (
                SELECT peer_id, MAX(id) AS max_id
                FROM ledger
                WHERE owner_id=? AND peer_id IS NOT NULL AND peer_id!=0
                GROUP BY peer_id
            )
            SELECT l.peer_id,l.user_name,l.balance_micro,l.time
            FROM ledger l
            JOIN latest x ON x.max_id=l.id
            WHERE l.owner_id=? AND COALESCE(l.balance_micro,0)<>0
            ORDER BY ABS(l.balance_micro) DESC,l.peer_id ASC
        """, (int(owner_id), int(owner_id))).fetchall()
    finally:
        conn.close()


def bill_summary(owner_id: int) -> dict:
    rows = list(_bill_latest_rows(owner_id))
    debt = [r for r in rows if int(r["balance_micro"] or 0) < 0]
    prepay = [r for r in rows if int(r["balance_micro"] or 0) > 0]
    return {"all": rows, "debt": debt, "prepay": prepay, "all_count": len(rows), "debt_count": len(debt), "prepay_count": len(prepay)}


def bill_center_text(owner_id: int) -> str:
    # 与主机器人 bill_menu_text 保持同一结构；水杯不具备的日/周/月报不在这里伪装展示。
    s = bill_summary(owner_id)
    unit = currency(owner_id)
    return (
        f"{premium_system_icon('finance', '💎')} <b>账单中心</b>\n\n"
        f"欠款账单：<b>{s['debt_count']}</b> 位\n"
        f"预付款账单：<b>{s['prepay_count']}</b> 位\n"
        f"总账：<b>{s['all_count']}</b> 位\n"
        f"当前记账单位：<b>{html.escape(str(unit))}</b>\n\n"
        "说明：\n"
        "• 欠款账单：记账余额为负数的客户\n"
        "• 预付款账单：记账余额为正数的客户\n"
        "• 总账：全部非零账单客户\n"
        "• 超过10位时自动发送 XLSX 表格，红底为欠款，绿底为预付款"
    )


def bill_list_text(owner_id: int, mode: str = "all") -> str:
    # 与主机器人 bill_build_text 保持同一视觉结构。水杯没有独立 username 字段时显示“-”。
    s = bill_summary(owner_id)
    key = mode if mode in {"all", "debt", "prepay"} else "all"
    rows = s[key]
    unit = currency(owner_id)
    title = {"all": "总账", "debt": "欠款账单", "prepay": "预付款账单"}[key]
    if not rows:
        return f"{title}：（共0位）\n暂无账单。"

    positive = sum(int(r["balance_micro"] or 0) for r in rows if int(r["balance_micro"] or 0) > 0)
    negative = sum(int(r["balance_micro"] or 0) for r in rows if int(r["balance_micro"] or 0) < 0)
    lines = []
    if key == "all":
        lines.append(f"总账：（共{len(rows)}位）")
        lines.append(f"预付款合计：{micro_to_str(positive)}{unit}")
        lines.append(f"欠款合计：{micro_to_str(abs(negative))}{unit}")
        lines.append(f"合剩余：{micro_to_str(positive + negative)}{unit}")
    elif key == "debt":
        lines.append(f"欠款账单：（共{len(rows)}位）")
        lines.append(f"欠款合计：{micro_to_str(abs(negative))}{unit}")
    else:
        lines.append(f"预付款账单：（共{len(rows)}位）")
        lines.append(f"合剩余：{micro_to_str(positive)}{unit}")
    lines.append("｜用户昵称｜用户名｜用户ID｜账单")
    for r in rows:
        name = str(r["user_name"] or "未知").replace("｜", "|")
        bal = int(r["balance_micro"] or 0)
        lines.append(f"{name}｜-｜{int(r['peer_id'])}｜{micro_to_str(bal)}{unit}")
    return "\n".join(lines)


# 覆盖最终个人/Business 记账入口：默认展示与墨清记账保持一致。
def handle_text(owner_id: int, peer_id: int, user_name: str, text: str) -> tuple[bool, str | None, str | None]:
    raw = str(text or "").strip()
    if not raw:
        return False, None, None
    st = get_settings(owner_id)
    unit = currency(owner_id)

    if raw in ("开启指定消费记账", "启用指定消费记账", "打开指定消费记账", "开启指定消费账本"):
        set_setting(owner_id, "item_ledger_enabled", 1)
        return True, "✅ 已开启指定消费记账。现在可发送 <code>+1可乐</code> / <code>-1可乐</code> 记录指定消费。", None
    if raw in ("关闭指定消费记账", "停用指定消费记账", "关掉指定消费记账", "关闭指定消费账本"):
        set_setting(owner_id, "item_ledger_enabled", 0)
        return True, "✅ 已关闭指定消费记账。", None

    # 与主机器人一致：普通 +数字 / -数字 优先，不让大额金额误入指定消费。
    m = LEDGER_RE.fullmatch(raw)
    if m:
        sign, amount_s, remark = m.groups()
        amount = parse_amount_to_micro(amount_s)
        before = get_balance(owner_id, peer_id)
        after = before + amount if sign == "+" else before - amount
        add_record(owner_id, peer_id, user_name, "入" if sign == "+" else "出", amount, after, remark or "")
        return True, ledger_format_success_html("入金" if sign == "+" else "出金", f"{micro_to_str(amount)} {unit}", f"{micro_to_str(after)} {unit}", remark or ""), None

    im = ITEM_LEDGER_RE.fullmatch(raw)
    if im:
        if not int(st.get("item_ledger_enabled") or 0):
            return True, "指定消费记账当前已关闭。\n如需使用 <code>+1可乐</code> / <code>-1可乐</code>，请先开启指定消费记账。", None
        sign, amount_s, item = im.groups()
        amount = parse_amount_to_micro(amount_s)
        before = _item_balance(owner_id, peer_id, item)
        after = before + amount if sign == "+" else before - amount
        _item_add(owner_id, peer_id, user_name, item, "入" if sign == "+" else "出", amount, after)
        return True, ledger_format_success_html("入金" if sign == "+" else "出金", f"{micro_to_str(amount)} {item}", f"{micro_to_str(after)} {item}"), None

    lo = raw.lower()
    if raw == "//":
        clear_ledger(owner_id, peer_id, user_name)
        return True, f"清账成功，剩余 0 {unit}", None
    if raw == "/":
        return True, query_text(owner_id, peer_id), None
    if lo == "/balance":
        return True, f"当前余额：{micro_to_str(get_balance(owner_id, peer_id))} {unit}", None
    if lo == "/undo":
        ok, bal = undo(owner_id, peer_id)
        return True, (f"已撤销上一条记录。当前余额：{micro_to_str(bal)} {unit}" if ok else "没有可撤销的记录。"), None
    if lo == "/export":
        return True, None, export_text(owner_id)
    return False, None, None
