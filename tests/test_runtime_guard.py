import inspect
import time

import pytest
import audit_source.runtime_guard as runtime_guard


def test_runtime_entrypoint_has_no_provider_injection_hook():
    assert list(inspect.signature(runtime_guard.require_moqing_runtime).parameters) == []


def test_runtime_is_fail_closed_without_private_adapter(monkeypatch):
    def unavailable():
        raise RuntimeError("private adapter unavailable")

    monkeypatch.setattr(runtime_guard, "current_gateway", unavailable)
    with pytest.raises(runtime_guard.RuntimeAuthorizationError):
        runtime_guard.require_moqing_runtime()


def test_runtime_accepts_matching_moqing_entitlement(monkeypatch):
    class Gateway:
        def runtime_entitlement(self, actual_fp):
            return {
                "authorized": True,
                "ecosystem": "moqing",
                "product": "shuibei",
                "source_fingerprint": actual_fp,
                "expires_at": int(time.time()) + 60,
            }

    monkeypatch.setattr(runtime_guard, "current_gateway", lambda: Gateway())
    assert runtime_guard.require_moqing_runtime()["authorized"] is True


def test_entitlement_validation_rejects_expired_response():
    fp = runtime_guard.source_fingerprint()
    with pytest.raises(runtime_guard.RuntimeAuthorizationError):
        runtime_guard._validate_entitlement(
            {
                "authorized": True,
                "ecosystem": "moqing",
                "product": "shuibei",
                "source_fingerprint": fp,
                "expires_at": 100,
            },
            fp,
            now=101,
        )
