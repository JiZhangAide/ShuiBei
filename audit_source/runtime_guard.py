# -*- coding: utf-8 -*-
"""Fail-closed runtime gate for the public audit snapshot."""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

try:
    from .moqing_gateway import current_gateway
except ImportError:
    from moqing_gateway import current_gateway


class RuntimeAuthorizationError(RuntimeError):
    pass


def source_fingerprint(root: Path | str | None = None) -> str:
    base = Path(root or Path(__file__).resolve().parent)
    h = hashlib.sha256()
    for path in sorted(base.glob("*.py")):
        h.update(path.name.encode("utf-8") + b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def _validate_entitlement(
    result: dict[str, Any],
    fingerprint: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate the private runtime response without exposing a runtime bypass hook."""
    if not isinstance(result, dict) or result.get("authorized") is not True:
        raise RuntimeAuthorizationError("runtime is not authorized")
    if str(result.get("ecosystem") or "").casefold() != "moqing":
        raise RuntimeAuthorizationError("unexpected ecosystem")
    if str(result.get("product") or "").casefold() != "shuibei":
        raise RuntimeAuthorizationError("unexpected product")

    bound_fp = str(result.get("source_fingerprint") or "")
    if not bound_fp or not hmac.compare_digest(bound_fp, fingerprint):
        raise RuntimeAuthorizationError("source fingerprint mismatch")

    now_value = time.time() if now is None else float(now)
    if int(result.get("expires_at") or 0) <= int(now_value):
        raise RuntimeAuthorizationError("runtime entitlement expired")
    return result


def require_moqing_runtime() -> dict[str, Any]:
    """Require the private MoQing adapter and a matching live entitlement."""
    fp = source_fingerprint()
    try:
        result = current_gateway().runtime_entitlement(fp)
    except Exception as exc:
        raise RuntimeAuthorizationError("MoQing private runtime is unavailable") from exc
    return _validate_entitlement(result, fp)


def public_entrypoint_notice() -> str:
    return "ShuiBei public audit source requires the private MoQing runtime."
