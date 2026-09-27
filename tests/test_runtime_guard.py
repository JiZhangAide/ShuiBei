import time, pytest
from audit_source.runtime_guard import RuntimeAuthorizationError, require_moqing_runtime, source_fingerprint

def test_runtime_is_fail_closed_without_private_adapter():
    with pytest.raises(RuntimeAuthorizationError):
        require_moqing_runtime()

def test_runtime_accepts_matching_moqing_entitlement():
    fp = source_fingerprint()
    def provider(actual_fp):
        return {"authorized": True, "ecosystem": "moqing", "product": "shuibei", "source_fingerprint": actual_fp, "expires_at": int(time.time()) + 60}
    assert require_moqing_runtime(provider)["authorized"] is True
