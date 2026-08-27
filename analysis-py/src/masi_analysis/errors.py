"""Stable domain errors used at the public boundary."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AnalysisError(Exception):
    code: str
    message: str
    http_status: int = 400
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ProviderUnavailable(AnalysisError):
    def __init__(self, message: str = "provider unavailable") -> None:
        super().__init__("PROVIDER_UNAVAILABLE", message, 503, True)


class MCPUnavailable(AnalysisError):
    def __init__(self, message: str = "MCP unavailable") -> None:
        super().__init__("MCP_UNAVAILABLE", message, 503, True)


class PeerUnavailable(AnalysisError):
    def __init__(self, message: str = "A2A peer unavailable") -> None:
        super().__init__("PEER_UNAVAILABLE", message, 503, True)


class UnsafeOutput(AnalysisError):
    def __init__(self, message: str = "unsafe provider output rejected") -> None:
        super().__init__("UNSAFE_OUTPUT", message, 422, False)
