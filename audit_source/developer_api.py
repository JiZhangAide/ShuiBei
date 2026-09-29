# -*- coding: utf-8 -*-
from __future__ import annotations

import requests

try:
    from .config import DEVELOPER_API_BASE, DEVELOPER_API_KEY, HTTP_TIMEOUT
except ImportError:
    from config import DEVELOPER_API_BASE, DEVELOPER_API_KEY, HTTP_TIMEOUT


class DeveloperAPIUnavailable(RuntimeError):
    pass


def _api_key() -> str:
    value = str(DEVELOPER_API_KEY or "").strip()
    if not value:
        raise DeveloperAPIUnavailable("developer_api_key_unavailable")
    return value


def get_json(path: str, *, params: dict | None = None) -> dict:
    rel = "/" + str(path or "").lstrip("/")
    url = str(DEVELOPER_API_BASE).rstrip("/") + rel
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + _api_key(),
        "User-Agent": "ShuiBei/OpenSource",
    }
    try:
        response = requests.get(
            url,
            params=dict(params or {}),
            headers=headers,
            timeout=(4, max(5, int(HTTP_TIMEOUT))),
        )
    except requests.RequestException as exc:
        raise DeveloperAPIUnavailable("developer_api_unreachable") from exc

    try:
        payload = response.json()
    except Exception as exc:
        raise DeveloperAPIUnavailable("developer_api_invalid_response") from exc

    if response.status_code in {200, 400} and isinstance(payload, dict):
        return payload
    raise DeveloperAPIUnavailable("developer_api_unavailable")
