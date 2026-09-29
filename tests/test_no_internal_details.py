from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_TEXT = (
    "/" + "www" + "/",
    "/" + "etc" + "/",
    "0" + "." + "0" + "." + "0" + "." + "0",
    "local" + "host",
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
    \b(?:api[_-]?key|api[_-]?hash|token|secret|password|private[_-]?key|session)\b
    \s*=\s*
    ["'][^"'\n]{12,}["']
    """
)

TELEGRAM_API_ID_LITERAL_RE = re.compile(
    r"""(?ix)
    \b(?:api[_-]?id|telegram[_-]?api[_-]?id)\b
    \s*=\s*
    \d{5,}
    """
)

ABSOLUTE_DEPLOYMENT_PATH_RE = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9])
    /
    (?:root|home|srv|opt|var/www|usr/local)
    /
    [^\s"'<>]+
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
        if path.suffix.lower() in {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".js", ".html", ".css", ".example"}:
            yield path


def test_public_tree_has_no_internal_paths_or_real_routes():
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(ROOT)
        for needle in FORBIDDEN_TEXT:
            assert needle not in text, f"{needle!r} leaked in {rel}"
        assert not BOT_TOKEN_RE.search(text), f"bot token leaked in {rel}"
        assert not SECRET_LITERAL_RE.search(text), f"literal credential leaked in {rel}"
        assert not TELEGRAM_API_ID_LITERAL_RE.search(text), f"Telegram API id leaked in {rel}"
        assert not ABSOLUTE_DEPLOYMENT_PATH_RE.search(text), f"absolute deployment path leaked in {rel}"
        assert not PRIVATE_IPV4_RE.search(text), f"private network address leaked in {rel}"


def test_public_tree_has_no_runtime_data_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        name = path.name.lower()
        rel = path.relative_to(ROOT)
        assert not name.endswith(FORBIDDEN_RUNTIME_SUFFIXES), f"runtime data file committed: {rel}"
        assert not name.endswith(("-wal", "-shm", "-journal")), f"SQLite sidecar committed: {rel}"


def test_only_documented_public_developer_routes_are_exposed():
    config_text = (ROOT / "audit_source" / "config.py").read_text(encoding="utf-8")
    assert 'DEVELOPER_API_BASE = "https://api.jizhang.org"' in config_text
    routes = set(re.findall(r'"/api/v1/[^"]+"', config_text))
    assert routes == {
        '"/api/v1/fanzha/records"',
        '"/api/v1/fakebot/check"',
    }


def test_public_runtime_binds_loopback_by_default():
    config_text = (ROOT / "audit_source" / "config.py").read_text(encoding="utf-8")
    assert 'SHUIBEI_MINIAPP_HOST") or "127.0.0.1"' in config_text
    bind_all = '"' + "0" + "." + "0" + "." + "0" + "." + "0" + '"'
    assert bind_all not in config_text
