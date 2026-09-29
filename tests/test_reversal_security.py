import importlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "audit_source" if (ROOT / "audit_source").is_dir() else ROOT
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

os.environ.setdefault("SHUIBEI_DATA_DIR", f"/tmp/shuibei-tests-{os.getpid()}")

import config
import db
import features
import ledger
import receivables
import advanced_ledger

assert str(config.APP_DB_PATH).startswith("/tmp/shuibei-tests-"), "tests must never use production ShuiBei data"
DBS = (config.APP_DB_PATH, config.LEDGER_DB_PATH, config.ARCHIVE_DB_PATH, config.SYNC_DB_PATH)


def setup_function():
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


def _seed(owner=91001, peer=92001):
    return ledger.add_record(
        owner, peer, "SecurityTest", "出", 100000, -100000, "original",
        category="服务", cost_micro=40000,
    )


def test_reversed_status_cannot_be_set_by_generic_metadata():
    owner, peer = 91001, 92001
    lid = _seed(owner, peer)
    with pytest.raises(ValueError, match="只能由冲正流程"):
        advanced_ledger.set_entry_meta(owner, lid, status="reversed")
    report = receivables.report_range_summary(owner, int(time.time()) - 60, int(time.time()) + 60)
    assert report["outflow_micro"] == 100000
    assert report["gross_profit_micro"] == 60000
    assert ledger.get_balance(owner, peer) == -100000


def test_metadata_projection_cannot_hide_accounting_fact():
    owner, peer = 91002, 92002
    lid = _seed(owner, peer)
    advanced_ledger.set_entry_meta(owner, lid, status="reversed", _internal=True)
    report = receivables.report_range_summary(owner, int(time.time()) - 60, int(time.time()) + 60)
    assert report["outflow_micro"] == 100000
    assert report["classified_sales_micro"] == 100000
    assert report["record_count"] == 1
    assert ledger.get_balance(owner, peer) == -100000


def test_concurrent_reverse_changes_balance_exactly_once():
    owner, peer = 91003, 92003
    lid = _seed(owner, peer)

    def go(i):
        try:
            return ("ok", advanced_ledger.reverse_entry(owner, lid, remark=f"race-{i}"))
        except ValueError as exc:
            return ("err", str(exc))

    with ThreadPoolExecutor(max_workers=2) as ex:
        rows = list(ex.map(go, (1, 2)))

    assert sum(1 for row in rows if row[0] == "ok") == 1
    assert sum(1 for row in rows if row[0] == "err") == 1
    assert ledger.get_balance(owner, peer) == 0

    conn = db.connect(config.LEDGER_DB_PATH)
    try:
        rel = conn.execute(
            "SELECT id,reversal_of,reversed_by FROM ledger WHERE owner_id=? ORDER BY id",
            (owner,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rel) == 2
    original, reversal = rel
    assert int(original["reversed_by"] or 0) == int(reversal["id"])
    assert int(reversal["reversal_of"] or 0) == int(original["id"])


def test_database_unique_constraint_allows_only_one_reversal():
    owner, peer = 91004, 92004
    lid = _seed(owner, peer)
    advanced_ledger.reverse_entry(owner, lid)

    conn = db.connect(config.LEDGER_DB_PATH)
    try:
        with pytest.raises(Exception):
            conn.execute(
                """INSERT INTO ledger(
                       owner_id,peer_id,user_name,action,amount_micro,balance_micro,remark,time,
                       category,cost_micro,reversal_of,reversed_by
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)""",
                (
                    owner, peer, "SecurityTest", "入", 100000, 100000, "duplicate",
                    time.strftime("%Y-%m-%d %H:%M:%S"), "", 0, lid,
                ),
            )
    finally:
        conn.close()
