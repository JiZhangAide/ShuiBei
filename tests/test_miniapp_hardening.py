from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit_source"))

import miniapp_api


STATIC_DIR = str(ROOT / "audit_source" / "miniapp_dist")


def _app():
    app = Flask(__name__)
    app.testing = True
    miniapp_api.register_shuibei_miniapp(app, static_dir=STATIC_DIR)
    return app


def _fake_auth(*args, **kwargs):
    return SimpleNamespace(
        user_id=9001,
        user={"id": 9001, "first_name": "Test", "username": "tester"},
        auth_date=1,
    )


def test_health_hides_build_and_sets_security_headers():
    response = _app().test_client().get("/ShuiBei/api/miniapp/v1/health")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "data": {"status": "ok"}}
    assert "build" not in response.get_data(as_text=True).lower()
    csp = response.headers.get("Content-Security-Policy") or ""
    assert "default-src 'self'" in csp
    assert "script-src 'self' https://telegram.org" in csp
    assert "connect-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert response.headers.get("Referrer-Policy") == "no-referrer"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


def test_read_auth_uses_30_minute_window():
    seen = []

    def validator(*args, **kwargs):
        seen.append(int(kwargs.get("max_age_seconds") or 0))
        return _fake_auth()

    app = _app()
    with patch.object(miniapp_api, "validate_init_data", side_effect=validator),          patch.object(miniapp_api, "currency", return_value="USDT"):
        response = app.test_client().get(
            "/ShuiBei/api/miniapp/v1/bootstrap",
            headers={"Authorization": "tma fake"},
        )
    assert response.status_code == 200
    assert seen == [1800]


def test_balance_changing_write_uses_10_minute_window():
    seen = []

    def validator(*args, **kwargs):
        seen.append(int(kwargs.get("max_age_seconds") or 0))
        return _fake_auth()

    app = _app()
    with patch.object(miniapp_api, "validate_init_data", side_effect=validator),          patch.object(
             miniapp_api,
             "_miniapp_add_ledger",
             return_value={"id": 1, "balance_after_micro": 0},
         ):
        response = app.test_client().post(
            "/ShuiBei/api/miniapp/v1/customers/9101/ledger",
            headers={"Authorization": "tma fake"},
            json={
                "kind": "payment",
                "amount_micro": 10000,
                "idempotency_key": "security-test-0001",
            },
        )
    assert response.status_code == 200
    assert seen == [600]


def test_settlement_write_uses_10_minute_window():
    seen = []

    def validator(*args, **kwargs):
        seen.append(int(kwargs.get("max_age_seconds") or 0))
        return _fake_auth()

    app = _app()
    with patch.object(miniapp_api, "validate_init_data", side_effect=validator),          patch.object(
             miniapp_api,
             "settle_customer",
             return_value={"peer_id": 9101, "balance_after_micro": 0},
         ):
        response = app.test_client().post(
            "/ShuiBei/api/miniapp/v1/customers/9101/settle",
            headers={"Authorization": "tma fake"},
            json={"mode": "full", "idempotency_key": "security-test-0002"},
        )
    assert response.status_code == 200
    assert seen == [600]


def test_unexpected_exception_is_generic_not_traceback():
    app = _app()
    with patch.object(miniapp_api, "validate_init_data", side_effect=_fake_auth),          patch.object(miniapp_api, "merchant_summary", side_effect=RuntimeError("private-db-detail")):
        response = app.test_client().get(
            "/ShuiBei/api/miniapp/v1/home",
            headers={"Authorization": "tma fake"},
        )
    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": {"message": "internal_error"}}
    text = response.get_data(as_text=True)
    assert "private-db-detail" not in text
    assert "Traceback" not in text
