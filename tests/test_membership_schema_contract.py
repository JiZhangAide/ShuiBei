from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMBERSHIP = (ROOT / "audit_source" / "membership.py").read_text(encoding="utf-8")
FEATURES = (ROOT / "audit_source" / "features.py").read_text(encoding="utf-8")


def test_pending_membership_tokens_are_nullable_and_real_tokens_unique():
    assert "referral_token TEXT DEFAULT NULL" in MEMBERSHIP
    assert "idx_membership_referral_token_unique" in MEMBERSHIP
    assert "WHERE referral_token IS NOT NULL AND referral_token<>''" in MEMBERSHIP


def test_membership_maintenance_indexes_are_declared():
    assert "idx_membership_activated" in MEMBERSHIP
    assert "idx_membership_reminder_due" in MEMBERSHIP
    assert "idx_referrals_pending_expiry" in MEMBERSHIP


def test_app_business_indexes_are_declared():
    assert "idx_business_enabled_owner" in FEATURES
    assert "idx_last_active_business_chat" in FEATURES
