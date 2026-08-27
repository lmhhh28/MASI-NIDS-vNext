"""Transport, path, redaction, and output-safety enforcement."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .errors import AnalysisError, UnsafeOutput

_SECRET_PATTERNS = (
    re.compile(r"(?i)authorization\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_FORBIDDEN_OUTPUT = (
    re.compile(r"(?i)\bP4Runtime\b"),
    re.compile(r"(?i)\bTableEntry\b"),
    re.compile(r"(?i)\beffect[_ -]?intent\b"),
    re.compile(r"(?i)\bapproval[_ -]?token\b"),
    re.compile(r"(?i)\b(?:deploy|approve|rollback|execute)\s*(?:now|automatically)\b"),
    re.compile(r"(?i)<\s*(?:script|iframe|object|svg)\b"),
    re.compile(r"(?i)javascript\s*:"),
    re.compile(r"https?://"),
)


def read_bounded_regular_file(path: str, *, max_bytes: int, secret: bool = False) -> bytes:
    candidate = Path(path)
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise AnalysisError("FILE_UNAVAILABLE", "required runtime file unavailable", 503) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        raise AnalysisError("FILE_REJECTED", "runtime file must be bounded, regular, and no-symlink", 503)
    if info.st_mode & stat.S_IWOTH:
        raise AnalysisError("FILE_REJECTED", "world-writable runtime file rejected", 503)
    if secret and info.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        raise AnalysisError("SECRET_MODE_REJECTED", "secret file permissions are too broad", 503)
    return candidate.read_bytes()


def validate_storage_path(path: str, production: bool) -> Path:
    candidate = Path(path)
    if production and not candidate.is_absolute():
        raise AnalysisError("STORE_PATH_REJECTED", "production store path must be absolute", 503)
    parent = candidate.parent
    if not parent.exists() or parent.is_symlink() or not parent.is_dir():
        raise AnalysisError("STORE_PATH_REJECTED", "store parent must be an existing ordinary directory", 503)
    if production:
        parent_info = parent.stat()
        if parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o077:
            raise AnalysisError("STORE_PATH_REJECTED", "production store parent must be private and owned by the runtime identity", 503)
    if candidate.exists() and (candidate.is_symlink() or not candidate.is_file()):
        raise AnalysisError("STORE_PATH_REJECTED", "store must be a regular no-symlink file", 503)
    if candidate.exists():
        info = candidate.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise AnalysisError("STORE_PATH_REJECTED", "store file must be private and owned by the runtime identity", 503)
    return candidate


def validate_outbound_url(url: str, allowed_ips: list[str], *, production: bool) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise AnalysisError("ENDPOINT_REJECTED", "outbound endpoint contains forbidden components", 503)
    if parsed.scheme not in ({"https"} if production else {"http", "https"}) or not parsed.hostname:
        raise AnalysisError("ENDPOINT_REJECTED", "outbound endpoint scheme or host rejected", 503)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    allowed = {str(ipaddress.ip_address(item)) for item in allowed_ips}
    resolved: set[str] = set()
    try:
        for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM):
            resolved.add(str(ipaddress.ip_address(item[4][0])))
    except OSError as exc:
        raise AnalysisError("ENDPOINT_DNS_FAILED", "outbound endpoint resolution failed", 503, True) from exc
    if not resolved or not resolved.issubset(allowed):
        raise AnalysisError("ENDPOINT_IP_REJECTED", "outbound endpoint resolved outside the static allowlist", 503)
    if production and any(ipaddress.ip_address(item).is_loopback for item in resolved):
        raise AnalysisError("ENDPOINT_IP_REJECTED", "production outbound endpoint must not be loopback", 503)
    return parsed.hostname, port


def client_ssl_context(ca_file: str | None, cert_file: str | None, key_file: str | None) -> ssl.SSLContext | None:
    if ca_file is None and cert_file is None and key_file is None:
        return None
    if not ca_file or not cert_file or not key_file:
        raise AnalysisError("TLS_CONFIG_REJECTED", "outbound TLS requires CA, certificate, and key", 503)
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.load_cert_chain(cert_file, key_file)
    return context


def server_ssl_context(cert_file: str, key_file: str, client_ca_file: str) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert_file, key_file)
    context.load_verify_locations(cafile=client_ca_file)
    return context


def peer_sans(ssl_object: ssl.SSLObject | None) -> set[str]:
    if ssl_object is None:
        return set()
    cert = ssl_object.getpeercert()
    if not cert:
        return set()
    values: set[str] = set()
    for kind, value in cert.get("subjectAltName", ()):
        if kind in {"DNS", "URI"}:
            values.add(str(value))
    return values


def redact_text(value: str, secret_values: tuple[str, ...] = ()) -> str:
    clean = value
    for marker in secret_values:
        if marker:
            clean = clean.replace(marker, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        clean = pattern.sub("[REDACTED]", clean)
    return clean


def require_safe_output(value: str, secret_values: tuple[str, ...] = ()) -> str:
    clean = redact_text(value, secret_values)
    if any(pattern.search(clean) for pattern in _FORBIDDEN_OUTPUT):
        raise UnsafeOutput()
    if len(clean.encode()) > 4096:
        raise UnsafeOutput("provider text exceeds the per-field bound")
    return clean


def atomic_write_private(path: Path, raw: bytes) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
