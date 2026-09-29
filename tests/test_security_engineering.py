import importlib
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

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
import advanced_ledger
import miniapp_api

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
    miniapp_api._RATE_LIMIT_STATE.clear()


def setup_function():
    _reset()


def test_projection_outbox_survives_projection_failure_and_replays_once():
    owner, peer = 93001, 94001
    ledger.add_record(owner, peer, "Outbox", "出", 10000, -10000, "seed")

    with patch.object(
        advanced_ledger,
        "_apply_projection_event",
        side_effect=RuntimeError("simulated-app-db-down"),
    ):
        result = advanced_ledger.record_entry(
            owner,
            peer,
            kind="payment",
            amount_micro=5000,
            remark="outbox-test",
        )

    assert result["balance_after_micro"] == -5000
    assert ledger.get_balance(owner, peer) == -5000

    conn = db.connect(config.LEDGER_DB_PATH)
    try:
        pending = conn.execute(
            "SELECT applied_at,attempts,last_error FROM ledger_projection_outbox WHERE owner_id=?",
            (owner,),
        ).fetchall()
    finally:
        conn.close()
    assert len(pending) == 1
    assert int(pending[0]["applied_at"] or 0) == 0
    assert int(pending[0]["attempts"] or 0) >= 1
    assert "simulated-app-db-down" in str(pending[0]["last_error"] or "")

    replay = advanced_ledger.drain_projection_outbox(owner)
    assert replay["applied"] == 1
    assert replay["failed"] == 0

    meta = advanced_ledger.entry_meta_map(owner, [int(result["id"])])
    assert int(result["id"]) in meta
    timeline = advanced_ledger.customer_timeline(owner, peer, limit=30)
    matches = [
        x for x in timeline
        if x.get("kind") == "ledger_created"
        and int(x.get("ref_id") or 0) == int(result["id"])
    ]
    assert len(matches) == 1

    advanced_ledger.drain_projection_outbox(owner)
    timeline2 = advanced_ledger.customer_timeline(owner, peer, limit=30)
    matches2 = [
        x for x in timeline2
        if x.get("kind") == "ledger_created"
        and int(x.get("ref_id") or 0) == int(result["id"])
    ]
    assert len(matches2) == 1


def test_rate_limiter_returns_429_and_retry_after():
    from flask import Flask
    from types import SimpleNamespace

    app = Flask(__name__)
    app.testing = True
    miniapp_api.register_shuibei_miniapp(
        app,
        static_dir=str(Path(miniapp_api.__file__).resolve().parent / "miniapp_dist"),
    )

    fake_auth = SimpleNamespace(
        user_id=95001,
        user={"id": 95001, "first_name": "Rate", "username": "rate"},
        auth_date=1,
    )

    miniapp_api._RATE_LIMIT_STATE.clear()
    old = dict(miniapp_api._RATE_LIMITS)
    miniapp_api._RATE_LIMITS["read"] = 2
    try:
        with patch.object(miniapp_api, "validate_init_data", return_value=fake_auth), \
             patch.object(miniapp_api, "currency", return_value="USDT"):
            client = app.test_client()
            headers = {"Authorization": "tma fake"}
            assert client.get("/ShuiBei/api/miniapp/v1/bootstrap", headers=headers).status_code == 200
            assert client.get("/ShuiBei/api/miniapp/v1/bootstrap", headers=headers).status_code == 200
            response = client.get("/ShuiBei/api/miniapp/v1/bootstrap", headers=headers)
            assert response.status_code == 429
            assert int(response.headers.get("Retry-After") or 0) >= 1
    finally:
        miniapp_api._RATE_LIMITS.clear()
        miniapp_api._RATE_LIMITS.update(old)
        miniapp_api._RATE_LIMIT_STATE.clear()


def test_local_db_mode_is_hardened_to_0600():
    path = config.APP_DB_PATH
    path.touch(exist_ok=True)
    os.chmod(path, 0o644)
    db._harden_local_db_mode(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_strict_db_permission_failure_is_not_silenced():
    path = config.APP_DB_PATH
    path.touch(exist_ok=True)
    os.chmod(path, 0o644)
    old = os.environ.get("SHUIBEI_STRICT_DB_PERMISSIONS")
    os.environ["SHUIBEI_STRICT_DB_PERMISSIONS"] = "1"
    try:
        with patch.object(db.os, "chmod", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError):
                db._harden_local_db_mode(path)
    finally:
        if old is None:
            os.environ.pop("SHUIBEI_STRICT_DB_PERMISSIONS", None)
        else:
            os.environ["SHUIBEI_STRICT_DB_PERMISSIONS"] = old
        os.chmod(path, 0o600)


def test_customer_profile_and_monthly_goal_round_trip():
    owner, peer = 96001, 97001
    ledger.add_record(owner, peer, "ProfileUser", "出", 10000, -10000, "seed")

    profile = advanced_ledger.set_customer_profile(owner, peer, alias="服务器客户", pinned=True)
    assert profile["alias"] == "服务器客户"
    assert profile["pinned"] is True
    stored = advanced_ledger.customer_profile_map(owner, [peer])[peer]
    assert stored["alias"] == "服务器客户"
    assert stored["pinned"] is True

    goal = advanced_ledger.set_goal(owner, 50000000)
    assert goal["target_micro"] == 50000000
    current = advanced_ledger.current_goal(owner)
    assert current["target_micro"] == 50000000
    assert current["current_micro"] >= 0


def test_miniapp_static_contains_bookkeeping_convenience_features():
    app_js = (Path(miniapp_api.__file__).resolve().parent / "miniapp_dist" / "app.js").read_text(encoding="utf-8")
    css = (Path(miniapp_api.__file__).resolve().parent / "miniapp_dist" / "style.css").read_text(encoding="utf-8")
    for needle in (
        "amountExpr",
        "复制上一笔",
        "客户别名",
        "置顶客户",
        "本月经营目标",
        "overdue_1_3",
        "overdue_4_7",
        "overdue_8_30",
        "overdue_30",
        "线性统计图",
    ):
        assert needle in app_js
    assert ".goal-card" in css
    assert ".profile-strip" in css
    assert ".trend-chart" in css
