from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_TEXT = (
    "/" + "www" + "/",
    "/" + "etc" + "/",
    "127" + "." + "0" + "." + "0" + "." + "1",
    "local" + "host",
    "/" + "api" + "/" + "v1" + "/",
    "api" + "." + "jizhang" + "." + "org",
    "panel" + "." + "jizhang" + "." + "org",
    "BEGIN " + "PRIVATE KEY",
    "BEGIN " + "RSA PRIVATE KEY",
    "BEGIN " + "OPENSSH PRIVATE KEY",
)

FORBIDDEN_RUNTIME_SUFFIXES = (
    ".db", ".sqlite", ".sqlite3", ".log", ".pid", ".lock",
)

BOT_TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b")

# Literal credentials are forbidden. Environment lookups and computed values
# do not match because this pattern requires a quoted string literal.
SECRET_LITERAL_RE = re.compile(
    r"""(?ix)
    \b(?:api[_-]?key|token|secret|password|private[_-]?key)\b
    \s*=\s*
    ["'][^"'\n]{12,}["']
    """
)

PRIVATE_IPV4_RE = re.compile(
    r"(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?!\d)"
)


def _text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() in {".py", ".md", ".txt", ".json", ".yml", ".yaml"}:
            yield path


def test_public_tree_has_no_internal_paths_or_real_routes():
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in FORBIDDEN_TEXT:
            assert needle not in text, f"{needle!r} leaked in {path.relative_to(ROOT)}"
        assert not BOT_TOKEN_RE.search(text), f"bot token leaked in {path.relative_to(ROOT)}"
        assert not SECRET_LITERAL_RE.search(text), f"literal credential leaked in {path.relative_to(ROOT)}"
        assert not PRIVATE_IPV4_RE.search(text), f"private network address leaked in {path.relative_to(ROOT)}"


def test_public_tree_has_no_runtime_data_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        name = path.name.lower()
        assert not name.endswith(FORBIDDEN_RUNTIME_SUFFIXES), f"runtime data file committed: {path.relative_to(ROOT)}"
        assert not name.endswith(("-wal", "-shm", "-journal")), f"SQLite sidecar committed: {path.relative_to(ROOT)}"
