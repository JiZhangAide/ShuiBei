# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import hashlib
import json
import time
from datetime import datetime, timedelta

from config import APP_DB_PATH, LEDGER_DB_PATH
from db import connect, tx, init_ledger_db
from ledger import currency, micro_to_str
from customers import customer_detail, list_customers

_VALID_STATUS = {"open", "settled", "cancelled"}


def ensure_receivable_schema() -> None:
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS customer_receivable_meta(
                owner_id INTEGER NOT NULL,
                peer_id INTEGER NOT NULL,
                due_at INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open',
                note TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(owner_id, peer_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_customer_receivable_due
            ON customer_receivable_meta(owner_id, status, due_at)
        """)


def get_due(owner_id: int, peer_id: int) -> dict:
    ensure_receivable_schema()
    conn = connect(APP_DB_PATH)
    try:
        row = conn.execute(
            "SELECT due_at,status,note,created_at,updated_at FROM customer_receivable_meta WHERE owner_id=? AND peer_id=?",
            (int(owner_id), int(peer_id)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"due_at": 0, "status": "open", "note": "", "created_at": 0, "updated_at": 0}
    return {
        "due_at": int(row["due_at"] or 0),
        "status": str(row["status"] or "open"),
        "note": str(row["note"] or ""),
        "created_at": int(row["created_at"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def set_due(owner_id: int, peer_id: int, due_at: int, note: str = "") -> dict:
    ensure_receivable_schema()
    if not customer_detail(int(owner_id), int(peer_id)):
        raise ValueError("客户不存在")
    now = int(time.time())
    due = max(0, int(due_at or 0))
    safe_note = str(note or "").strip()[:500]
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            """INSERT INTO customer_receivable_meta(owner_id,peer_id,due_at,status,note,created_at,updated_at)
               VALUES(?,?,?,'open',?,?,?)
               ON CONFLICT(owner_id,peer_id) DO UPDATE SET
                 due_at=excluded.due_at,status='open',note=excluded.note,updated_at=excluded.updated_at""",
            (int(owner_id), int(peer_id), due, safe_note, now, now),
        )
    return get_due(owner_id, peer_id)


def _set_status(owner_id: int, peer_id: int, status: str) -> None:
    if status not in _VALID_STATUS:
        return
    ensure_receivable_schema()
    now = int(time.time())
    with tx(APP_DB_PATH, immediate=True) as conn:
        conn.execute(
            "UPDATE customer_receivable_meta SET status=?,updated_at=? WHERE owner_id=? AND peer_id=?",
            (status, now, int(owner_id), int(peer_id)),
        )


def decorate_customer(owner_id: int, row: dict, now_ts: int | None = None) -> dict:
    out = dict(row or {})
    pid = int(out.get("peer_id") or 0)
    meta = get_due(int(owner_id), pid) if pid else {"due_at": 0, "status": "open", "note": ""}
    now = int(time.time() if now_ts is None else now_ts)
    bal = int(out.get("balance_micro") or 0)
    due_at = int(meta.get("due_at") or 0)
    status = str(meta.get("status") or "open")
    out.update({
        "due_at": due_at,
        "due_status": status,
        "due_note": str(meta.get("note") or ""),
        "overdue": bool(bal < 0 and status == "open" and due_at > 0 and due_at < now),
    })
    return out


def due_customers(owner_id: int, mode: str = "due", limit: int = 100, now_ts: int | None = None) -> list[dict]:
    now = int(time.time() if now_ts is None else now_ts)
    today = datetime.fromtimestamp(now)
    end_today = int(datetime(today.year, today.month, today.day, 23, 59, 59).timestamp())
    rows = [decorate_customer(owner_id, r, now) for r in list_customers(owner_id, "all", limit=5000, now_ts=now)]
    rows = [r for r in rows if int(r.get("balance_micro") or 0) < 0 and str(r.get("due_status") or "open") == "open"]
    if mode == "overdue":
        rows = [r for r in rows if bool(r.get("overdue"))]
    elif mode == "today":
        rows = [r for r in rows if 0 < int(r.get("due_at") or 0) <= end_today and not bool(r.get("overdue"))]
    elif mode == "week":
        rows = [r for r in rows if 0 < int(r.get("due_at") or 0) <= now + 7 * 86400]
    elif mode == "due":
        rows = [r for r in rows if int(r.get("due_at") or 0) > 0]
    rows.sort(key=lambda r: (int(r.get("due_at") or 2**31), -abs(int(r.get("balance_micro") or 0))))
    return rows[:max(1, min(500, int(limit or 100)))]


def settle_customer(owner_id: int, peer_id: int, *, mode: str, amount_micro: int = 0, remark: str = "", idempotency_key: str = "") -> dict:
    oid, pid = int(owner_id), int(peer_id)
    d = customer_detail(oid, pid)
    if not d:
        raise ValueError("客户不存在")
    clean_mode = str(mode or "").strip().lower()
    if clean_mode not in {"full", "partial", "waive"}:
        raise ValueError("不支持的结清方式")
    key = str(idempotency_key or "").strip()
    if not 8 <= len(key) <= 120:
        raise ValueError("请求标识缺失或无效，请重新打开 MiniApp")
    clean_remark = str(remark or "").strip()[:500]
    requested = int(amount_micro or 0) if clean_mode == "partial" else 0
    digest = hashlib.sha256(json.dumps([oid, pid, clean_mode, requested, clean_remark], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    with tx(LEDGER_DB_PATH, immediate=True) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS settlement_idempotency_v1(
            owner_id INTEGER NOT NULL, idem_key TEXT NOT NULL, request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(owner_id,idem_key))""")
        existing = conn.execute("SELECT request_hash,result_json FROM settlement_idempotency_v1 WHERE owner_id=? AND idem_key=?", (oid,key)).fetchone()
        if existing:
            if str(existing[0]) != digest:
                raise ValueError("idempotency_conflict: 请求标识与原收款内容不一致，请先核对账本")
            result = json.loads(str(existing[1]))
        else:
            row = conn.execute("SELECT balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1", (oid,pid)).fetchone()
            before = int(row[0] or 0) if row else 0
            if before >= 0:
                raise ValueError("该客户当前没有欠款")
            debt = abs(before)
            if clean_mode == "full":
                amount, action, default_remark = debt, "入", "收到全款"
            elif clean_mode == "partial":
                amount = requested
                if amount <= 0 or amount > debt:
                    raise ValueError("部分收款金额必须大于 0 且不能超过当前欠款")
                action, default_remark = "入", "收到部分款"
            else:
                amount, action, default_remark = debt, "减免", "免除欠款"
            after = before + amount
            if not -(2**63) <= after <= 2**63-1 or amount > 2**63-1:
                raise ValueError("金额超出存储范围")
            conn.execute("INSERT INTO ledger(owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time) VALUES(?,?,?,?,?,?,?,?)", (oid,pid,str(d.get("name") or "客户"),action,amount,after,clean_remark or default_remark,time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())))
            result = {"peer_id":pid,"mode":clean_mode,"amount_micro":amount,"balance_before_micro":before,"balance_after_micro":after,"currency":currency(oid)}
            conn.execute("INSERT INTO settlement_idempotency_v1(owner_id,idem_key,request_hash,result_json,created_at) VALUES(?,?,?,?,?)", (oid,key,digest,json.dumps(result,ensure_ascii=False,separators=(",",":")),int(time.time())))
    if int(result["balance_after_micro"]) >= 0:
        current = customer_detail(oid,pid)
        if current and int(current.get("balance_micro", 0) or 0) >= 0:
            _set_status(oid,pid,"settled")
    return result


