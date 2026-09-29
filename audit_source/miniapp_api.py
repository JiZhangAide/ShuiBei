# -*- coding: utf-8 -*-
from __future__ import annotations

import html
import hashlib
import json
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request, send_from_directory

from config import APP_DB_PATH, BOT_TOKEN, LEDGER_DB_PATH
from db import connect, init_ledger_db, tx
from customers import customer_detail, list_customers, mark_collection_reminded, merchant_summary, set_customer_meta
from ledger import BUSINESS_CATEGORIES, currency, micro_to_str, normalize_category
from receivables import (
    decorate_customer,
    due_customers,
    get_due,
    report_range_summary,
    report_summary,
    set_due,
    settle_customer,
    statement_html,
)
from telegram_api import TelegramAPI
from miniapp_auth import validate_init_data
from advanced_ledger import (
    apply_template,
    book_overview,
    create_book,
    create_close_snapshot,
    create_recurring_receivable,
    create_template,
    customer_summary,
    customer_timeline,
    entry_meta_map,
    list_books,
    list_close_snapshots,
    list_recurring,
    list_templates,
    process_due_recurring,
    reverse_entry,
    set_entry_meta,
    set_recurring_active,
    trend_series,
)

BUILD = "shuibei-miniapp-v3-security-20260927"


def _ok(data=None, status=200):
    return jsonify({"ok": True, "data": data}), status


def _fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": {"message": str(message)[:300]}}), status


def _auth_from_request(*, max_age_seconds: int = 1800):
    raw = str(request.headers.get("Authorization") or "").strip()
    if not raw.lower().startswith("tma "):
        raise ValueError("请从 Telegram 打开水杯记账")
    return validate_init_data(
        raw[4:].strip(),
        BOT_TOKEN,
        max_age_seconds=max(60, int(max_age_seconds)),
    )


def _auth_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            g.shuibei_auth = _auth_from_request(max_age_seconds=1800)
        except Exception:
            return _fail("Mini App 授权已失效，请从水杯记账重新打开", 401)
        return fn(*args, **kwargs)
    return wrapped


def _financial_auth_required(fn):
    """Use a shorter Telegram initData lifetime for balance-changing writes."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            g.shuibei_auth = _auth_from_request(max_age_seconds=600)
        except Exception:
            return _fail("Mini App 授权已失效，请从水杯记账重新打开", 401)
        return fn(*args, **kwargs)
    return wrapped


def _customer_row(owner_id: int, row: dict) -> dict:
    d = decorate_customer(owner_id, row)
    bal = int(d.get("balance_micro") or 0)
    return {
        "peer_id": int(d.get("peer_id") or 0),
        "name": str(d.get("name") or "客户"),
        "username": str(d.get("username") or ""),
        "balance_micro": bal,
        "amount_due_micro": abs(bal) if bal < 0 else 0,
        "prepaid_micro": bal if bal > 0 else 0,
        "currency": currency(owner_id),
        "last_contact_at": int(d.get("last_contact_at") or 0),
        "label": str(d.get("label") or ""),
        "note": str(d.get("note") or ""),
        "has_business": bool(d.get("has_business")),
        "connection_id": str(d.get("connection_id") or ""),
        "due_at": int(d.get("due_at") or 0),
        "overdue": bool(d.get("overdue")),
        "remind_count": int(d.get("remind_count") or 0),
    }


def _active_business_connection(owner_id: int, connection_id: str) -> bool:
    cid = str(connection_id or "").strip()
    if not cid:
        return False
    conn = connect(APP_DB_PATH)
    try:
        return conn.execute(
            "SELECT 1 FROM business_connections WHERE owner_id=? AND connection_id=? AND is_enabled=1 LIMIT 1",
            (int(owner_id), cid),
        ).fetchone() is not None
    finally:
        conn.close()


def _ledger_rows(owner_id: int, peer_id: int, limit: int = 30) -> list[dict]:
    conn = connect(LEDGER_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT id,time,user_name,action,amount_micro,balance_micro,remark,category,cost_micro
               FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT ?""",
            (int(owner_id), int(peer_id), max(1, min(100, int(limit or 30)))),
        ).fetchall()
    finally:
        conn.close()
    meta = entry_meta_map(int(owner_id), [int(r["id"]) for r in rows])
    return [{
        "id": int(r["id"]),
        "time": str(r["time"] or ""),
        "action": str(r["action"] or ""),
        "amount_micro": int(r["amount_micro"] or 0),
        "balance_micro": int(r["balance_micro"] or 0),
        "remark": str(r["remark"] or ""),
        "category": str(r["category"] or ""),
        "cost_micro": max(0, int(r["cost_micro"] or 0)),
        "book_id": int(meta.get(int(r["id"]), {}).get("book_id") or 0),
        "status": str(meta.get(int(r["id"]), {}).get("status") or "posted"),
        "reversal_of": int(meta.get(int(r["id"]), {}).get("reversal_of") or 0),
        "source_message_id": int(meta.get(int(r["id"]), {}).get("source_message_id") or 0),
        "attachment_ref": str(meta.get(int(r["id"]), {}).get("attachment_ref") or ""),
    } for r in rows]


