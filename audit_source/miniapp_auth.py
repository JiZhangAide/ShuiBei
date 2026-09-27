# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


@dataclass(frozen=True)
class MiniAppAuth:
    user_id: int
    user: dict
    auth_date: int
    query_id: str = ""
    start_param: str = ""


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    now_ts: int | None = None,
    max_age_seconds: int = 600,
) -> MiniAppAuth:
    """Validate Telegram Mini App initData exactly as documented by Telegram."""
    pairs = parse_qsl(str(init_data or ""), keep_blank_values=True)
    keys = [k for k, _ in pairs]
    if not pairs or len(keys) != len(set(keys)):
        raise ValueError("invalid mini app authorization")

    data = dict(pairs)
    provided = data.pop("hash", "")
    if len(provided) != 64:
        raise ValueError("invalid mini app authorization")

    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", str(bot_token).encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise ValueError("invalid mini app authorization")

    now = int(time.time() if now_ts is None else now_ts)
    try:
        auth_date = int(data.get("auth_date") or 0)
    except Exception as exc:
        raise ValueError("invalid mini app authorization") from exc
    if auth_date <= 0 or now - auth_date > int(max_age_seconds) or auth_date - now > 60:
        raise ValueError("invalid mini app authorization")

    try:
        user = json.loads(data.get("user") or "{}")
    except Exception as exc:
        raise ValueError("invalid mini app authorization") from exc

    uid = int(user.get("id") or 0)
    if uid <= 0 or bool(user.get("is_bot")):
        raise ValueError("invalid mini app authorization")

    return MiniAppAuth(
        uid,
        user,
        auth_date,
        str(data.get("query_id") or ""),
        str(data.get("start_param") or ""),
    )