def statement_html(owner_id: int, peer_id: int, limit: int = 12) -> str:
    d = customer_detail(int(owner_id), int(peer_id))
    if not d:
        raise ValueError("客户不存在")
    meta = get_due(owner_id, peer_id)
    unit = currency(int(owner_id))
    bal = int(d.get("balance_micro") or 0)
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute(
            "SELECT action,amount_micro,balance_micro,remark,time FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT ?",
            (int(owner_id), int(peer_id), max(1, min(50, int(limit or 12)))),
        ).fetchall()
    finally:
        conn.close()
    if bal < 0:
        summary = f"待结清 <b>{html.escape(micro_to_str(abs(bal)))} {html.escape(unit)}</b>"
    elif bal > 0:
        summary = f"当前预付款 <b>{html.escape(micro_to_str(bal))} {html.escape(unit)}</b>"
    else:
        summary = "<b>当前已结清</b>"
    lines = [
        "<b>水杯记账 · 对账单</b>",
        "",
        f"客户：{html.escape(str(d.get('name') or '客户'))}",
        summary,
    ]
    due = int(meta.get("due_at") or 0)
    if due and bal < 0:
        lines.append("到期：" + html.escape(time.strftime("%Y-%m-%d", time.localtime(due))))
    if rows:
        lines += ["", "<b>最近流水</b>"]
        for r in reversed(rows):
            act = str(r["action"] or "")
            amount = micro_to_str(abs(int(r["amount_micro"] or 0)))
            rem = str(r["remark"] or "").strip()
            sign = "+" if act in ("入", "收入", "+") else "-" if act in ("出", "支出", "-") else act
            body = f"{html.escape(str(r['time'] or ''))}｜{html.escape(sign)}{html.escape(amount)} {html.escape(unit)}"
            if rem:
                body += "｜" + html.escape(rem)
            lines.append(body)
    lines += ["", "如记录有误，请直接回复本消息核对。"]
    return "\n".join(lines)


