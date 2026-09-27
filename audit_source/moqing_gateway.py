# -*- coding: utf-8 -*-
"""Public contract for MoQing-backed capabilities.

This file deliberately exposes validation semantics, not production routing.
The private runtime injects the actual transport, endpoint and authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

MAX_QUERY_PARTS = 6
MAX_QUERY_LENGTH = 25
BLOCKED_COMMANDS = {"/start", "/invite"}


class MoQingGatewayError(RuntimeError):
    pass


class MoQingRuntimeUnavailable(MoQingGatewayError):
    pass


def normalize_query_parts(parts: Iterable[str]) -> tuple[str, ...]:
    values = tuple(str(x or "").strip() for x in parts)
    if not values or not values[0]:
        raise ValueError("query1 is required")
    if len(values) > MAX_QUERY_PARTS:
        raise ValueError("at most query1..query6 are accepted")

    cleaned: list[str] = []
    for index, value in enumerate(values, start=1):
        if not value:
            continue
        if len(value) > MAX_QUERY_LENGTH:
            raise ValueError(f"query{index} exceeds {MAX_QUERY_LENGTH} characters")
        if not value.isprintable():
            raise ValueError(f"query{index} contains non-printable characters")
        folded = value.casefold()
        if any(folded == cmd or folded.startswith(cmd + " ") for cmd in BLOCKED_COMMANDS):
            raise ValueError("service commands are not valid query input")
        cleaned.append(value)

    if not cleaned:
        raise ValueError("empty query")
    return tuple(cleaned)


@dataclass(frozen=True)
class MoQingGateway:
    """Thin public interface around a private MoQing transport.

    transport is provided only by the private production runtime.  The public
    repository contains no hostname, route, key, session or infrastructure path.
    """

    transport: Callable[[str, dict[str, Any]], dict[str, Any]]

    def antifraud_query(self, *parts: str) -> dict[str, Any]:
        normalized = normalize_query_parts(parts)
        payload = {f"query{i}": value for i, value in enumerate(normalized, start=1)}
        result = self.transport("antifraud.query", payload)
        if not isinstance(result, dict):
            raise MoQingGatewayError("invalid MoQing response")
        return result

    def runtime_entitlement(self, fingerprint: str) -> dict[str, Any]:
        result = self.transport(
            "runtime.entitlement",
            {"product": "shuibei", "fingerprint": str(fingerprint or "")},
        )
        if not isinstance(result, dict):
            raise MoQingGatewayError("invalid entitlement response")
        return result
