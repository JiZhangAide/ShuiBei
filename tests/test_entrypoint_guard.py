from pathlib import Path


def test_app_run_calls_runtime_guard_first():
    path = Path(__file__).resolve().parents[1] / "audit_source" / "app.py"
    text = path.read_text(encoding="utf-8")
    marker = "def run() -> None:"
    start = text.index(marker)
    body = text[start:start + 220]
    assert "require_moqing_runtime()" in body
    assert "entitlement_provider" not in body