def _period_bounds(mode: str, now_ts: int | None = None) -> tuple[int, int, str]:
    now = int(time.time() if now_ts is None else now_ts)
    dt = datetime.fromtimestamp(now)
    key = str(mode or "day").lower()
    if key == "week":
        start_dt = datetime(dt.year, dt.month, dt.day) - timedelta(days=dt.weekday())
        title = "本周"
    elif key == "month":
        start_dt = datetime(dt.year, dt.month, 1)
        title = "本月"
    else:
        start_dt = datetime(dt.year, dt.month, dt.day)
        title = "今日"
        key = "day"
    return int(start_dt.timestamp()), now, title


def _report_between(owner_id: int, start_ts: int, end_ts: int, title: str, mode: str) -> dict:
    ensure_receivable_schema()
    init_ledger_db()
    start_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(start_ts)))
    end_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(end_ts)))
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT peer_id,action,amount_micro,time,category,cost_micro FROM ledger
               WHERE owner_id=? AND time>=? AND time<=? ORDER BY id ASC""",
            (int(owner_id), start_s, end_s),
        ).fetchall()
    finally:
        conn.close()
    inflow = sum(int(r["amount_micro"] or 0) for r in rows if str(r["action"] or "") in ("入", "收入", "+"))
    outflow = sum(int(r["amount_micro"] or 0) for r in rows if str(r["action"] or "") in ("出", "支出", "-"))
    waived = sum(int(r["amount_micro"] or 0) for r in rows if str(r["action"] or "") == "减免")
    active_customers = len({int(r["peer_id"] or 0) for r in rows if int(r["peer_id"] or 0) > 0})

    sale_rows = [r for r in rows if str(r["action"] or "") in ("出", "支出", "-")]
    classified = [r for r in sale_rows if str(r["category"] or "").strip()]
    sales = sum(abs(int(r["amount_micro"] or 0)) for r in classified)
    cost = sum(max(0, int(r["cost_micro"] or 0)) for r in classified)
    gross_profit = sales - cost
    category_map = {}
    for row in classified:
        cat = str(row["category"] or "").strip() or "其他"
        item = category_map.setdefault(cat, {"category": cat, "sales_micro": 0, "cost_micro": 0, "count": 0})
        item["sales_micro"] += abs(int(row["amount_micro"] or 0))
        item["cost_micro"] += max(0, int(row["cost_micro"] or 0))
        item["count"] += 1
    category_breakdown = []
    for item in category_map.values():
        item["gross_profit_micro"] = int(item["sales_micro"]) - int(item["cost_micro"])
        category_breakdown.append(item)
    category_breakdown.sort(key=lambda x: (-int(x["sales_micro"]), str(x["category"])))

    all_customers = list_customers(int(owner_id), "all", limit=5000, now_ts=int(end_ts))
    receivable = sum(abs(int(r.get("balance_micro") or 0)) for r in all_customers if int(r.get("balance_micro") or 0) < 0)
    prepaid = sum(int(r.get("balance_micro") or 0) for r in all_customers if int(r.get("balance_micro") or 0) > 0)
    overdue = len(due_customers(owner_id, "overdue", limit=500, now_ts=int(end_ts)))
    return {
        "mode": str(mode or "custom").lower(),
        "title": str(title or "自定义"),
        "start_at": int(start_ts),
        "end_at": int(end_ts),
        "record_count": len(rows),
        "active_customer_count": active_customers,
        "inflow_micro": inflow,
        "outflow_micro": outflow,
        "waived_micro": waived,
        "receivable_micro": receivable,
        "prepaid_micro": prepaid,
        "overdue_count": overdue,
        "classified_sales_micro": sales,
        "cost_micro": cost,
        "gross_profit_micro": gross_profit,
        "classified_sale_count": len(classified),
        "sale_count": len(sale_rows),
        "unclassified_sale_count": max(0, len(sale_rows) - len(classified)),
        "category_breakdown": category_breakdown,
        "currency": currency(int(owner_id)),
    }


def report_summary(owner_id: int, mode: str = "day", now_ts: int | None = None) -> dict:
    start_ts, end_ts, title = _period_bounds(mode, now_ts)
    return _report_between(owner_id, start_ts, end_ts, title, str(mode or "day").lower())


def report_range_summary(owner_id: int, start_ts: int, end_ts: int) -> dict:
    start_i, end_i = int(start_ts), int(end_ts)
    if start_i <= 0 or end_i <= 0 or start_i > end_i:
        raise ValueError("报表时间范围无效")
    if end_i - start_i > 730 * 86400:
        raise ValueError("单次报表最多查询 730 天")
    start_day = time.strftime("%Y-%m-%d", time.localtime(start_i))
    end_day = time.strftime("%Y-%m-%d", time.localtime(end_i))
    return _report_between(owner_id, start_i, end_i, f"{start_day} ~ {end_day}", "custom")


def _report_html_from_summary(r: dict) -> str:
    unit = html.escape(str(r["currency"]))
    lines = [
        f"<b>水杯记账 · {html.escape(str(r['title']))}经营简报</b>",
        "",
        f"入账：<b>{html.escape(micro_to_str(int(r['inflow_micro'])))} {unit}</b>",
        f"出账：<b>{html.escape(micro_to_str(int(r['outflow_micro'])))} {unit}</b>",
        f"减免：<b>{html.escape(micro_to_str(int(r['waived_micro'])))} {unit}</b>",
        f"活跃客户：<b>{int(r['active_customer_count'])}</b> 位",
        f"流水：<b>{int(r['record_count'])}</b> 条",
    ]
    if int(r.get("classified_sale_count") or 0) > 0:
        lines += [
            "",
            "<b>经营利润（仅统计已分类成交）</b>",
            f"成交额：<b>{html.escape(micro_to_str(int(r.get('classified_sales_micro') or 0)))} {unit}</b>",
            f"成本：<b>{html.escape(micro_to_str(int(r.get('cost_micro') or 0)))} {unit}</b>",
            f"毛利润：<b>{html.escape(micro_to_str(int(r.get('gross_profit_micro') or 0)))} {unit}</b>",
            f"已分类：<b>{int(r.get('classified_sale_count') or 0)}</b> / {int(r.get('sale_count') or 0)} 笔",
        ]
    elif int(r.get("sale_count") or 0) > 0:
        lines += ["", "利润：本周期成交均为旧版未分类流水，暂不估算。"]
    lines += [
        "",
        f"当前总待收：<b>{html.escape(micro_to_str(int(r['receivable_micro'])))} {unit}</b>",
        f"当前预付款：<b>{html.escape(micro_to_str(int(r['prepaid_micro'])))} {unit}</b>",
        f"逾期客户：<b>{int(r['overdue_count'])}</b> 位",
    ]
    return "\n".join(lines)


def report_html(owner_id: int, mode: str = "day", now_ts: int | None = None) -> str:
    return _report_html_from_summary(report_summary(owner_id, mode, now_ts))


def report_range_html(owner_id: int, start_ts: int, end_ts: int) -> str:
    return _report_html_from_summary(report_range_summary(owner_id, start_ts, end_ts))
