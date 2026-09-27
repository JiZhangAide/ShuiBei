from pathlib import Path
import re
ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "/" + "www" + "/", "/" + "etc" + "/",
    "127" + "." + "0" + "." + "0" + "." + "1", "local" + "host",
    "/" + "api" + "/" + "v1" + "/",
    "api" + "." + "jizhang" + "." + "org",
    "panel" + "." + "jizhang" + "." + "org",
)
BOT_TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b")

def test_public_tree_has_no_internal_paths_or_real_routes():
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts: continue
        if path.suffix.lower() not in {".py", ".md", ".txt", ".json", ".yml", ".yaml"}: continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in FORBIDDEN:
            assert needle not in text, f"{needle!r} leaked in {path.relative_to(ROOT)}"
        assert not BOT_TOKEN_RE.search(text)
