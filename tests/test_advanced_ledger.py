import importlib
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "audit_source" if (ROOT / "audit_source").is_dir() else ROOT
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

os.environ.setdefault("SHUIBEI_DATA_DIR", f"/tmp/shuibei-tests-{os.getpid()}")

import config
import db
import features
import advanced_ledger
import ledger
import receivables

assert str(config.APP_DB_PATH).startswith("/tmp/shuibei-tests-"), "tests must never use production ShuiBei data"

DBS = (config.APP_DB_PATH, config.LEDGER_DB_PATH, config.ARCHIVE_DB_PATH, config.SYNC_DB_PATH)


def _reset():
    for p in DBS:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(str(p) + suffix)
            except FileNotFoundError:
                pass
    db._SCHEMA_READY.clear()
    features._FEATURE_SCHEMA_READY = False
    db.init_all()
    features.ensure_feature_schema()
    advanced_ledger._SCHEMA_GENERATION = -1
    importlib.reload(advanced_ledger)


def test_book_template_apply_and_reverse_keeps_audit_trail():
    _reset()
    ledger.add_record(41, 4101, "Alice", "出", 100000, -100000, "seed", category="服务", cost_micro=30000)
    book = advanced_ledger.create_book(41, "服务器项目")
    template = advanced_ledger.create_template(
        41,
        "月费",
        peer_id=4101,
        book_id=book["id"],
        kind="debt",
        amount_micro=50000,
        remark="月费",
        category="服务器",
        cost_micro=20000,
    )
    result = advanced_ledger.apply_template(41, int(template["id"]), peer_id=4101)
    assert ledger.get_balance(41, 4101) == -150000
    meta = advanced_ledger.entry_meta_map(41, [int(result["id"])])[int(result["id"])]
    assert int(meta["book_id"]) == int(book["id"])

    reversed_row = advanced_ledger.reverse_entry(41, int(result["id"]))
    assert ledger.get_balance(41, 4101) == -100000
    timeline = advanced_ledger.customer_timeline(41, 4101, limit=20)
    assert any(x.get("kind") == "ledger_reversed" for x in timeline)
    report = receivables.report_summary(41, "day")
    assert report["outflow_micro"] == 100000
    assert report["gross_profit_micro"] == 70000
    assert int(reversed_row["balance_after_micro"]) == -100000


def test_recurring_receivable_is_idempotent_for_same_period():
    _reset()
    ledger.add_record(42, 4201, "Bob", "入", 1, 1, "seed")
    now = int(time.time())
    advanced_ledger.create_recurring_receivable(
        42,
        4201,
        title="每周服务费",
        amount_micro=25000,
        cadence="weekly",
        next_due_at=now - 60,
        remark="周期服务费",
        category="服务",
    )
    first = advanced_ledger.process_due_recurring(42, now_ts=now, max_runs=10)
    second = advanced_ledger.process_due_recurring(42, now_ts=now, max_runs=10)
    assert len(first) == 1
    assert second == []
    assert ledger.get_balance(42, 4201) == 1 - 25000


def test_close_snapshot_is_immutable_per_period():
    _reset()
    ledger.add_record(43, 4301, "Carol", "出", 60000, -60000, "sale", category="服务", cost_micro=20000)
    a = advanced_ledger.create_close_snapshot(43, "month")
    b = advanced_ledger.create_close_snapshot(43, "month")
    assert a["period_key"] == b["period_key"]
    assert a["summary"]["gross_profit_micro"] == 40000
    assert b["summary"] == a["summary"]
    assert len(advanced_ledger.list_close_snapshots(43)) == 1


def test_customer_summary_timeline_and_trend():
    _reset()
    now = int(time.time())
    ledger.add_record(44, 4401, "Dave", "出", 100000, -100000, "sale", category="服务", cost_micro=40000)
    ledger.apply_delta(44, 4401, "Dave", "入", 30000, "partial")
    summary = advanced_ledger.customer_summary(44, 4401)
    assert summary["record_count"] == 2
    assert summary["turnover_micro"] == 130000
    assert summary["current_balance_micro"] == -70000

    timeline = advanced_ledger.customer_timeline(44, 4401, limit=10)
    assert len([x for x in timeline if x.get("kind") == "ledger"]) >= 2

    trend = advanced_ledger.trend_series(44, days=7, end_ts=now + 60, start_ts=now - 6 * 86400)
    assert len(trend["series"]) >= 6
    assert sum(x["outflow_micro"] for x in trend["series"]) == 100000
    assert sum(x["inflow_micro"] for x in trend["series"]) == 30000
    assert sum(x["gross_profit_micro"] for x in trend["series"]) == 60000
