from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "audit_source" / "app.py").read_text(encoding="utf-8")


def test_button_color_policy_is_selection_only():
    helper = APP[APP.index("def button("):APP.index("\ndef kb(", APP.index("def button("))]
    assert 'if selected:' in helper
    assert 'out["style"] = "success"' in helper
    assert 'style_norm' not in helper
    assert 'out["style"] = style_norm' not in helper


def test_action_buttons_are_not_marked_selected():
    assert 'button("收到", "onboarding:ack", selected=True' not in APP
    assert 'button("确认同步一次", "sync:run", selected=True' not in APP
    assert 'settings:currency",selected=True' not in APP


def test_compact_high_frequency_copy():
    assert '客户往来点「客户」，记账与报表点「财务」。' not in APP
    assert 'Business 私聊发送关键词 → 自动发送模板。' in APP
    assert '_ui_field("在线时", "不回复" if skip_online else "仍回复")' in APP