def _ensure_miniapp_ledger_schema() -> None:
    # db.init_ledger_db() owns schema creation/migrations and is process-cached.
    init_ledger_db()


def _miniapp_add_ledger(
    owner_id: int,
    peer_id: int,
    kind: str,
    amount_micro: int,
    remark: str,
    idem_key: str,
    *,
    category: str = "",
    cost_micro: int = 0,
    book_id: int = 0,
) -> dict:
    amount = int(amount_micro or 0)
    if amount <= 0 or amount > 10**15:
        raise ValueError("金额无效")
    kind = str(kind or "").strip().lower()
    if kind not in {"debt", "payment"}:
        raise ValueError("记账类型无效")
    d = customer_detail(owner_id, peer_id)
    if not d:
        raise ValueError("客户不存在")
    key = str(idem_key or "").strip()
    if len(key) < 8 or len(key) > 120:
        raise ValueError("请求标识无效")

    cat = normalize_category(category or ("其他" if kind == "debt" else ""))
    cost = max(0, int(cost_micro or 0))
    if cost > 10**15:
        raise ValueError("成本金额无效")
    if kind == "payment":
        cat = ""
        cost = 0

    canonical = [int(owner_id), int(peer_id), kind, amount, str(remark or "")[:500], cat, cost, int(book_id or 0)]
    request_hash = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    _ensure_miniapp_ledger_schema()
    with tx(LEDGER_DB_PATH, immediate=True) as conn:
        existing = conn.execute(
            "SELECT result_json,request_hash FROM miniapp_idempotency WHERE owner_id=? AND idem_key=?",
            (int(owner_id), key),
        ).fetchone()
        if existing:
            if not existing[1] or str(existing[1]) != request_hash:
                raise ValueError("idempotency_conflict: 请求标识与原记账内容不一致，请先核对账本")
            try:
                return json.loads(str(existing[0] or "{}"))
            except Exception:
                raise ValueError("重复请求状态异常")
        row = conn.execute(
            "SELECT balance_micro FROM ledger WHERE owner_id=? AND peer_id=? ORDER BY id DESC LIMIT 1",
            (int(owner_id), int(peer_id)),
        ).fetchone()
        before = int(row[0] or 0) if row else 0
        if kind == "debt":
            after = before - amount
            action = "出"
        else:
            after = before + amount
            action = "入"
        if not -(2**63) <= after <= 2**63 - 1:
            raise ValueError("余额超出存储范围")
        now_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        cur = conn.execute(
            """INSERT INTO ledger(owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time,category,cost_micro)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                int(owner_id), int(peer_id), str(d.get("name") or "客户"), action, amount, after,
                str(remark or "")[:500], now_s, cat, cost,
            ),
        )
        result = {
            "id": int(cur.lastrowid or 0),
            "balance_before_micro": before,
            "balance_after_micro": after,
            "amount_micro": amount,
            "action": action,
            "category": cat,
            "cost_micro": cost,
            "gross_profit_micro": (amount - cost) if kind == "debt" else 0,
            "book_id": int(book_id or 0),
            "currency": currency(owner_id),
        }
        conn.execute(
            "INSERT INTO miniapp_idempotency(owner_id,idem_key,result_json,created_at,request_hash) VALUES(?,?,?,?,?)",
            (int(owner_id), key, json.dumps(result, ensure_ascii=False, separators=(",", ":")), int(time.time()), request_hash),
        )
        conn.execute(
            "DELETE FROM miniapp_idempotency WHERE owner_id=? AND created_at<?",
            (int(owner_id), int(time.time()) - 7 * 86400),
        )
    set_entry_meta(int(owner_id), int(result["id"]), book_id=int(book_id or 0), status="posted")
    return result


def _peer_ids(body: dict, *, max_count: int = 100) -> list[int]:
    raw = body.get("peer_ids") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        raise ValueError("请选择客户")
    out = []
    seen = set()
    for value in raw:
        try:
            pid = int(value)
        except Exception:
            continue
        if pid > 0 and pid not in seen:
            seen.add(pid)
            out.append(pid)
    if not out:
        raise ValueError("请选择客户")
    if len(out) > int(max_count):
        raise ValueError(f"单次最多选择 {int(max_count)} 位客户")
    return out


def _query_date_range(start_s: str, end_s: str) -> tuple[int, int]:
    try:
        start = datetime.strptime(str(start_s or "").strip(), "%Y-%m-%d")
        end = datetime.strptime(str(end_s or "").strip(), "%Y-%m-%d")
    except Exception as exc:
        raise ValueError("日期格式无效") from exc
    start_ts = int(datetime(start.year, start.month, start.day, 0, 0, 0).timestamp())
    end_ts = int(datetime(end.year, end.month, end.day, 23, 59, 59).timestamp())
    if start_ts > end_ts:
        raise ValueError("开始日期不能晚于结束日期")
    return start_ts, end_ts


def register_shuibei_miniapp(app, *, static_dir: str):
    static_root = Path(static_dir).resolve()
    bp = Blueprint("shuibei_miniapp_v1", __name__, url_prefix="/ShuiBei")

    @bp.after_request
    def _headers(resp):
        if request.path.startswith("/ShuiBei/api/") or request.path in {"/ShuiBei/app", "/ShuiBei/app/"}:
            resp.headers["Cache-Control"] = "no-store"
        else:
            resp.headers["Cache-Control"] = "private, no-cache, max-age=0, must-revalidate"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://telegram.org; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'"
        )
        return resp

    @bp.errorhandler(Exception)
    def _unexpected_error(exc):
        current_app.logger.exception("ShuiBei MiniApp request failed")
        return _fail("internal_error", 500)

    @bp.route("/app")
    @bp.route("/app/")
    def app_index():
        return send_from_directory(static_root, "index.html")

    @bp.route("/app/<path:name>")
    def app_asset(name):
        target = static_root / name
        if target.is_file():
            return send_from_directory(static_root, name)
        return send_from_directory(static_root, "index.html")

    @bp.route("/api/miniapp/v1/health")
    def health():
        return _ok({"status": "ok"})

    @bp.route("/api/miniapp/v1/bootstrap")
    @_auth_required
    def bootstrap():
        a = g.shuibei_auth
        return _ok({
            "build": BUILD,
            "viewer": {
                "id": int(a.user_id),
                "name": str(a.user.get("first_name") or "用户"),
                "username": str(a.user.get("username") or ""),
            },
            "currency": currency(int(a.user_id)),
            "categories": list(BUSINESS_CATEGORIES),
            "books": list_books(int(a.user_id)),
        })

    @bp.route("/api/miniapp/v1/home")
    @_auth_required
    def home():
        uid = int(g.shuibei_auth.user_id)
        try:
            process_due_recurring(uid, max_runs=20)
        except Exception:
            pass
        sm = merchant_summary(uid)
        day = report_summary(uid, "day")
        overdue = due_customers(uid, "overdue", limit=50)
        today = due_customers(uid, "today", limit=50)
        recent = list_customers(uid, "all", limit=6)
        return _ok({
            "customer_count": int(sm.get("customer_count") or 0),
            "receivable_micro": int(sm.get("debt_amount_micro") or 0),
            "prepaid_micro": int(sm.get("prepay_amount_micro") or 0),
            "overdue_count": len(overdue),
            "today_due_count": len(today),
            "today_inflow_micro": int(day.get("inflow_micro") or 0),
            "today_outflow_micro": int(day.get("outflow_micro") or 0),
            "today_gross_profit_micro": int(day.get("gross_profit_micro") or 0),
            "today_classified_sale_count": int(day.get("classified_sale_count") or 0),
            "currency": currency(uid),
            "recent_customers": [_customer_row(uid, x) for x in recent],
        })

    @bp.route("/api/miniapp/v1/customers")
    @_auth_required
    def customers_list():
        uid = int(g.shuibei_auth.user_id)
        mode = str(request.args.get("filter") or "all").lower()
        q = str(request.args.get("q") or "").strip().lower()[:80]
        if mode in {"overdue", "today", "week", "due"}:
            rows = due_customers(uid, mode, limit=500)
        elif mode == "debt":
            rows = list_customers(uid, "debt", limit=500)
        elif mode == "prepay":
            rows = list_customers(uid, "prepay", limit=500)
        elif mode == "recent":
            rows = list_customers(uid, "recent", limit=500)
        else:
            rows = list_customers(uid, "all", limit=500)
        out = [_customer_row(uid, x) for x in rows]
        if q:
            out = [x for x in out if q in x["name"].lower() or q in x["username"].lower() or q in x["label"].lower()]
        return _ok(out[:200])

    @bp.route("/api/miniapp/v1/customers/<int:peer_id>")
    @_auth_required
    def customer_get(peer_id: int):
        uid = int(g.shuibei_auth.user_id)
        d = customer_detail(uid, int(peer_id))
        if not d:
            return _fail("客户不存在", 404)
        return _ok(_customer_row(uid, d))

    @bp.route("/api/miniapp/v1/customers/<int:peer_id>/ledger")
    @_auth_required
    def customer_ledger(peer_id: int):
        uid = int(g.shuibei_auth.user_id)
        if not customer_detail(uid, peer_id):
            return _fail("客户不存在", 404)
        return _ok(_ledger_rows(uid, peer_id, int(request.args.get("limit") or 30)))

    @bp.route("/api/miniapp/v1/customers/<int:peer_id>/ledger", methods=["POST"])
    @_financial_auth_required
    def customer_ledger_add(peer_id: int):
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            result = _miniapp_add_ledger(
                uid,
                int(peer_id),
                str(body.get("kind") or ""),
                int(body.get("amount_micro") or 0),
                str(body.get("remark") or ""),
                str(body.get("idempotency_key") or ""),
                category=str(body.get("category") or ""),
                cost_micro=int(body.get("cost_micro") or 0),
                book_id=int(body.get("book_id") or 0),
            )
            return _ok(result)
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)

    @bp.route("/api/miniapp/v1/customers/<int:peer_id>/due", methods=["POST"])
    @_auth_required
    def customer_due(peer_id: int):
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            return _ok(set_due(uid, int(peer_id), int(body.get("due_at") or 0), str(body.get("note") or "")))
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)

    @bp.route("/api/miniapp/v1/customers/<int:peer_id>/settle", methods=["POST"])
    @_financial_auth_required
    def customer_settle(peer_id: int):
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            result = settle_customer(
                uid,
                int(peer_id),
                mode=str(body.get("mode") or ""),
                amount_micro=int(body.get("amount_micro") or 0),
                remark=str(body.get("remark") or ""),
                idempotency_key=str(body.get("idempotency_key") or ""),
            )
            return _ok(result)
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)

    @bp.route("/api/miniapp/v1/customers/<int:peer_id>/statement", methods=["POST"])
    @_auth_required
    def customer_statement(peer_id: int):
        uid = int(g.shuibei_auth.user_id)
        d = customer_detail(uid, int(peer_id))
        if not d:
            return _fail("客户不存在", 404)
        body = request.get_json(silent=True) or {}
        try:
            content = statement_html(uid, int(peer_id))
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)
        sent = False
        if bool(body.get("send")):
            cid = str(d.get("connection_id") or "")
            if not cid or not _active_business_connection(uid, cid):
                return _fail("当前客户没有可用的 Telegram Business 会话", 409)
            try:
                TelegramAPI(BOT_TOKEN).send_message(int(peer_id), content, business_connection_id=cid, parse_mode="HTML")
                sent = True
            except Exception:
                return _fail("对账单发送失败，请回机器人里重试", 502)
        return _ok({"html": content, "sent": sent})

    @bp.route("/api/miniapp/v1/batch/label", methods=["POST"])
    @_auth_required
    def batch_label():
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            peers = _peer_ids(body, max_count=100)
            label = str(body.get("label") or "").strip()[:40]
            changed = 0
            for peer_id in peers:
                set_customer_meta(uid, peer_id, label=label)
                changed += 1
            return _ok({"changed": changed})
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)

    @bp.route("/api/miniapp/v1/batch/due", methods=["POST"])
    @_auth_required
    def batch_due():
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            peers = _peer_ids(body, max_count=100)
            due_at = max(0, int(body.get("due_at") or 0))
            changed = 0
            for peer_id in peers:
                set_due(uid, peer_id, due_at)
                changed += 1
            return _ok({"changed": changed, "due_at": due_at})
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)

    @bp.route("/api/miniapp/v1/batch/statements", methods=["POST"])
    @_auth_required
    def batch_statements():
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            peers = _peer_ids(body, max_count=20)
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)
        sent = 0
        failed = []
        api = TelegramAPI(BOT_TOKEN)
        for peer_id in peers:
            d = customer_detail(uid, peer_id)
            cid = str((d or {}).get("connection_id") or "")
            if not d or not cid or not _active_business_connection(uid, cid):
                failed.append({"peer_id": peer_id, "reason": "Business 会话不可用"})
                continue
            try:
                api.send_message(peer_id, statement_html(uid, peer_id), business_connection_id=cid, parse_mode="HTML")
                sent += 1
            except Exception:
                failed.append({"peer_id": peer_id, "reason": "发送失败"})
        return _ok({"requested": len(peers), "sent": sent, "failed": failed})

    @bp.route("/api/miniapp/v1/batch/reminders", methods=["POST"])
    @_auth_required
    def batch_reminders():
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            peers = _peer_ids(body, max_count=20)
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)
        sent = 0
        failed = []
        api = TelegramAPI(BOT_TOKEN)
        unit = currency(uid)
        for peer_id in peers:
            d = customer_detail(uid, peer_id)
            bal = int((d or {}).get("balance_micro") or 0)
            cid = str((d or {}).get("connection_id") or "")
            if not d or bal >= 0:
                failed.append({"peer_id": peer_id, "reason": "当前没有欠款"})
                continue
            if not cid or not _active_business_connection(uid, cid):
                failed.append({"peer_id": peer_id, "reason": "Business 会话不可用"})
                continue
            amount = f"{micro_to_str(abs(bal))} {unit}"
            body_text = (
                f"您好 {html.escape(str(d.get('name') or ''))}，您当前还有 "
                f"<b>{html.escape(amount)}</b> 待结清，请及时处理，谢谢。"
            )
            try:
                api.send_message(peer_id, body_text, business_connection_id=cid, parse_mode="HTML")
                mark_collection_reminded(uid, peer_id)
                sent += 1
            except Exception:
                failed.append({"peer_id": peer_id, "reason": "发送失败"})
        return _ok({"requested": len(peers), "sent": sent, "failed": failed})

    @bp.route("/api/miniapp/v1/reports/custom")
    @_auth_required
    def reports_custom():
        try:
            start_ts, end_ts = _query_date_range(request.args.get("start") or "", request.args.get("end") or "")
            return _ok(report_range_summary(int(g.shuibei_auth.user_id), start_ts, end_ts))
        except ValueError as exc:
            return _fail(str(exc), 409 if str(exc).startswith("idempotency_") else 400)

    @bp.route("/api/miniapp/v1/reports/<mode>")
    @_auth_required
    def reports(mode: str):
        if mode not in {"day", "week", "month"}:
            return _fail("报表周期无效", 400)
        return _ok(report_summary(int(g.shuibei_auth.user_id), mode))


    @bp.route("/api/miniapp/v1/books", methods=["GET", "POST"])
    @_auth_required
    def advanced_books():
        uid = int(g.shuibei_auth.user_id)
        if request.method == "GET":
            return _ok(list_books(uid))
        body = request.get_json(silent=True) or {}
        try:
            return _ok(create_book(uid, str(body.get("name") or "")), 201)
        except ValueError as exc:
            return _fail(str(exc), 400)

    @bp.route("/api/miniapp/v1/books/overview")
    @_auth_required
    def advanced_books_overview():
        return _ok(book_overview(int(g.shuibei_auth.user_id)))

    @bp.route("/api/miniapp/v1/templates", methods=["GET", "POST"])
    @_auth_required
    def advanced_templates():
        uid = int(g.shuibei_auth.user_id)
        if request.method == "GET":
            return _ok(list_templates(uid, peer_id=int(request.args.get("peer_id") or 0)))
        body = request.get_json(silent=True) or {}
        try:
            row = create_template(
                uid,
                str(body.get("name") or ""),
                kind=str(body.get("kind") or ""),
                amount_micro=int(body.get("amount_micro") or 0),
                peer_id=int(body.get("peer_id") or 0),
                book_id=int(body.get("book_id") or 0),
                remark=str(body.get("remark") or ""),
                category=str(body.get("category") or ""),
                cost_micro=int(body.get("cost_micro") or 0),
            )
            return _ok(row, 201)
        except ValueError as exc:
            return _fail(str(exc), 400)

    @bp.route("/api/miniapp/v1/templates/<int:template_id>/apply", methods=["POST"])
    @_financial_auth_required
    def advanced_template_apply(template_id: int):
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            return _ok(apply_template(uid, template_id, peer_id=int(body.get("peer_id") or 0)))
        except ValueError as exc:
            return _fail(str(exc), 400)

    @bp.route("/api/miniapp/v1/recurring", methods=["GET", "POST"])
    @_auth_required
    def advanced_recurring():
        uid = int(g.shuibei_auth.user_id)
        if request.method == "GET":
            process_due_recurring(uid, max_runs=20)
            return _ok(list_recurring(uid, peer_id=int(request.args.get("peer_id") or 0), active_only=False))
        body = request.get_json(silent=True) or {}
        try:
            row = create_recurring_receivable(
                uid,
                int(body.get("peer_id") or 0),
                title=str(body.get("title") or ""),
                amount_micro=int(body.get("amount_micro") or 0),
                cadence=str(body.get("cadence") or ""),
                next_due_at=int(body.get("next_due_at") or 0),
                book_id=int(body.get("book_id") or 0),
                remark=str(body.get("remark") or ""),
                category=str(body.get("category") or ""),
                cost_micro=int(body.get("cost_micro") or 0),
            )
            return _ok(row, 201)
        except ValueError as exc:
            return _fail(str(exc), 400)

    @bp.route("/api/miniapp/v1/recurring/<int:recurring_id>/active", methods=["POST"])
    @_auth_required
    def advanced_recurring_active(recurring_id: int):
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        return _ok({"updated": bool(set_recurring_active(uid, recurring_id, bool(body.get("active"))))})

    @bp.route("/api/miniapp/v1/recurring/process", methods=["POST"])
    @_financial_auth_required
    def advanced_recurring_process():
        uid = int(g.shuibei_auth.user_id)
        return _ok({"created": process_due_recurring(uid, max_runs=50)})

    @bp.route("/api/miniapp/v1/customers/<int:peer_id>/summary")
    @_auth_required
    def advanced_customer_summary(peer_id: int):
        try:
            return _ok(customer_summary(int(g.shuibei_auth.user_id), peer_id))
        except ValueError as exc:
            return _fail(str(exc), 404)

    @bp.route("/api/miniapp/v1/customers/<int:peer_id>/timeline")
    @_auth_required
    def advanced_customer_timeline(peer_id: int):
        return _ok(customer_timeline(
            int(g.shuibei_auth.user_id),
            peer_id,
            limit=int(request.args.get("limit") or 30),
        ))

    @bp.route("/api/miniapp/v1/ledger/<int:ledger_id>/reverse", methods=["POST"])
    @_financial_auth_required
    def advanced_reverse(ledger_id: int):
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            return _ok(reverse_entry(uid, ledger_id, remark=str(body.get("remark") or "")))
        except ValueError as exc:
            return _fail(str(exc), 409)

    @bp.route("/api/miniapp/v1/ledger/<int:ledger_id>/meta", methods=["PATCH"])
    @_auth_required
    def advanced_entry_meta(ledger_id: int):
        uid = int(g.shuibei_auth.user_id)
        body = request.get_json(silent=True) or {}
        try:
            return _ok(set_entry_meta(
                uid,
                ledger_id,
                book_id=body.get("book_id") if "book_id" in body else None,
                status=body.get("status") if "status" in body else None,
                source_message_id=body.get("source_message_id") if "source_message_id" in body else None,
                attachment_ref=body.get("attachment_ref") if "attachment_ref" in body else None,
            ))
        except ValueError as exc:
            return _fail(str(exc), 400)

    @bp.route("/api/miniapp/v1/snapshots", methods=["GET", "POST"])
    @_auth_required
    def advanced_snapshots():
        uid = int(g.shuibei_auth.user_id)
        if request.method == "GET":
            return _ok(list_close_snapshots(uid, limit=int(request.args.get("limit") or 12)))
        body = request.get_json(silent=True) or {}
        try:
            return _ok(create_close_snapshot(uid, str(body.get("period") or "")), 201)
        except ValueError as exc:
            return _fail(str(exc), 400)

    @bp.route("/api/miniapp/v1/reports/trend")
    @_auth_required
    def advanced_trend():
        uid = int(g.shuibei_auth.user_id)
        book_raw = request.args.get("book_id")
        book_id = None if book_raw in (None, "") else int(book_raw)
        start_raw = str(request.args.get("start") or "").strip()
        end_raw = str(request.args.get("end") or "").strip()
        start_ts = end_ts = 0
        if start_raw or end_raw:
            try:
                start_ts, end_ts = _query_date_range(start_raw, end_raw)
            except ValueError as exc:
                return _fail(str(exc), 400)
        return _ok(trend_series(
            uid,
            days=int(request.args.get("days") or 30),
            start_ts=start_ts,
            end_ts=end_ts,
            book_id=book_id,
        ))

    app.register_blueprint(bp)
    return bp
