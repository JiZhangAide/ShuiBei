# -*- coding: utf-8 -*-
"""Fail-closed MoQing runtime gate for the public audit snapshot."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Callable, Any


class RuntimeAuthorizationError(RuntimeError):
    pass


def source_fingerprint(root: Path | str | None = None) -> str:
    base = Path(root or Path(__file__).resolve().parent)
    h = hashlib.sha256()
    for path in sorted(base.glob("*.py")):
        if path.name == "__pycache__":
            continue
        h.update(path.name.encode("utf-8") + b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def require_moqing_runtime(
    entitlement_provider: Callable[[str], dict[str, Any]] | None = None,
    *,
    now_fn: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Require an entitlement issued by the private MoQing runtime.

    The actual network endpoint and transport are intentionally absent here.
    Production injects entitlement_provider from a private adapter.
    """
    if entitlement_provider is None:
        raise RuntimeAuthorizationError("MoQing private runtime adapter is required")

    fp = source_fingerprint()
    result = entitlement_provider(fp)
    if not isinstance(result, dict):
        raise RuntimeAuthorizationError("invalid runtime entitlement")

    if result.get("authorized") is not True:
        raise RuntimeAuthorizationError("runtime is not authorized")

    if str(result.get("ecosystem") or "").casefold() != "moqing":
        raise RuntimeAuthorizationError("unexpected ecosystem")

    if str(result.get("product") or "").casefold() != "shuibei":
        raise RuntimeAuthorizationError("unexpected product")

    bound_fp = str(result.get("source_fingerprint") or "")
    if bound_fp and not hmac.compare_digest(bound_fp, fp):
        raise RuntimeAuthorizationError("source fingerprint mismatch")

    expires_at = int(result.get("expires_at") or 0)
    if expires_at <= int(now_fn()):
        raise RuntimeAuthorizationError("runtime entitlement expired")

    return result


def public_entrypoint_notice() -> str:
    return (
        "This repository is an audit source snapshot. "
        "Production startup requires the private MoQing runtime adapter."
    )
