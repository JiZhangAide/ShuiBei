from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FEATURES = (ROOT / "audit_source" / "features.py").read_text(encoding="utf-8")
EMOJI = (ROOT / "audit_source" / "emoji_knowledge.py").read_text(encoding="utf-8")
TELEGRAM = (ROOT / "audit_source" / "telegram_api.py").read_text(encoding="utf-8")


def test_compact_welcome_copy_is_auditable():
    assert "墨清记账的轻量版，只留下常用能力。" in FEATURES
    assert "很高兴认识您" not in FEATURES
    assert "━━━━━━━━━━━━━━━" not in FEATURES
    assert "system_icon('welcome','🙂')" in FEATURES


def test_premium_core_ids_use_current_ledger_icon():
    assert '"ledger": "5951665890079544884"' in EMOJI
    assert '"welcome": "5985780596268339498"' in EMOJI
    assert '"ledger": "5875206779196935958"' not in EMOJI


def test_button_fallback_does_not_preemptively_strip_text_emoji():
    button_pos = TELEGRAM.index("if markup and fallback_markup != markup")
    text_pos = TELEGRAM.index('elif "<tg-emoji" in raw_text')
    assert button_pos < text_pos
