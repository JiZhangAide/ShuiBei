# -*- coding: utf-8 -*-
"""Public MoQing capability contract."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable

CAP_ANTIFRAUD_RECORDS = "antifraud.records"
CAP_FAKEBOT_CHECK = "fakebot.check"
CAP_RUNTIME_ENTITLEMENT = "runtime.entitlement"
CAP_MINIAPP_LAUNCH = "miniapp.launch"

class MoQingGatewayError(RuntimeError):
    pass

class MoQingRuntimeUnavailable(MoQingGatewayError):
    pass

Transport = Callable[[str, dict[str, Any]], dict[str, Any]]

@dataclass(frozen=True)
class MoQingGateway:
    transport: Transport

    def _call(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.transport(str(capability), dict(payload))
        if not isinstance(result, dict):
            raise MoQingGatewayError("invalid MoQing response")
        return result

    def antifraud_records(self, query: str) -> dict[str, Any]:
        return self._call(CAP_ANTIFRAUD_RECORDS, {"query": str(query or "")})

    def fakebot_check(self, username: str) -> dict[str, Any]:
        return self._call(CAP_FAKEBOT_CHECK, {"username": str(username or "")})

    def runtime_entitlement(self, fingerprint: str) -> dict[str, Any]:
        return self._call(CAP_RUNTIME_ENTITLEMENT, {"product": "shuibei", "fingerprint": str(fingerprint or "")})

    def miniapp_url(self) -> str:
        result = self._call(CAP_MINIAPP_LAUNCH, {"product": "shuibei"})
        url = str(result.get("url") or "")
        if not url.startswith("https://"):
            raise MoQingGatewayError("invalid Mini App launch response")
        return url

_GATEWAY: MoQingGateway | None = None

def install_private_gateway(gateway: MoQingGateway) -> None:
    global _GATEWAY
    if not isinstance(gateway, MoQingGateway):
        raise TypeError("MoQingGateway is required")
    _GATEWAY = gateway

def current_gateway() -> MoQingGateway:
    if _GATEWAY is None:
        raise MoQingRuntimeUnavailable("MoQing private runtime adapter is required")
    return _GATEWAY
