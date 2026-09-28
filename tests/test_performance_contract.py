from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUSTOMERS = (ROOT / "audit_source" / "customers.py").read_text(encoding="utf-8")
RECEIVABLES = (ROOT / "audit_source" / "receivables.py").read_text(encoding="utf-8")
DB = (ROOT / "audit_source" / "db.py").read_text(encoding="utf-8")
LEDGER = (ROOT / "audit_source" / "ledger.py").read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    a = source.index(start)
    b = source.index(end, a)
    return source[a:b]


def test_customer_detail_is_targeted_not_full_scan():
    body = _block(CUSTOMERS, "def customer_detail(", "\ndef merchant_summary(")
    assert "list_customers(" not in body
    assert "WHERE owner_id=? AND peer_id=? LIMIT 1" in body


def test_dashboard_summary_avoids_full_crm_materialization():
    body = _block(CUSTOMERS, "def merchant_summary(", "\ndef bootstrap_customer_index(")
    assert "list_customers(" not in body
    assert "_latest_ledger_rows" in body


def test_due_lists_batch_metadata():
    body = _block(RECEIVABLES, "def _due_customers_from_rows(", "\ndef due_customers(")
    assert "due_map = _due_map(owner_id)" in body
    assert "get_due(" not in body


def test_sqlite_wal_mode_is_process_cached():
    assert "_WAL_READY" in DB
    assert "_file_identity" in DB
    assert 'conn.execute("PRAGMA journal_mode=WAL")' in DB


def test_hot_ledger_path_reuses_loaded_settings():
    body = _block(LEDGER, "def handle_text(", "\n# ================== v21")
    assert 'unit_raw = str(st.get("ledger_currency")' in body


def test_hot_ledger_balance_update_is_transactional():
    delta = _block(LEDGER, "def apply_delta(", "\ndef clear_ledger(")
    assert "with tx(LEDGER_DB_PATH, immediate=True)" in delta
    assert "SELECT balance_micro FROM ledger" in delta
    assert "INSERT INTO ledger" in delta
    hot = _block(LEDGER, "def handle_text(", "\n# ================== v21")
    assert "apply_delta(" in hot
    assert "before = get_balance" not in hot
