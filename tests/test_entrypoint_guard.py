from pathlib import Path

def test_app_run_calls_runtime_guard_first():
    path = Path(__file__).resolve().parents[1] / "audit_source" / "app.py"
    text = path.read_text(encoding="utf-8")
    marker = "def run(entitlement_provider=None) -> None:"
    start = text.index(marker)
    body = text[start:start + 240]
    assert "require_moqing_runtime(entitlement_provider)" in body
