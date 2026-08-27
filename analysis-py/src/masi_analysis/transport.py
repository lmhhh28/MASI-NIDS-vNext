"""DNS-pinned, no-proxy, no-redirect bounded HTTP transport."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import AnalysisError
from .security import validate_outbound_url


@dataclass(frozen=True, slots=True)
class BoundedResponse:
    status_code: int
    headers: httpx.Headers
    body: bytes

    def json_object(self) -> dict[str, object]:
        try:
            value = json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnalysisError("OUTBOUND_MALFORMED", "outbound response is not strict JSON", 503, True) from exc
        if not isinstance(value, dict):
            raise AnalysisError("OUTBOUND_MALFORMED", "outbound response must be a JSON object", 503, True)
        return value


class PinnedHTTPClient:
    def __init__(
        self,
        base_url: str,
        allowed_ips: list[str],
        server_name: str,
        ssl_context: ssl.SSLContext | None,
        *,
        production: bool,
        max_connections: int,
    ) -> None:
        host, port = validate_outbound_url(base_url, allowed_ips, production=production)
        parsed = urlsplit(base_url)
        allowed = {str(ipaddress.ip_address(item)) for item in allowed_ips}
        resolved = sorted(
            {
                str(ipaddress.ip_address(item[4][0]))
                for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                if str(ipaddress.ip_address(item[4][0])) in allowed
            }
        )
        if not resolved:
            raise AnalysisError("ENDPOINT_IP_REJECTED", "no pinned outbound address remains", 503)
        self._scheme = parsed.scheme
        self._host = host
        self._port = port
        self._ip = resolved[0]
        self._server_name = server_name
        self._client = httpx.AsyncClient(
            verify=ssl_context if ssl_context is not None else True,
            follow_redirects=False,
            trust_env=False,
            timeout=None,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )

    def _url(self, path: str) -> str:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise AnalysisError("ENDPOINT_PATH_REJECTED", "outbound request path rejected", 500)
        netloc = f"{self._ip}:{self._port}"
        return urlunsplit((self._scheme, netloc, path, "", ""))

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_ms: int,
        max_response_bytes: int,
        allow_empty: bool = False,
    ) -> BoundedResponse:
        if timeout_ms < 1 or max_response_bytes < 1:
            raise AnalysisError("OUTBOUND_BOUND_REJECTED", "outbound bounds invalid", 500)
        request_headers = dict(headers)
        default_port = 443 if self._scheme == "https" else 80
        request_headers["Host"] = self._host if self._port == default_port else f"{self._host}:{self._port}"
        request = self._client.build_request(method, self._url(path), headers=request_headers, content=body)
        request.extensions["sni_hostname"] = self._server_name
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                async with self._client.stream(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=body,
                    timeout=timeout_ms / 1000,
                    extensions=request.extensions,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise AnalysisError("OUTBOUND_REDIRECT_REJECTED", "outbound redirect rejected", 503)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_response_bytes:
                            raise AnalysisError("OUTBOUND_RESPONSE_TOO_LARGE", "outbound response exceeded bound", 503)
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except AnalysisError:
            raise
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            raise AnalysisError("OUTBOUND_UNAVAILABLE", "bounded outbound request failed", 503, True) from exc
        if not raw and not allow_empty:
            raise AnalysisError("OUTBOUND_MALFORMED", "outbound response was empty", 503, True)
        return BoundedResponse(response.status_code, response.headers, raw)

    async def close(self) -> None:
        await self._client.aclose()
